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

`bb_monitor_systemcheck.py` is a separate script that posts status messages to a Telegram channel independent of the monitor image bot.

### Cadence

The loop runs every `systemcheck_fast_interval_minutes` (default 10) aligned to the wall clock (`:00`, `:10`, `:20`, ...). On every tick:

- If any issue is found, a Telegram message lists the issues — the **immediate alert**.
- If everything is fine, the tick is silent — *except* the first fast-tick of each hour, which always posts an "All systems OK" summary so a silent failure of the systemcheck process itself eventually becomes visible.

### Checks

Four independent check lists in the config:

- `systemcheck_cameras` — typed entries that bundle a per-camera set of checks:
  - `feedercam`: ping → `raspicam.service` active → raspicam heartbeat file fresh → `imgstorage.service` active → `mini_scale_logger.service` active → mini-scale CSV last row fresh.
  - `exitcam`: ping → `raspicam.service` active → raspicam heartbeat file fresh → `imgstorage.service` active.
  If ping fails, the rest of the host's checks are skipped to avoid a wall of cascading SSH timeouts.
- `systemcheck_temploggers` — for each entry, ping → `bb-templogger.service` active → CSV last row fresh.
- `systemcheck_ping_hosts` — plain ICMP pings.
- `systemcheck_process_hosts` — SSHes to the host, runs the configured command, and counts occurrences of a substring in the output. Generic enough to cover `nvidia-smi`-based GPU process checks or plain `pgrep` checks.

The "service active" checks use `systemctl is-active`. "Heartbeat fresh" stats a file on the remote host and compares its mtime to the remote `date +%s` (no client/server clock skew). "CSV last row fresh" parses the leading ISO timestamp on the last non-header row of the newest file matching a glob.

For raspicam heartbeat freshness to work, the camera host must be running a build of [bb_raspicam](https://github.com/BioroboticsLab/bb_raspicam) that touches the heartbeat file (default `/tmp/raspicam_heartbeat`) every ~30 captured frames. Optionally configurable in the raspicam config via a `[Monitoring]` section.

SSH keys must be configured for passwordless login from the machine running the script to every camera, templogger, and process host.

### Running

Configuration lives in a separate file from the monitor config so the two can use different Telegram bots/chats. Copy `default_config_systemcheck.py` to `user_config_systemcheck.py` (gitignored) and edit:

```bash
python bb_monitor_systemcheck.py /path/to/my_systemcheck_config.py
```

If no config is passed on the command line, the script loads `user_config_systemcheck.py`, falling back to `default_config_systemcheck.py`.
