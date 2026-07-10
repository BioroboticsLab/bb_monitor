"""System-check loop for bb_monitor.

Runs camera/templogger/process checks on a fast cadence (default every 10 min),
silently when everything is OK and immediately on any *confirmed* issue.

An issue must be seen on two consecutive ticks before it is reported, so a
short-lived network blip that clears by the next tick never reaches Telegram. The
cost is that a real fault is announced one tick (default 10 min) later than it is
first seen. Findings are matched across ticks by a stable (host, kind) key rather
than by message text, because the text carries volatile detail (a heartbeat's age
grows every tick) and several different probes share the same wording.

The first fully clean tick after an alert posts a one-time "All systems OK"
recovery message, then the loop goes quiet again. Once per hour the loop also
emits a sanity-check summary even when there is nothing to report. Posts go to a
Telegram channel independent of the monitor image bot.

When a camera's raspicam heartbeat problem is confirmed, the loop also tries to fix
it: it SIGKILLs the remote raspicam process so systemd restarts it. See _Remediator
for the guards around that.

Confirmation and remediation state is in-memory only, so a process restart while an
issue is outstanding re-arms the two-tick counter and can miss a single recovery
message; the hourly summary still confirms all-clear within the hour.
"""
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timedelta

import src.mon as mon
from src.systemcheck_core import (
    RESOLVE_GRACE_SECONDS,
    Finding,
    HostAddresses,
    PingResult,
    PingSettings,
    SshTarget,
    clock_findings,
    clock_skew,
    collapse_unreachable,
    confirm,
    parse_heartbeat,
    parse_ping_address,
    ping_targets,
)

config = mon.get_config(
    default_module="default_config_systemcheck",
    user_module="user_config_systemcheck",
)

# Hostname -> IPv4, held for the life of the process. See HostAddresses for why a
# resolver cache cannot do this job. Every add and drop is logged, so `tmux attach`
# shows exactly what the monitor believes each camera's address to be.
_addresses = HostAddresses(
    enabled=getattr(config, "systemcheck_cache_addresses", True),
    log=lambda message: print(message, flush=True),
)


# ---------- low-level check helpers ----------

def _ping_args(timeout_seconds):
    """macOS ping -W is in milliseconds; Linux ping -W is in seconds.

    Both are clamped to at least 1 unit: Linux `ping -W 0` means "wait forever", so
    a sub-second config value would truncate to a ping that never returns against an
    unreachable host.
    """
    if platform.system() == "Darwin":
        return ["-W", str(max(1, int(timeout_seconds * 1000)))]
    return ["-W", str(max(1, int(timeout_seconds)))]


def _ping_once(target, ping, deadline):
    """One `ping -c 1 -n` at `target` (a hostname or an IP). Return a PingResult.

    `-n` is load-bearing, not cosmetic. Without it iputils does a *blocking* reverse
    PTR lookup on every reply, to print a name we never read. `-W` does not bound it
    — "the option affects only timeout in absence of any responses" (ping(8)) — so it
    is unbounded by anything except our own subprocess deadline. On this fleet that
    lookup asks for `192.168.178.x` in-addr.arpa; nss-mdns answers reverse queries
    only for 169.254.0.0/16 and returns UNAVAIL otherwise, which slips past
    `[NOTFOUND=return]` in nsswitch and falls through to unicast DNS — the university
    resolver on the default route, which drops RFC1918 PTRs. glibc then waits
    `timeout:5` × `attempts:2` (resolv.conf(5)), and ping never returns.

    That is the whole reason healthy cameras were reported unreachable. `-n` is also
    portable: BSD ping has it, and it is already the implicit default when the target
    is numeric — which is exactly why pinging a cached IP never showed the stall.

    Classifies whose fault a failure is. iputils gives us enough to tell them apart:

      exit 1, nothing on stderr  the host did not answer          -> host-side
      exit 2, "Name or service not known" / "Network is unreachable"
                                 this machine could not get there -> monitor-side
      no exit at all             a name lookup stalled            -> monitor-side

    Name resolution runs here, so a lookup that stalls or fails says nothing about
    the camera.
    """
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-n"] + _ping_args(ping.timeout_seconds) + [target],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=deadline,
        )
    except subprocess.TimeoutExpired:
        return PingResult(
            False,
            f"ping did not return within {deadline}s (name resolution stalled?)",
            monitor_side=True,
        )
    except FileNotFoundError:
        return PingResult(False, "ping binary not found", monitor_side=True)

    stdout = proc.stdout.decode(errors="replace")
    if proc.returncode == 0:
        return PingResult(True, address=parse_ping_address(stdout))
    stderr = proc.stderr.decode(errors="replace").strip().splitlines()
    # A silent exit 1 is the only outcome that means "the remote host is quiet".
    # Everything else — anything ping saw fit to complain about, any other exit
    # code — happened before a packet ever left this machine.
    monitor_side = bool(stderr) or proc.returncode != 1
    reason = stderr[-1] if stderr else f"no reply within {ping.timeout_seconds}s"
    return PingResult(False, reason, monitor_side)


