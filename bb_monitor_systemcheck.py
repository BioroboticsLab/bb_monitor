"""System-check loop for bb_monitor.

Pings reachability hosts and runs SSH process checks on compute hosts; posts a
single Telegram message per iteration to a channel that is independent of the
monitor image bot.
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


def check_remote_process(host, command, match_substring, min_count, ssh_timeout=30):
    """SSH to host, run command, count substring in stdout. Return (ok, msg)."""
    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={ssh_timeout}",
        host,
    ] + list(command)
    try:
        proc = subprocess.run(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=ssh_timeout + 5,
        )
    except subprocess.TimeoutExpired:
        return False, f"{host}: ssh timeout after {ssh_timeout}s"
    except FileNotFoundError:
        return False, f"{host}: ssh binary not found"

    stdout = proc.stdout.decode(errors="replace")
    stderr = proc.stderr.decode(errors="replace").strip()

    if proc.returncode != 0 and not stdout:
        return False, f"{host}: '{' '.join(command)}' failed ({stderr or f'exit {proc.returncode}'})"

    count = stdout.count(match_substring)
    if count < min_count:
        return False, f"{host}: '{match_substring}' count={count}, expected >={min_count}"
    return True, None


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

    return issues


def main():
    print("Starting bb_monitor_systemcheck...")
    interval = max(1, int(getattr(config, "systemcheck_interval_hours", 1)))
    while True:
        loop_start = datetime.now()
        issues = run_checks()
        if not issues:
            text = "All systems OK"
        else:
            text = "Issues found:\n" + "\n".join(f"- {i}" for i in issues)
        try:
            mon.send_message(config, text)
        except Exception as e:
            print(f"Failed to send Telegram message: {e}", flush=True)

        next_run = (loop_start + timedelta(hours=interval)).replace(
            minute=0, second=0, microsecond=0,
        )
        sleep_secs = (next_run - datetime.now()).total_seconds()
        time.sleep(max(60, sleep_secs))


if __name__ == "__main__":
    main()
