# Telegram channel for system-check messages.
# Use a different bot/chat than the monitor bot so check alerts don't pollute the image feed.
monitor_bot_name   = "System Check"
telegram_bot_token = "FILL IN API TOKEN"
telegram_chat_id   = "FILL IN TELEGRAM CHAT ID"

# Fast cadence (minutes). The loop wakes on every multiple of this past midnight,
# but only posts to Telegram when issues are found. The first fast-tick of every
# hour also posts an "All systems OK" summary even when there are no issues.
systemcheck_fast_interval_minutes = 10

# Cameras with bundled per-type checks.
# - feedercam: ping + raspicam.service + raspicam heartbeat freshness
#              + imgstorage.service + mini_scale_logger.service + scale CSV freshness
# - exitcam:   ping + raspicam.service + raspicam heartbeat freshness + imgstorage.service
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

# Temperature loggers: ping + bb-templogger.service + CSV freshness.
systemcheck_temploggers = [
    # {"hostname": "tempbox-a.local",
    #  "csv_glob": "~/bb_temperatureloggers/data/temperature_data_*.csv",
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

# Optional overrides; the script has sensible defaults if these are absent.
ping_timeout_seconds = 2
ssh_timeout_seconds  = 30
