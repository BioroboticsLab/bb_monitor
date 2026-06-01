"""System-check loop for bb_monitor.

Runs camera/templogger/process checks on a fast cadence (default every 10 min),
silently when everything is OK and immediately on any issue. Once per hour the
loop also emits a sanity-check "all systems OK" summary even when there is
nothing to report. Posts go to a Telegram channel independent of the monitor
image bot.
"""
import platform
import subprocess
import time
from datetime import datetime, timedelta

import src.mon as mon

config = mon.get_config(
    default_module="default_config_systemcheck",
    user_module="user_config_systemcheck",
)


# ---------- low-level check helpers ----------

def _ping_args(timeout_seconds):
    """macOS ping -W is in milliseconds; Linux ping -W is in seconds."""
    if platform.system() == "Darwin":
        return ["-W", str(int(timeout_seconds * 1000))]
    return ["-W", str(int(timeout_seconds))]


def check_ping(host, timeout_seconds=2):
    try:
        subprocess.run(
            ["ping", "-c", "1"] + _ping_args(timeout_seconds) + [host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=timeout_seconds + 2,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _ssh_run(host, remote_cmd, ssh_timeout):
    """SSH to host and run remote_cmd (a shell string). Return (proc, err_msg).
    proc is None when ssh itself failed; err_msg is non-None in that case.
    """
    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={ssh_timeout}",
        host,
        remote_cmd,
    ]
    try:
        proc = subprocess.run(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=ssh_timeout + 5,
        )
        return proc, None
    except subprocess.TimeoutExpired:
        return None, f"ssh timeout after {ssh_timeout}s"
    except FileNotFoundError:
        return None, "ssh binary not found"


def check_remote_process(host, command, match_substring, min_count, ssh_timeout=30):
    """SSH to host, run command, count substring in stdout. Return (ok, msg)."""
    remote_cmd = " ".join(command) if isinstance(command, (list, tuple)) else command
    proc, err = _ssh_run(host, remote_cmd, ssh_timeout)
    if proc is None:
        return False, f"{host}: {err}"

    stdout = proc.stdout.decode(errors="replace")
    stderr = proc.stderr.decode(errors="replace").strip()

    if proc.returncode != 0 and not stdout:
        return False, f"{host}: '{remote_cmd}' failed ({stderr or f'exit {proc.returncode}'})"

    count = stdout.count(match_substring)
    if count < min_count:
        return False, f"{host}: '{match_substring}' count={count}, expected >={min_count}"
    return True, None


def check_remote_service(host, service, ssh_timeout=30):
    """`systemctl is-active <service>` on the remote host. Return (ok, msg)."""
    proc, err = _ssh_run(host, f"systemctl is-active {service}", ssh_timeout)
    if proc is None:
        return False, f"{host}: {err}"
    state = proc.stdout.decode(errors="replace").strip()
    if state == "active":
        return True, None
    return False, f"{host}: {service} not active (state={state or 'unknown'})"


def check_remote_file_mtime(host, path, max_age_seconds, ssh_timeout=30):
    """Compare remote file mtime against remote `date +%s` to avoid clock-skew."""
    # `--` lets paths starting with - work; both timestamps come from the remote shell.
    remote_cmd = f"stat -c %Y -- {path} && date +%s"
    proc, err = _ssh_run(host, remote_cmd, ssh_timeout)
    if proc is None:
        return False, f"{host}: {err}"
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        return False, f"{host}: stat {path} failed ({stderr or f'exit {proc.returncode}'})"
    lines = proc.stdout.decode(errors="replace").split()
    try:
        mtime = int(lines[0])
        now = int(lines[1])
    except (IndexError, ValueError):
        return False, f"{host}: could not parse stat output for {path}"
    age = now - mtime
    if age > max_age_seconds:
        return False, f"{host}: {path} stale ({age}s old, max {max_age_seconds}s)"
    return True, None


def check_remote_csv_freshness(host, glob_pattern, max_age_seconds, ssh_timeout=30):
    """Find newest CSV matching glob, read leading ISO timestamp on last non-empty
    data row, compare to remote `date +%s`. Returns (ok, msg).
    """
    # Pipeline (single remote shell):
    #   - newest matching file
    #   - last non-empty, non-header line
    #   - leading ISO timestamp -> epoch
    #   - print "<epoch> <now>" so we can diff client-side
    remote_cmd = (
        f"f=$(ls -1t {glob_pattern} 2>/dev/null | head -n1); "
        "if [ -z \"$f\" ]; then echo NO_FILE; exit 1; fi; "
        # last non-empty line that doesn't start with 'Time' (the CSV header)
        "last=$(grep -v '^[[:space:]]*$' \"$f\" | grep -v '^Time' | tail -n1); "
        "if [ -z \"$last\" ]; then echo NO_ROWS; exit 1; fi; "
        "ts=$(echo \"$last\" | cut -d, -f1); "
        # date -d understands the ISO 8601 timestamps the loggers write.
        "epoch=$(date -d \"$ts\" +%s 2>/dev/null); "
        "if [ -z \"$epoch\" ]; then echo BAD_TS \"$ts\"; exit 1; fi; "
        "echo \"$epoch $(date +%s) $f\""
    )
    proc, err = _ssh_run(host, remote_cmd, ssh_timeout)
    if proc is None:
        return False, f"{host}: {err}"
    out = proc.stdout.decode(errors="replace").strip()
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


# ---------- bundled per-host check sets ----------

# Defaults applied to each camera entry; per-entry keys override.
_FEEDERCAM_DEFAULTS = {
    "raspicam_heartbeat": "/tmp/raspicam_heartbeat",
    "raspicam_max_age_seconds": 30,
    "scale_csv_glob": "~/bb_mini_scales/data/weight_data_*.csv",
    "scale_max_age_seconds": 30,
}
_EXITCAM_DEFAULTS = {
    "raspicam_heartbeat": "/tmp/raspicam_heartbeat",
    "raspicam_max_age_seconds": 30,
}


def _camera_checks(cam, ping_timeout, ssh_timeout):
    """Run the bundle of checks appropriate for `cam`. Returns list of issue strings."""
    host = cam["hostname"]
    cam_type = cam.get("type")
    issues = []

    if not check_ping(host, timeout_seconds=ping_timeout):
        # Skip the rest — SSH-based checks would all just time out.
        return [f"Cannot reach {host}"]

    if cam_type == "feedercam":
        merged = {**_FEEDERCAM_DEFAULTS, **cam}
        service_checks = [
            ("raspicam.service", merged["raspicam_heartbeat"], merged["raspicam_max_age_seconds"]),
            ("imgstorage.service", None, None),
            ("mini_scale_logger.service", None, None),
        ]
        scale_glob = merged["scale_csv_glob"]
        scale_age = merged["scale_max_age_seconds"]
    elif cam_type == "exitcam":
        merged = {**_EXITCAM_DEFAULTS, **cam}
        service_checks = [
            ("raspicam.service", merged["raspicam_heartbeat"], merged["raspicam_max_age_seconds"]),
            ("imgstorage.service", None, None),
        ]
        scale_glob = None
        scale_age = None
    else:
        return [f"{host}: unknown camera type {cam_type!r}"]

    for service, heartbeat_path, heartbeat_age in service_checks:
        ok, msg = check_remote_service(host, service, ssh_timeout=ssh_timeout)
        if not ok:
            issues.append(msg)
            continue
        if heartbeat_path is not None:
            ok, msg = check_remote_file_mtime(
                host, heartbeat_path, heartbeat_age, ssh_timeout=ssh_timeout,
            )
            if not ok:
                issues.append(msg)

    if scale_glob is not None:
        ok, msg = check_remote_csv_freshness(
            host, scale_glob, scale_age, ssh_timeout=ssh_timeout,
        )
        if not ok:
            issues.append(msg)

    return issues


def _templogger_checks(entry, ping_timeout, ssh_timeout):
    host = entry["hostname"]
    if not check_ping(host, timeout_seconds=ping_timeout):
        return [f"Cannot reach {host}"]
    issues = []
    service = entry.get("service", "temperaturelogger.service")
    ok, msg = check_remote_service(host, service, ssh_timeout=ssh_timeout)
    if not ok:
        issues.append(msg)
    glob_pattern = entry["csv_glob"]
    max_age = entry.get("max_age_seconds", 60)
    ok, msg = check_remote_csv_freshness(host, glob_pattern, max_age, ssh_timeout=ssh_timeout)
    if not ok:
        issues.append(msg)
    return issues


# ---------- top-level run ----------

def run_checks():
    issues = []
    ping_timeout = getattr(config, "ping_timeout_seconds", 2)
    ssh_timeout = getattr(config, "ssh_timeout_seconds", 30)

    for host in getattr(config, "systemcheck_ping_hosts", []):
        if not check_ping(host, timeout_seconds=ping_timeout):
            issues.append(f"Cannot reach {host}")

    for spec in getattr(config, "systemcheck_process_hosts", []):
        ok, msg = check_remote_process(
            spec["hostname"],
            spec["command"],
            spec["match_substring"],
            spec["min_count"],
            ssh_timeout=ssh_timeout,
        )
        if not ok:
            issues.append(msg)

    for cam in getattr(config, "systemcheck_cameras", []):
        issues.extend(_camera_checks(cam, ping_timeout, ssh_timeout))

    for entry in getattr(config, "systemcheck_temploggers", []):
        issues.extend(_templogger_checks(entry, ping_timeout, ssh_timeout))

    return issues


def main():
    print("Starting bb_monitor_systemcheck...")
    fast_minutes = max(1, int(getattr(config, "systemcheck_fast_interval_minutes", 10)))
    while True:
        loop_start = datetime.now()
        issues = run_checks()
        # The first fast tick of every hour also emits the "all OK" sanity ping.
        is_hourly_tick = loop_start.minute < fast_minutes

        if issues:
            text = "Issues found:\n" + "\n".join(f"- {i}" for i in issues)
            try:
                mon.send_message(config, text)
            except Exception as e:
                print(f"Failed to send Telegram message: {e}", flush=True)
        elif is_hourly_tick:
            try:
                mon.send_message(config, "All systems OK")
            except Exception as e:
                print(f"Failed to send Telegram message: {e}", flush=True)
        else:
            print(f"[{loop_start.isoformat(timespec='seconds')}] all OK (silent)", flush=True)

        # Sleep until the next snap minute (a multiple of fast_minutes since midnight).
        midnight = loop_start.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_min = int((loop_start - midnight).total_seconds() // 60)
        next_idx = (elapsed_min // fast_minutes + 1) * fast_minutes
        nxt = midnight + timedelta(minutes=next_idx)
        sleep_secs = (nxt - datetime.now()).total_seconds()
        time.sleep(max(60, sleep_secs))


if __name__ == "__main__":
    main()
