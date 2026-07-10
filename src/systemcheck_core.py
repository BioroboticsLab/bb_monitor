"""Pure decision logic for bb_monitor_systemcheck.

Everything here is a function of its arguments — no SSH, no config, no Telegram — so
the parts of the system check that are easy to get subtly wrong (which findings are
confirmed, whether a clock skew is real, whether a recovery is genuine) can be
tested without a network or a Raspberry Pi. See tests/test_systemcheck_core.py.

bb_monitor_systemcheck.py does the I/O and calls into here.
"""
import statistics
from typing import NamedTuple, Optional

# Host used for the "your own clock is wrong" finding, which belongs to no device.
MONITOR_HOST = "__monitor__"


class PingSettings(NamedTuple):
    """How hard to try before calling a host unreachable.

    Retries are *spaced*, not back-to-back, so that the attempts outlast a hiccup on
    the monitor host's own link rather than all landing inside it. A misbehaving
    mDNS lookup shows up either way: it can stall for seconds (WiFi power save delays
    the multicast exchange) or fail instantly once the interface has gone away.
    retry_span_seconds() assumes the instant-failure case because it is the one where
    the timeouts contribute nothing, making it a lower bound on coverage.
    """
    timeout_seconds: float = 2
    attempts: int = 3
    retry_delay_seconds: float = 5

    def retry_span_seconds(self):
        """Lower bound on the wall clock the retries cover.

        When every attempt fails instantly, only the delays separate them. When
        attempts instead stall to their deadline, the real span is longer.
        """
        return max(0, self.attempts - 1) * self.retry_delay_seconds

    def worst_case_seconds(self):
        """Wall clock per host when every attempt runs to its full timeout."""
        per_attempt = self.timeout_seconds + RESOLVE_GRACE_SECONDS
        return self.attempts * per_attempt + self.retry_span_seconds()


class PingResult(NamedTuple):
    """Outcome of pinging one host.

    `monitor_side` is the load-bearing bit. Name resolution happens on *this*
    machine, so a lookup that stalls or fails is never the remote camera's fault —
    it is a fact about the monitor, no matter which hostname was being resolved.
    Distinguishing that from "the host did not answer" is what lets N identical
    alerts collapse into the one machine actually at fault.
    """
    ok: bool
    reason: Optional[str] = None
    monitor_side: bool = False


# `ping -W` bounds only the wait for an ICMP reply; name resolution happens before
# that and is unbounded by it. Give each attempt this much extra wall clock so a
# slow mDNS lookup is not mistaken for an unreachable host.
RESOLVE_GRACE_SECONDS = 3


class Finding(NamedTuple):
    """One failing check.

    `host` + `kind` identify the *probe*, not the failure reason, so a probe whose
    message changes between ticks ("stale (166s old)" then "(767s old)") still
    matches itself across ticks and can be confirmed. Never fold volatile detail
    into `kind`, and never try to recover the probe from the message: several
    probes emit the identical "ssh exec failed (...)" wording.
    """
    host: str
    kind: str
    message: str
    remediable: bool = False
    ssh_target: Optional[str] = None
    monitor_side: bool = False

    @property
    def key(self):
        return (self.host, self.kind)


def clock_skew(t0, remote_epoch, t1):
    """Bound the remote clock's offset from ours, given local times either side of
    the round trip. Returns (skew, offset).

    The remote read happened somewhere in [t0, t1], so the true offset lies in
    [remote-t1, remote-t0]. `skew` is that interval's closest point to zero — a
    lower bound on the real error, so a slow round trip can only ever *shrink* it
    and never invent a false positive. `offset` is the midpoint estimate, signed
    (positive = remote ahead of us), used only to tell "this device is wrong" apart
    from "we are wrong".
    """
    skew = max(0.0, remote_epoch - t1, t0 - remote_epoch)
    offset = remote_epoch - (t0 + t1) / 2.0
    return skew, offset


