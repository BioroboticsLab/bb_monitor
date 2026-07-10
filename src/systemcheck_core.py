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