def check_ping(host, ping=None, addresses=None):
    """ICMP-ping `host`, preferring its cached address. Return a PingResult.

    The cache is what keeps mDNS out of the hot path (see HostAddresses). The last
    attempt always goes by name, so a Pi on a new DHCP lease is found in the same
    tick; and if every attempt fails, the cached address is dropped so the next tick
    starts from a fresh lookup.

    Retries are spaced by `retry_delay_seconds` so the attempts outlast a hiccup on
    this machine's link rather than all landing inside it.
    """
    ping = ping or PingSettings()
    addresses = _addresses if addresses is None else addresses
    cached = addresses.get(host)
    deadline = ping.timeout_seconds + RESOLVE_GRACE_SECONDS

    result = PingResult(False, "not attempted")
    for attempt, target in enumerate(ping_targets(host, cached, ping.attempts)):
        if attempt:
            time.sleep(ping.retry_delay_seconds)
        result = _ping_once(target, ping, deadline)
        if result.ok:
            # Keep whatever ping resolved: by name this is the lookup we just paid
            # for, by IP it is the address we already had.
            addresses.remember(host, result.address)
            return result
    if cached:
        addresses.forget(host)
    return result


def _ping_settings(cfg):
    """Config overrides on top of PingSettings' own defaults, which stay the single
    source of truth for how long a dropout the retries must outlast."""
    default = PingSettings()
    return PingSettings(
        timeout_seconds=getattr(cfg, "ping_timeout_seconds", default.timeout_seconds),
        attempts=getattr(cfg, "ping_attempts", default.attempts),
        retry_delay_seconds=getattr(cfg, "ping_retry_delay_seconds", default.retry_delay_seconds),
    )


def _as_ssh_target(value):
    return value if isinstance(value, SshTarget) else SshTarget(value)


def _ssh_run(host, remote_cmd, ssh_timeout):
    """SSH to host and run remote_cmd (a shell string). Return (proc, err_msg).
    proc is None when ssh itself failed; err_msg is non-None in that case.

    `host` may be an SshTarget carrying a cached IP, which keeps mDNS out of the ssh
    path too — ssh resolves the same names over the same link, so caching only the
    ping would leave every other check stalling.
    """
    target = _as_ssh_target(host)
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={ssh_timeout}"]
    if target.host_key_alias:
        # Connecting by IP: look the host key up under the name so known_hosts still
        # matches, and so a recycled lease fails loudly instead of silently checking
        # the wrong machine.
        ssh_cmd += ["-o", f"HostKeyAlias={target.host_key_alias}"]
    ssh_cmd += [target.destination, remote_cmd]
    try:
        proc = subprocess.run(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=ssh_timeout + 5,
        )
    except subprocess.TimeoutExpired:
        _forget_cached_address(target)
        return None, f"ssh timeout after {ssh_timeout}s"
    except FileNotFoundError:
        return None, "ssh binary not found"
    if proc.returncode == 255:
        # Transport failure against a cached address (wrong host, moved lease, bad
        # host key). Drop it so the next tick resolves the name again.
        _forget_cached_address(target)
    return proc, None


def _forget_cached_address(target):
    """Only a target built from the cache carries an alias, so this is a no-op for
    hosts we reached by name."""
    if target.host_key_alias:
        _addresses.forget(target.host_key_alias)


def _ssh_failed(proc, stdout):
    """SSH exit 255 (or any non-zero exit with empty stdout) means SSH itself or
    the remote shell never got to the real command. Caller should surface this
    as an SSH failure, not a domain-level failure.

    The "non-zero exit with empty stdout" half of that is a heuristic, and remote
    commands that can legitimately fail must not rely on it: write them to always
    exit 0 and always print something, so a domain failure can never be mistaken
    for a transport failure. check_remote_heartbeat does exactly that.
    """
    return proc.returncode == 255 or (proc.returncode != 0 and not stdout)


