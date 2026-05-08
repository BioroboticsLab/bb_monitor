# Telegram channel for system-check messages.
# Use a different bot/chat than the monitor bot so check alerts don't pollute the image feed.
monitor_bot_name   = "System Check"
telegram_bot_token = "FILL IN API TOKEN"
telegram_chat_id   = "FILL IN TELEGRAM CHAT ID"

# Run every N hours, aligned to the top of the hour.
systemcheck_interval_hours = 1

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