def clock_findings(samples, max_skew_seconds):
    """Turn per-host (host, skew, offset) clock samples into findings.

    Samples are compared as *offsets*, never as raw remote epochs: a tick's SSH
    calls are serialized over tens of seconds, so two hosts' `date` output is not
    directly comparable, while their offsets from our clock are.
    """
    violations = [s for s in samples if s[1] > max_skew_seconds]
    if not violations:
        return []
    # Every device we could reach disagrees with us, all in the same direction:
    # the one clock they have no say in is ours.
    if (len(violations) >= 2
            and len(violations) == len(samples)
            and _same_sign(offset for _, _, offset in violations)):
        typical = statistics.median(abs(offset) for _, _, offset in violations)
        return [Finding(
            MONITOR_HOST, "clock",
            f"Monitor host clock may be wrong "
            f"(all {len(violations)} devices differ by ~{typical:.0f}s)",
        )]
    return [
        Finding(host, "clock",
                f"{host}: clock off by {skew:.0f}s from monitor (max {max_skew_seconds}s)")
        for host, skew, _ in violations
    ]


def _same_sign(values):
    values = list(values)
    return all(v > 0 for v in values) or all(v < 0 for v in values)


COLLAPSE_THRESHOLD = 2


def _reason_of(finding):
    """Pull the parenthesised reason back out of a "Cannot reach x (why)" message."""
    return finding.message.partition("(")[2].rstrip(")")


def collapse_unreachable(findings, hosts_pinged):
    """Replace many "Cannot reach ..." lines with one finding blaming the monitor.

    Two independent reasons to blame this machine:

    1. Several hosts failed *monitor-side* — a name lookup that stalled or failed.
       Resolution happens here, so it is a fact about this machine regardless of how
       many other hosts happened to answer. This fires even when some hosts are fine,
       which matters: a templogger running on the monitor itself always answers, and
       would otherwise veto the collapse forever.

    2. Every host we pinged is unreachable. Cameras do not leave a network together;
       the machine they have in common is this one.

    Keyed on MONITOR_HOST, so the two-tick confirmation treats a fleet-wide outage as
    a single issue that must persist before anyone is told about it.
    """
    unreachable = [f for f in findings if f.kind == "ping"]
    local = [f for f in unreachable if f.monitor_side]

    if len(local) >= COLLAPSE_THRESHOLD:
        detail = _reason_of(local[0])
        survivors = [f for f in findings if f not in local]
        return survivors + [Finding(
            MONITOR_HOST, "network",
            f"Monitor host could not resolve or reach {len(local)} of its devices "
            f"— its own network/resolver is at fault"
            + (f": {detail}" if detail else ""),
            monitor_side=True,
        )]

    if hosts_pinged >= COLLAPSE_THRESHOLD and len(unreachable) == hosts_pinged:
        detail = _reason_of(unreachable[0])
        survivors = [f for f in findings if f.kind != "ping"]
        return survivors + [Finding(
            MONITOR_HOST, "network",
            f"Monitor host cannot reach any of its {hosts_pinged} devices "
            f"— check its own network" + (f": {detail}" if detail else ""),
            monitor_side=True,
        )]

    return findings


def confirm(pending, found):
    """Split this tick's findings into the ones to report and the next pending set.

    A finding is confirmed once its key has been seen on two consecutive ticks, so
    a transient blip that clears by the next tick is never reported.
    """
    confirmed = [f for f in found if f.key in pending]
    return confirmed, {f.key for f in found}


def parse_heartbeat(stdout, max_age_seconds, future_tolerance_seconds=5):
    """Interpret the heartbeat probe's stdout. Return (state, age).

    state is one of "ok", "missing", "stale", "future", "unparseable"; age is the
    heartbeat's age in seconds (negative when the mtime is in the future), or None.
    """
    parts = stdout.split()
    if parts and parts[0] == "MISSING":
        return "missing", None
    if len(parts) >= 3 and parts[0] == "OK":
        try:
            mtime, now = int(parts[1]), int(parts[2])
        except ValueError:
            return "unparseable", None
        age = now - mtime
        if age > max_age_seconds:
            return "stale", age
        # A negative age means the file was touched "after" the remote clock's
        # current time, i.e. the clock jumped backwards since the last write. The
        # old `age > max_age` test passed this silently.
        if age < -future_tolerance_seconds:
            return "future", age
        return "ok", age
    return "unparseable", None