def check_remote_process(host, command, match_substring, min_count, ssh_timeout=30, ssh_target=None):
    """SSH to host, run command, count substring in stdout. Return (ok, msg)."""
    target = ssh_target if ssh_target is not None else host
    remote_cmd = " ".join(command) if isinstance(command, (list, tuple)) else command
    proc, err = _ssh_run(target, remote_cmd, ssh_timeout)
    if proc is None:
        return False, f"{host}: {err}"

    stdout = proc.stdout.decode(errors="replace")
    stderr = proc.stderr.decode(errors="replace").strip()

    if _ssh_failed(proc, stdout):
        return False, f"{host}: ssh exec failed ({stderr or f'exit {proc.returncode}'})"

    count = stdout.count(match_substring)
    if count < min_count:
        return False, f"{host}: '{match_substring}' count={count}, expected >={min_count}"
    return True, None


def check_remote_service(host, service, ssh_timeout=30, ssh_target=None):
    """`systemctl is-active <service>` on the remote host. Return (ok, msg)."""
    target = ssh_target if ssh_target is not None else host
    proc, err = _ssh_run(target, f"systemctl is-active {service}", ssh_timeout)
    if proc is None:
        return False, f"{host}: {err}"
    state = proc.stdout.decode(errors="replace").strip()
    stderr = proc.stderr.decode(errors="replace").strip()
    if state == "active":
        return True, None
    if _ssh_failed(proc, state):
        return False, f"{host}: ssh exec failed ({stderr or f'exit {proc.returncode}'})"
    return False, f"{host}: {service} not active (state={state})"


def _heartbeat_probe_cmd(path):
    """Remote command for the heartbeat probe.

    It must always exit 0 and always print a line. `_ssh_failed` reads "non-zero
    exit + empty stdout" as a transport failure, and that is exactly what the
    previous `stat -c %Y -- {path} && date +%s` produced for a missing file: stat
    exits 1, the `&&` short-circuits so nothing is printed, and a missing heartbeat
    got reported as `ssh exec failed (stat: cannot statx ...)`.

    `p=` is unquoted so a leading ~ still expands, as it did when this was a bare
    `stat -c %Y -- {path}`; every later use quotes it.
    """
    return (
        f"p={path}; now=$(date +%s); "
        'if [ -e "$p" ]; then m=$(stat -c %Y -- "$p"); echo "OK $m $now"; '
        'else echo "MISSING $now"; fi'
    )


def check_remote_heartbeat(host, path, max_age_seconds, ssh_timeout=30, ssh_target=None):
    """Freshness of a heartbeat file on the remote host. Return (ok, msg, state).

    Both timestamps come from the remote shell, so a constant client/server clock
    offset cancels out (a clock *step* between the last write and this read does
    not — that surfaces as "stale" or "future").
    """
    target = ssh_target if ssh_target is not None else host
    proc, err = _ssh_run(target, _heartbeat_probe_cmd(path), ssh_timeout)
    if proc is None:
        return False, f"{host}: {err}", "error"
    stdout = proc.stdout.decode(errors="replace").strip()
    stderr = proc.stderr.decode(errors="replace").strip()
    if _ssh_failed(proc, stdout):
        return False, f"{host}: ssh exec failed ({stderr or f'exit {proc.returncode}'})", "error"

    state, age = parse_heartbeat(stdout, max_age_seconds)
    if state == "ok":
        return True, None, state
    if state == "missing":
        return False, f"{host}: {path} missing (raspicam not writing heartbeat)", state
    if state == "stale":
        return False, f"{host}: {path} stale ({age}s old, max {max_age_seconds}s)", state
    if state == "future":
        return False, f"{host}: {path} mtime is {-age}s in the future (clock stepped?)", state
    return False, f"{host}: could not parse heartbeat probe output for {path}: {stdout!r}", state


def check_remote_clock(host, ssh_timeout=30, ssh_target=None):
    """Read the remote clock. Return (skew, offset) as defined by clock_skew(), or
    None when the host could not be reached — its other SSH checks will report that.
    """
    target = ssh_target if ssh_target is not None else host
    t0 = time.time()
    proc, err = _ssh_run(target, "date +%s", ssh_timeout)
    t1 = time.time()
    if proc is None:
        return None
    stdout = proc.stdout.decode(errors="replace").strip()
    if _ssh_failed(proc, stdout):
        return None
    try:
        remote_epoch = int(stdout.split()[0])
    except (IndexError, ValueError):
        return None
    return clock_skew(t0, remote_epoch, t1)


