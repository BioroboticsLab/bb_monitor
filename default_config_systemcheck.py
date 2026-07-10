# Telegram channel for system-check messages.
# Use a different bot/chat than the monitor bot so check alerts don't pollute the image feed.
monitor_bot_name   = "System Check"
telegram_bot_token = "FILL IN API TOKEN"
telegram_chat_id   = "FILL IN TELEGRAM CHAT ID"

# Fast cadence (minutes). The loop wakes on every multiple of this past midnight,
# but only posts to Telegram when issues are found. An issue must be seen on TWO
# consecutive ticks before it is reported, so a blip that clears by the next tick
# stays quiet; a real fault is announced one tick late. The first fully clean tick
# after an alert posts a one-time "All systems OK" recovery message, then goes
# silent again. The first fast-tick of every hour also posts a summary even when
# there are no issues.
systemcheck_fast_interval_minutes = 10

# --- Clock check ---
# Every SSH-reachable host's clock is compared against this machine's, bounded by
# the SSH round-trip time so a slow connection can never fake a skew. Flag a host
# whose clock differs by more than this many seconds. When *every* reachable device
# disagrees in the same direction, the loop reports "monitor host clock may be
# wrong" once instead of one alert per device.
systemcheck_max_clock_skew_seconds = 60

# --- Auto-remediation of wedged raspicams ---
# A raspicam can stop delivering frames while the process stays alive, so
# `systemctl is-active` says "active" and systemd never restarts it. The only
# symptom is a stale (or eventually missing) heartbeat file. When that finding is
# CONFIRMED (seen on two consecutive ticks), SSH in and SIGKILL the raspicam
# process by name; systemd then restarts it.
#
# Guards: never fires while raspicam.service is stopped (someone is working on the
# device), at most one kill per host per cooldown, and it gives up after
# max_attempts and says so — a Pi running a raspicam build older than the heartbeat
# support will never produce one, however often it is restarted.
systemcheck_remediation_enabled          = True
systemcheck_remediation_cooldown_minutes = 60
systemcheck_remediation_max_attempts     = 3
# Restrict fixes to the top-of-hour tick, so they are less likely to interrupt
# someone physically at the device.
systemcheck_remediation_hourly_only      = False

# --- On-demand monitor image on recovery (off by default) ---
# When the system check detects a recovery (an "All systems OK" right after an
# error), it spawns the monitor bot once per config listed below to push a fresh
# image to that monitor's own image channel — so you can visually confirm the
# cameras are back. Leave the list empty to disable. Use ABSOLUTE paths (each
# child runs with cwd = this repo).
systemcheck_trigger_monitor_configs = [
    # "/home/pi/bb_monitor/feeders_monitor_config.py",
    # "/home/pi/bb_monitor/exitcams_monitor_config.py",
]
# Per-config wall-clock timeout (seconds) for the one-shot image send.
systemcheck_trigger_timeout_seconds = 60

# Cameras with bundled per-type checks. Every camera also gets a clock check.
# - feedercam: ping + clock + raspicam.service + raspicam heartbeat freshness
#              + imgstorage.service + mini_scale_logger.service + scale CSV freshness
# - exitcam:   ping + clock + raspicam.service + raspicam heartbeat freshness + imgstorage.service
# Per-entry keys override the defaults baked into bb_monitor_systemcheck.py
# (raspicam heartbeat path, max-age thresholds, scale CSV glob).
systemcheck_cameras = [
    # {"hostname": "feedercama.local", "type": "feedercam"},
    # {"hostname": "feedercamb.local", "type": "feedercam",
    #  "scale_csv_glob": "~/bb_mini_scales/data/weight_data_*.csv",
    #  "scale_max_age_seconds": 30,
    #  "raspicam_heartbeat": "/tmp/raspicam_heartbeat",
    #  "raspicam_max_age_seconds": 30},
    # {"hostname": "exitcama.local",   "type": "exitcam"},
    # {"hostname": "exitcamb.local",   "type": "exitcam"},
]

# Temperature loggers: ping + temperaturelogger.service + CSV freshness.
# Override the default service name per-entry with "service": "..." if needed.
systemcheck_temploggers = [
    # {"hostname": "thria",
    #  "csv_glob": "~/bb2026/bb_temperatureloggers/data/temperature_data_*.csv",
    #  "max_age_seconds": 60},
]

# Hosts pinged via ICMP. Empty list = skip ping checks.
systemcheck_ping_hosts = [
    # "exitcama.local", "exitcamb.local",
    # "feedercama.local", "feedercamb.local",
]

# Compute hosts checked via SSH. SSH keys must be set up for passwordless login
# from the machine running this script to every hostname listed below.
# Each entry: SSH to `hostname`, run `command`, count occurrences of `match_substring`
# in stdout; flag the host if count < `min_count`.
systemcheck_process_hosts = [
    # {"hostname": "thria",  "command": ["nvidia-smi"],
    #  "match_substring": "bb_imgacquisition", "min_count": 4},
    # {"hostname": "cirrus", "command": ["pgrep", "-af", "rpi_imgcapture"],
    #  "match_substring": "rpi_imgcapture",     "min_count": 1},
]

# Transfer-backlog hosts: SSH in and count the video files bb_imgacquisition has
# written under <directory>/<cam>/ but the transfer process hasn't moved off the
# box yet. Normally near-zero; a growing total means the transfer is broken. Warn
# when the host's TOTAL file count exceeds num_files_to_warn (default 60).
# "directory" may use a leading ~/ for the SSH user's home; set "command" to override
# the auto-built count command (it must print an integer). SSH keys must allow
# passwordless login, as for systemcheck_process_hosts.
systemcheck_transfer_hosts = [
    # {"hostname": "cirrus", "ssh_user": "beesbook",
    #  "directory": "bb2026/bb_imgacquisition/data/out", "num_files_to_warn": 60},
]

# Optional overrides; the script has sensible defaults if these are absent.
#
# Keep ping_timeout_seconds >= 1: Linux `ping -W 0` waits forever.
#
# The retries exist to outlast a dropout on the MONITOR host, not to be patient with
# the cameras. When its WiFi reassociates (a few seconds), `ping` fails instantly
# with "Temporary failure in name resolution" — so back-to-back retries would all
# land inside the dropout and report the whole fleet dead. What matters is the span
# they cover: (attempts - 1) * retry_delay_seconds, here 2 * 5 = 10s.
ping_timeout_seconds     = 2
ping_attempts            = 3
ping_retry_delay_seconds = 5
ssh_timeout_seconds      = 30

# Remember each hostname's IPv4 address in memory and use it for ping and ssh, so a
# check never waits on a name lookup. A resolver cache cannot do this: mDNS host
# records carry a 120s TTL (RFC 6762 s10) while checks run every 600s, so a compliant
# cache is expired every time and each check pays a fresh multicast lookup — which
# stalls over a WiFi link in power save.
#
# Nothing is persisted, and the cache is never trusted for long: the last ping
# attempt of every check goes by name, an address that stops answering is dropped,
# and ssh looks the host key up under the hostname so a recycled DHCP lease fails
# loudly rather than silently monitoring the wrong machine. Set False to disable and
# resolve on every check.
systemcheck_cache_addresses = True
