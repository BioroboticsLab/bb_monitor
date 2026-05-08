# bb_monitor

`bb_monitor` is a simple monitoring tool that extracts the first frame from each video recorded by your camera system, adds timestamps and filenames, combines them into a composite image, and sends it to a Telegram bot. It's useful for remote checks on multi-camera setups.

## Features

- Extracts and stamps first frames from recent videos
- Optionally saves individual images
- Creates a vertically stacked composite image
- Sends results via Telegram on a set schedule
- Configurable via Python config files

## Installation

```bash
git clone https://github.com/BioroboticsLab/bb_monitor.git
cd bb_monitor
pip install .
```

## Configuration

You can pass a config file as a command-line argument:

```bash
python bb_monitor.py /path/to/my_config.py
```

If no config is provided, the script will try to load user_config.py, and finally fall back to default_config.py. Create your config by copying and editing default_config.py.

## Running

Run the script using either:

```bash
python bb_monitor.py /path/to/my_config.py
```

or (with user_config.py in the root):
```bash
python bb_monitor.py
```

## Running multiple configs

If you have several monitor configs (e.g. one per hive plus feeders/exits), run them all in one process with `bb_monitor_multi.py`:

```bash
python bb_monitor_multi.py hiveA_monitor_config.py hiveB_monitor_config.py \
                           hiveC_monitor_config.py hiveD_monitor_config.py \
                           feeders_monitor_config.py exitcams_monitor_config.py
```

Or with a glob: `python bb_monitor_multi.py *_monitor_config.py`.

Each config runs in its own thread; if a thread crashes it auto-restarts after 10s. Ctrl-C exits the whole launcher.

## System check

`bb_monitor_systemcheck.py` is a separate script that runs on a regular interval (hourly by default) and posts a single status message to a Telegram channel that is independent of the monitor image bot. It performs two kinds of checks:

- **Reachability** — ICMP-pings each host listed in `systemcheck_ping_hosts` (typically the Raspberry Pi camera hosts).
- **Remote process check** — for each entry in `systemcheck_process_hosts`, SSHes to the host, runs the configured command, and counts occurrences of a substring in the output. If the count is below `min_count`, the host is flagged. This is generic enough to cover both `nvidia-smi`-based GPU process checks and plain `pgrep` checks.

SSH keys must be configured for passwordless login from the machine running the script to every host in `systemcheck_process_hosts`.

Configuration lives in a separate file from the monitor config so the two can use different Telegram bots/chats. Copy `default_config_systemcheck.py` to `user_config_systemcheck.py` (gitignored) and edit:

```bash
python bb_monitor_systemcheck.py /path/to/my_systemcheck_config.py
```

If no config is passed on the command line, the script loads `user_config_systemcheck.py`, falling back to `default_config_systemcheck.py`.