def check_remote_csv_freshness(host, glob_pattern, max_age_seconds, ssh_timeout=30, ssh_target=None):
    """Find newest CSV matching glob, read leading ISO timestamp on last non-empty
    data row, compare to remote `date +%s`. Returns (ok, msg).
    """
    target = ssh_target if ssh_target is not None else host
    # Pipeline (single remote shell):
    #   - newest matching file
    #   - last non-empty, non-header line (looking only at the tail to avoid
    #     grep falling back to "binary file matches" if a NUL byte got written
    #     somewhere earlier in the file; -a also forces text mode)
    #   - leading ISO timestamp -> epoch
    #   - print "<epoch> <now>" so we can diff client-side
    remote_cmd = (
        f"f=$(ls -1t {glob_pattern} 2>/dev/null | head -n1); "
        "if [ -z \"$f\" ]; then echo NO_FILE; exit 1; fi; "
        "last=$(tail -n 50 \"$f\" 2>/dev/null | grep -av '^Time' | grep -av '^[[:space:]]*$' | tail -n1); "
        "if [ -z \"$last\" ]; then echo NO_ROWS; exit 1; fi; "
        "ts=$(echo \"$last\" | cut -d, -f1); "
        # date -d understands the ISO 8601 timestamps the loggers write.
        "epoch=$(date -d \"$ts\" +%s 2>/dev/null); "
        "if [ -z \"$epoch\" ]; then echo BAD_TS \"$ts\"; exit 1; fi; "
        "echo \"$epoch $(date +%s) $f\""
    )
    proc, err = _ssh_run(target, remote_cmd, ssh_timeout)
    if proc is None:
        return False, f"{host}: {err}"
    out = proc.stdout.decode(errors="replace").strip()
    stderr = proc.stderr.decode(errors="replace").strip()
    if _ssh_failed(proc, out):
        return False, f"{host}: ssh exec failed ({stderr or f'exit {proc.returncode}'})"
    if proc.returncode != 0:
        return False, f"{host}: csv-freshness {glob_pattern}: {out or 'failed'}"
    parts = out.split()
    try:
        last_ts = int(parts[0])
        now = int(parts[1])
    except (IndexError, ValueError):
        return False, f"{host}: csv-freshness unexpected output: {out!r}"
    age = now - last_ts
    if age > max_age_seconds:
        path = parts[2] if len(parts) > 2 else glob_pattern
        return False, f"{host}: {path} last row {age}s old (max {max_age_seconds}s)"
    return True, None


def check_remote_file_count(host, list_command, max_files, ssh_timeout=30, ssh_target=None):
    """Run list_command on the remote host; it must print an integer file count
    (e.g. `find <dir>/ -mindepth 2 -maxdepth 2 -type f 2>/dev/null | wc -l`). Warn
    when count > max_files — for bb_imgacquisition's out/ dir a growing count means
    the file transfer is backing up. Return (ok, msg).
    """
    target = ssh_target if ssh_target is not None else host
    proc, err = _ssh_run(target, list_command, ssh_timeout)
    if proc is None:
        return False, f"{host}: {err}"
    out = proc.stdout.decode(errors="replace").strip()
    stderr = proc.stderr.decode(errors="replace").strip()
    if _ssh_failed(proc, out):
        return False, f"{host}: ssh exec failed ({stderr or f'exit {proc.returncode}'})"
    try:
        count = int(out.split()[0])
    except (IndexError, ValueError):
        return False, f"{host}: file-count unexpected output: {out!r}"
    if count > max_files:
        return False, f"{host}: {count} files awaiting transfer (warn >{max_files})"
    return True, None


# ---------- clock fan-out ----------

class _ClockCollector:
    """Samples every SSH-reachable host's clock, then turns the samples into findings.

    Deferring the findings until all hosts are sampled lets clock_findings() tell
    "one device drifted" apart from "our own clock is wrong", which otherwise fans
    out into one alert per device.

    A transport failure produces no finding — every host we probe also runs at least
    one domain check over SSH, which surfaces the failure under its own key.
    """

    def __init__(self, max_skew_seconds, ssh_timeout):
        self.max_skew_seconds = max_skew_seconds
        self.ssh_timeout = ssh_timeout
        self._samples = []      # (host, skew, offset)
        self._probed = set()

    def probe(self, host, ssh_target):
        if host in self._probed:  # a host can appear in two config lists
            return
        self._probed.add(host)
        sample = check_remote_clock(host, ssh_timeout=self.ssh_timeout, ssh_target=ssh_target)
        if sample is not None:
            skew, offset = sample
            self._samples.append((host, skew, offset))

    def findings(self):
        return clock_findings(self._samples, self.max_skew_seconds)


# ---------- bundled per-host check sets ----------

# Defaults applied to each camera entry; per-entry keys override.
# ssh_user prefixes the hostname for SSH (e.g. "pi@feedercama.local"); set to
# None or "" per-entry to ssh as the local user.
_FEEDERCAM_DEFAULTS = {
    "ssh_user": "pi",
    "raspicam_heartbeat": "/tmp/raspicam_heartbeat",
    "raspicam_max_age_seconds": 30,
    "scale_csv_glob": "~/bb_mini_scales/data/weight_data_*.csv",
    "scale_max_age_seconds": 30,
}
_EXITCAM_DEFAULTS = {
    "ssh_user": "pi",
    "raspicam_heartbeat": "/tmp/raspicam_heartbeat",
    "raspicam_max_age_seconds": 30,
}

RASPICAM_SERVICE = "raspicam.service"


def _ssh_target_for(host, user, addresses=None):
    """Prefer the cached IP, but keep the hostname for the host-key lookup."""
    addresses = _addresses if addresses is None else addresses
    address = addresses.get(host)
    destination = address or host
    if user:
        destination = f"{user}@{destination}"
    return SshTarget(destination, host if address else None)


def _unreachable(host, result):
    message = f"Cannot reach {host} ({result.reason})" if result.reason else f"Cannot reach {host}"
    return Finding(host, "ping", message,
                   monitor_side=result.monitor_side, reason=result.reason)


def _camera_checks(cam, ping, ssh_timeout, clock):
    """Run the bundle of checks appropriate for `cam`. Returns list of Findings."""
    host = cam["hostname"]
    cam_type = cam.get("type")
    findings = []

    reachable = check_ping(host, ping)
    if not reachable.ok:
        # Skip the rest — SSH-based checks would all just time out.
        return [_unreachable(host, reachable)]

    if cam_type == "feedercam":
        merged = {**_FEEDERCAM_DEFAULTS, **cam}
        service_checks = [
            (RASPICAM_SERVICE, merged["raspicam_heartbeat"], merged["raspicam_max_age_seconds"]),
            ("imgstorage.service", None, None),
            ("mini_scale_logger.service", None, None),
        ]
        scale_glob = merged["scale_csv_glob"]
        scale_age = merged["scale_max_age_seconds"]
    elif cam_type == "exitcam":
        merged = {**_EXITCAM_DEFAULTS, **cam}
        service_checks = [
            (RASPICAM_SERVICE, merged["raspicam_heartbeat"], merged["raspicam_max_age_seconds"]),
            ("imgstorage.service", None, None),
        ]
        scale_glob = None
        scale_age = None
    else:
        return [Finding(host, "config", f"{host}: unknown camera type {cam_type!r}")]

    ssh_target = _ssh_target_for(host, merged.get("ssh_user"))
    clock.probe(host, ssh_target)

    for service, heartbeat_path, heartbeat_age in service_checks:
        ok, msg = check_remote_service(
            host, service, ssh_timeout=ssh_timeout, ssh_target=ssh_target,
        )
        if not ok:
            findings.append(Finding(host, f"svc:{service}", msg))
            continue
        if heartbeat_path is not None:
            ok, msg, state = check_remote_heartbeat(
                host, heartbeat_path, heartbeat_age,
                ssh_timeout=ssh_timeout, ssh_target=ssh_target,
            )
            if not ok:
                # "future" is a clock artefact, not a wedged camera: restarting it
                # would not help and the clock check reports the real problem.
                findings.append(Finding(
                    host, "heartbeat", msg,
                    remediable=state in ("missing", "stale"),
                    ssh_target=ssh_target,
                ))

    if scale_glob is not None:
        ok, msg = check_remote_csv_freshness(
            host, scale_glob, scale_age,
            ssh_timeout=ssh_timeout, ssh_target=ssh_target,
        )
        if not ok:
            findings.append(Finding(host, f"csv:{scale_glob}", msg))

    return findings


def _templogger_checks(entry, ping, ssh_timeout, clock):
    host = entry["hostname"]
    reachable = check_ping(host, ping)
    if not reachable.ok:
        return [_unreachable(host, reachable)]
    findings = []
    ssh_target = _ssh_target_for(host, entry.get("ssh_user"))
    clock.probe(host, ssh_target)
    service = entry.get("service", "temperaturelogger.service")
    ok, msg = check_remote_service(host, service, ssh_timeout=ssh_timeout, ssh_target=ssh_target)
    if not ok:
        findings.append(Finding(host, f"svc:{service}", msg))
    glob_pattern = entry["csv_glob"]
    max_age = entry.get("max_age_seconds", 60)
    ok, msg = check_remote_csv_freshness(
        host, glob_pattern, max_age, ssh_timeout=ssh_timeout, ssh_target=ssh_target,
    )
    if not ok:
        findings.append(Finding(host, f"csv:{glob_pattern}", msg))
    return findings


def _transfer_checks(entry, ping, ssh_timeout, clock):
    """Count files awaiting transfer under the host's bb_imgacquisition out/ dir.
    No ping pre-check (these are wired servers, like systemcheck_process_hosts);
    `ping` is accepted only for signature symmetry with the other runners.
    """
    host = entry["hostname"]
    ssh_target = _ssh_target_for(host, entry.get("ssh_user"))
    clock.probe(host, ssh_target)
    max_files = entry.get("num_files_to_warn", 60)
    command = entry.get("command")
    if command is None:
        directory = entry["directory"].rstrip("/")
        command = f"find {directory}/ -mindepth 2 -maxdepth 2 -type f 2>/dev/null | wc -l"
    ok, msg = check_remote_file_count(
        host, command, max_files, ssh_timeout=ssh_timeout, ssh_target=ssh_target,
    )
    return [] if ok else [Finding(host, "transfer", msg)]


# ---------- remediation ----------

# Kill the wedged raspicam so systemd restarts it. Every piece is load-bearing:
#
#   [r]aspicam\.py  the regex matches "raspicam.py", but the remote login shell's own
#                   /proc/pid/cmdline holds the literal brackets, so `pkill -f` cannot
#                   kill the SSH session it is running in. pkill excludes itself, but
#                   not its parent.
#   -u "$uid"       scope to the SSH user, who owns the process — so no sudo. (It is
#                   the pattern, not the uid, that spares imgstorage.py: that also
#                   runs as pi.)
#   -KILL           systemd counts SIGTERM as a *clean* exit, so `Restart=on-failure`
#                   (what setup_autostart.sh deployed for years) would not restart a
#                   SIGTERMed unit. SIGKILL restarts under both policies. raspicam.py
#                   installs no signal handler, so nothing graceful is lost.
#   wc -l, exit 0   `pkill` exits 1 when nothing matched; a non-zero exit with empty
#                   stdout is exactly what _ssh_failed reads as a transport error.
_REMEDIATION_CMD = (
    r"""pat='[r]aspicam\.py'; uid=$(id -u); """
    r"""n=$(pgrep -u "$uid" -f "$pat" 2>/dev/null | wc -l | tr -d ' '); """
    r"""pkill -KILL -u "$uid" -f "$pat" 2>/dev/null; """
    r"""echo "killed $n"; exit 0"""
)


def kill_remote_raspicam(host, ssh_target, ssh_timeout=30):
    """SIGKILL the remote raspicam process(es). Return (ok, detail).

    Deliberately does not go through _ssh_failed: only ssh's own 255 counts as a
    failure here.
    """
    target = ssh_target if ssh_target is not None else host
    proc, err = _ssh_run(target, _REMEDIATION_CMD, ssh_timeout)
    if proc is None:
        return False, err
    stdout = proc.stdout.decode(errors="replace").strip()
    stderr = proc.stderr.decode(errors="replace").strip()
    if proc.returncode == 255:
        return False, stderr or "ssh exit 255"
    if not stdout.startswith("killed"):
        return False, stderr or f"unexpected output {stdout!r}"
    return True, stdout


class _Remediator:
    """Restarts wedged raspicams, with the guards that keep it from doing harm.

    Only fires on a *confirmed* heartbeat finding (two consecutive ticks), never
    when the service is stopped (that means a human stopped it), at most once per
    cooldown per host, and at most max_attempts times before it gives up and says
    so — a Pi running a raspicam build that predates the heartbeat will never
    produce one no matter how often it is restarted.

    State is in-memory, like the confirmation counter: a monitor restart re-arms
    both, which is the conservative direction (it delays a kill, never repeats one).
    """

    def __init__(self, cfg):
        self.enabled = getattr(cfg, "systemcheck_remediation_enabled", True)
        self.cooldown_seconds = 60 * getattr(cfg, "systemcheck_remediation_cooldown_minutes", 60)
        self.max_attempts = getattr(cfg, "systemcheck_remediation_max_attempts", 3)
        self.hourly_only = getattr(cfg, "systemcheck_remediation_hourly_only", False)
        self.ssh_timeout = getattr(cfg, "ssh_timeout_seconds", 30)
        self._attempts = {}       # host -> attempts since the heartbeat last recovered
        self._last_attempt = {}   # host -> time.monotonic() of the last kill

    def forget_recovered(self, found_keys):
        """Clear a host's attempt budget once its heartbeat check passes again."""
        for host in [h for h in self._attempts if (h, "heartbeat") not in found_keys]:
            del self._attempts[host]
            self._last_attempt.pop(host, None)

    def run(self, confirmed, is_hourly_tick):
        """Attempt fixes for confirmed findings. Returns extra lines for the alert."""
        if not self.enabled:
            return []
        if self.hourly_only and not is_hourly_tick:
            return []

        lines = []
        now = time.monotonic()
        for finding in confirmed:
            if finding.kind != "heartbeat" or not finding.remediable:
                continue

            attempts = self._attempts.get(finding.host, 0)
            if attempts >= self.max_attempts:
                lines.append(
                    f"- {finding.host}: auto-restart did not help after {attempts} attempts "
                    f"— check that bb_raspicam is up to date on this Pi"
                )
                continue
            last = self._last_attempt.get(finding.host)
            if last is not None and now - last < self.cooldown_seconds:
                continue

            # A camera whose service is stopped never produces a heartbeat finding
            # (the probe is skipped), so reaching here normally means the service is
            # up. But the probe ran a few SSH round trips ago; re-check immediately
            # before pulling the trigger, in case someone has stopped the service in
            # between to work on the camera. Costs one SSH, only on the fix path.
            active, _ = check_remote_service(
                finding.host, RASPICAM_SERVICE,
                ssh_timeout=self.ssh_timeout, ssh_target=finding.ssh_target,
            )
            if not active:
                lines.append(
                    f"- {finding.host}: {RASPICAM_SERVICE} is stopped; skipping auto-restart"
                )
                continue

            ok, detail = kill_remote_raspicam(
                finding.host, finding.ssh_target, ssh_timeout=self.ssh_timeout,
            )
            self._attempts[finding.host] = attempts + 1
            self._last_attempt[finding.host] = now
            if ok:
                lines.append(f"- ↻ restarted raspicam on {finding.host} ({detail})")
            else:
                lines.append(f"- {finding.host}: auto-restart failed ({detail})")
        return lines


# ---------- top-level run ----------

def run_checks():
    findings = []
    ping = _ping_settings(config)
    ssh_timeout = getattr(config, "ssh_timeout_seconds", 30)
    max_skew = getattr(config, "systemcheck_max_clock_skew_seconds", 60)
    clock = _ClockCollector(max_skew, ssh_timeout)

    # Cameras and temploggers are ping-gated inside their runners; count them here so
    # collapse_unreachable() can tell "some hosts are down" from "we are down".
    hosts_pinged = (len(getattr(config, "systemcheck_ping_hosts", []))
                    + len(getattr(config, "systemcheck_cameras", []))
                    + len(getattr(config, "systemcheck_temploggers", [])))

    for host in getattr(config, "systemcheck_ping_hosts", []):
        reachable = check_ping(host, ping)
        if not reachable.ok:
            findings.append(_unreachable(host, reachable))

    for spec in getattr(config, "systemcheck_process_hosts", []):
        ssh_target = _ssh_target_for(spec["hostname"], spec.get("ssh_user"))
        clock.probe(spec["hostname"], ssh_target)
        ok, msg = check_remote_process(
            spec["hostname"],
            spec["command"],
            spec["match_substring"],
            spec["min_count"],
            ssh_timeout=ssh_timeout,
            ssh_target=ssh_target,
        )
        if not ok:
            findings.append(Finding(spec["hostname"], f"proc:{spec['match_substring']}", msg))

    for cam in getattr(config, "systemcheck_cameras", []):
        findings.extend(_camera_checks(cam, ping, ssh_timeout, clock))

    for entry in getattr(config, "systemcheck_temploggers", []):
        findings.extend(_templogger_checks(entry, ping, ssh_timeout, clock))

    for entry in getattr(config, "systemcheck_transfer_hosts", []):
        findings.extend(_transfer_checks(entry, ping, ssh_timeout, clock))

    findings.extend(clock.findings())
    return collapse_unreachable(findings, hosts_pinged)


def _warn_if_ping_budget_overruns_tick(fast_minutes):
    """A fleet-wide outage pings every host to exhaustion. Make sure that still fits
    inside one tick, or the loop silently falls behind its own schedule."""
    ping = _ping_settings(config)
    hosts = (len(getattr(config, "systemcheck_ping_hosts", []))
             + len(getattr(config, "systemcheck_cameras", []))
             + len(getattr(config, "systemcheck_temploggers", [])))
    budget = hosts * ping.worst_case_seconds()
    if budget > fast_minutes * 60:
        print(f"WARNING: with every host down, pinging alone would take ~{budget:.0f}s, "
              f"longer than the {fast_minutes}-minute check interval. Lower "
              f"ping_attempts or ping_retry_delay_seconds.", flush=True)


def _notify(text):
    """Send a Telegram message; return True only on a confirmed successful send.
    Collapses both a raised exception and an `ok == False` API response to False.
    """
    try:
        return bool(mon.send_message(config, text))
    except Exception as e:
        print(f"Failed to send Telegram message: {e}", flush=True)
        return False


def _trigger_monitor_images():
    """Spawn the monitor bot once per configured monitor config to push a fresh
    image to each monitor's own Telegram feed. Used on recovery for visual
    confirmation. Runs each send as an isolated subprocess with a timeout so a
    hung video read (e.g. stale NFS) can never stall the system-check loop.
    """
    paths = getattr(config, "systemcheck_trigger_monitor_configs", [])
    if not paths:
        return
    timeout = getattr(config, "systemcheck_trigger_timeout_seconds", 60)
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    monitor_script = os.path.join(repo_dir, "bb_monitor.py")
    child_env = {**os.environ, "BB_MONITOR_ONCE": "1"}
    for cfg_path in paths:
        try:
            subprocess.run(
                [sys.executable, monitor_script, cfg_path],  # same venv via sys.executable
                cwd=repo_dir,                                # so `import src.mon` resolves
                env=child_env,
                timeout=timeout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            print(f"Failed to trigger monitor image for {cfg_path}: {e}", flush=True)


def main():
    print("Starting bb_monitor_systemcheck...")
    fast_minutes = max(1, int(getattr(config, "systemcheck_fast_interval_minutes", 10)))
    _warn_if_ping_budget_overruns_tick(fast_minutes)
    remediator = _Remediator(config)
    pending = set()          # keys seen on the previous tick, not yet reported
    alerted = False          # an alert was sent and has not been recovered from
    while True:
        loop_start = datetime.now()
        found = run_checks()
        confirmed, found_keys = confirm(pending, found)
        pending = found_keys        # this tick's findings gate the next tick's alerts
        remediator.forget_recovered(found_keys)
        # The first fast tick of every hour also emits the "all OK" sanity ping.
        is_hourly_tick = loop_start.minute < fast_minutes

        if confirmed:
            lines = [f"- {f.message}" for f in confirmed]
            lines += remediator.run(confirmed, is_hourly_tick)
            _notify("Issues found:\n" + "\n".join(lines))
            # Set unconditionally: the system did have issues even if the alert
            # couldn't be delivered, so the next clean tick should still recover.
            alerted = True
        elif not found_keys and (alerted or is_hourly_tick):
            # Recovery ("an alert is outstanding") and the hourly sanity ping send
            # the same text, so they share one branch — and coincide as a single send.
            #
            # Recovery demands a *completely* clean tick, not merely no confirmed
            # findings. A finding vanishing from `found` does not prove it was fixed:
            # _camera_checks returns early when ping fails, so a transient blip hides
            # a wedged camera's heartbeat finding and would otherwise be read as
            # "recovered".
            is_recovery = alerted
            if _notify("All systems OK"):
                # Clear only on a confirmed send so a failed recovery is retried next
                # tick; when already False (plain hourly tick) this is a safe no-op.
                alerted = False
                # On a genuine recovery (not a plain hourly OK), push a fresh monitor
                # image so the cameras can be visually confirmed back. Gating on the
                # confirmed send means exactly one image per error->clear edge.
                if is_recovery:
                    _trigger_monitor_images()
        elif is_hourly_tick:
            # Unconfirmed findings only. Keep the hourly liveness ping, but don't
            # claim everything is fine — and don't clear `alerted`.
            unconfirmed = "\n".join(f"- {f.message}" for f in found)
            _notify(f"No confirmed issues; {len(found)} awaiting confirmation:\n{unconfirmed}")
        else:
            state = f"{len(found)} unconfirmed" if found else "all OK"
            print(f"[{loop_start.isoformat(timespec='seconds')}] {state} (silent)", flush=True)

        # Sleep until the next snap minute (a multiple of fast_minutes since midnight).
        midnight = loop_start.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_min = int((loop_start - midnight).total_seconds() // 60)
        next_idx = (elapsed_min // fast_minutes + 1) * fast_minutes
        nxt = midnight + timedelta(minutes=next_idx)
        sleep_secs = (nxt - datetime.now()).total_seconds()
        time.sleep(max(60, sleep_secs))


if __name__ == "__main__":
    main()
