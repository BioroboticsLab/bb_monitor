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

- An issue must be found on **two consecutive ticks** before it is reported. A short-lived network blip that has cleared by the next tick never reaches Telegram; a real fault is announced one tick (default 10 min) later than it is first seen. Once confirmed, it is re-reported on every tick until it clears.
- The first *completely clean* tick after an alert posts a one-time "All systems OK" **recovery** message, then the loop goes silent again.
- If everything is fine, the tick is silent — *except* the first fast-tick of each hour, which always posts a summary so a silent failure of the systemcheck process itself eventually becomes visible. If findings are present but not yet confirmed, that hourly summary lists them rather than claiming all is well.

Findings are matched across ticks by a stable `(host, kind)` key, not by message text — the text carries detail that changes every tick (a stale heartbeat's age grows), and several different probes share the same wording.

Recovery requires a tick with **zero** findings, not merely zero *confirmed* findings. A finding disappearing does not prove it was fixed: when a camera fails to ping, the rest of its checks are skipped, so a transient blip would otherwise hide a still-wedged camera and read as a recovery.

Confirmation and remediation state is in-memory, so a restart while an issue is outstanding re-arms the two-tick counter and can miss that single recovery message; the hourly summary still confirms all-clear within the hour.

### Image on recovery

On a recovery (the "All systems OK" right after an error), the system check can push a fresh monitor image so you can *visually* confirm the cameras are back. List the monitor config(s) to fire in the system-check config:

```python
systemcheck_trigger_monitor_configs = [
    "/abs/path/feeders_monitor_config.py",
    "/abs/path/exitcams_monitor_config.py",
]
systemcheck_trigger_timeout_seconds = 60  # per-config wall-clock timeout
```

Each listed config is run once via `BB_MONITOR_ONCE=1 python bb_monitor.py <config>` as an isolated subprocess (so a hung video read can't stall the check loop). The image lands in **that monitor's own image channel**, not the System Check channel. Use **absolute paths**. Leave the list empty (the default) to disable the feature — recovery then just sends the text message. The image fires once per error→clear edge, not on the routine hourly "All systems OK".

### Checks

Four independent check lists in the config:

- `systemcheck_cameras` — typed entries that bundle a per-camera set of checks:
  - `feedercam`: ping → clock → `raspicam.service` active → raspicam heartbeat file fresh → `imgstorage.service` active → `mini_scale_logger.service` active → mini-scale CSV last row fresh.
  - `exitcam`: ping → clock → `raspicam.service` active → raspicam heartbeat file fresh → `imgstorage.service` active.
  If ping fails, the rest of the host's checks are skipped to avoid a wall of cascading SSH timeouts.
- `systemcheck_temploggers` — for each entry, ping → clock → `temperaturelogger.service` active (override via `"service"` key) → CSV last row fresh.
- `systemcheck_ping_hosts` — plain ICMP pings.
- `systemcheck_process_hosts` — SSHes to the host, runs the configured command, and counts occurrences of a substring in the output. Generic enough to cover `nvidia-smi`-based GPU process checks or plain `pgrep` checks.

The "service active" checks use `systemctl is-active`. "CSV last row fresh" parses the leading ISO timestamp on the last non-header row of the newest file matching a glob.

The heartbeat check reads the file's mtime and the remote `date +%s` in one shot, so a constant client/server clock offset cancels out. It distinguishes **stale** (raspicam is alive but not capturing — the wedge), **missing** (this Pi runs a raspicam build older than heartbeat support, so it will never write one), and **mtime in the future** (the remote clock stepped backwards). The camera host must be running a build of [bb_raspicam](https://github.com/BioroboticsLab/bb_raspicam) that touches the heartbeat file (default `/tmp/raspicam_heartbeat`); the path, the ~30-frame cadence, and the camera's own watchdog are configurable there via a `[Monitoring]` section.

### Clock check

Every SSH-reachable host's clock is compared against this machine's, since all devices in an experiment share a time server. A host whose clock is off by more than `systemcheck_max_clock_skew_seconds` (default 60) is reported.

The comparison is bounded by the SSH round trip: the local clock is read either side of the call, so a slow connection can only ever *shrink* the reported skew, never invent one. When **every** reachable device disagrees in the same direction, the monitor reports `Monitor host clock may be wrong` once instead of one alert per device.

### Auto-remediation

A raspicam can stop delivering frames while its process stays alive, so `systemctl is-active` keeps saying `active` and systemd never restarts it. The only symptom is a stale heartbeat.

When a heartbeat finding is *confirmed* (present on two consecutive ticks), the system check SSHes in and SIGKILLs the raspicam process by name; systemd then restarts it. The kill line is added to the Telegram alert as an audit trail.

```python
systemcheck_remediation_enabled          = True
systemcheck_remediation_cooldown_minutes = 60   # at most one kill per host per hour
systemcheck_remediation_max_attempts     = 3    # then escalate instead of retrying
systemcheck_remediation_hourly_only      = False  # restrict fixes to the top-of-hour tick
```

Guards: it never fires while `raspicam.service` is stopped (someone is working on the device), never on a "mtime in the future" finding (that is a clock problem, not a wedge), and after `max_attempts` it gives up and says the Pi probably needs a bb_raspicam redeploy rather than restarting it forever.

`SIGKILL` rather than `SIGTERM` is deliberate: systemd treats SIGTERM as a *clean* exit, so a unit with `Restart=on-failure` — what `setup_autostart.sh` deployed for years — would not restart after a `pkill`. SIGKILL restarts under both policies, so remediation works before the `Restart=always` unit change has reached every Pi. `raspicam.py` installs no signal handler, so nothing graceful is lost.

SSH keys must be configured for passwordless login from the machine running the script to every camera, templogger, and process host. No `sudo` is required: the kill targets a process owned by the same user we SSH in as.

### Diagnosing "Cannot reach ..."

The ping check reports *why* it failed and **whose fault it is**, because the failure
modes mean opposite things:

| what happened | ping | blamed on |
|---|---|---|
| host did not answer | exit 1, silent | the host |
| name did not resolve | exit 2, `Temporary failure in name resolution` | the monitor |
| lookup stalled, ping never returned | *(no exit)* | the monitor |
| no route | exit 2, `Network is unreachable` | the monitor |

Name resolution runs on the monitor, so a lookup that stalls or fails says nothing
about the camera. **When two or more hosts fail monitor-side, the alert collapses
into one line** naming the machine actually at fault, rather than one line per
camera. It also collapses when *every* pinged host is unreachable — cameras do not
leave a network together. A host that is genuinely silent keeps its own line either
way, so a real outage never hides behind a resolver complaint.

Run `bash diagnose_ping.sh [host ...]` **on the monitor host** to pin it down. Run it
from a *cold* link (schedule it with `at`): an interactive ssh session keeps WiFi
awake and hides exactly the stall you are hunting.

A stalled mDNS lookup on WiFi usually means **power saving**. mDNS is multicast, and
a client in power save only wakes for multicast at DTIM beacons, so a link that idles
between checks starts every check cold. Fix it with
`sudo iw dev <dev> set power_save off` (persist via NetworkManager
`wifi.powersave = 2`); this does not touch routing.

### Address cache

A caching *resolver* cannot help here: [RFC 6762 §10](https://www.rfc-editor.org/rfc/rfc6762.html#section-10)
gives mDNS host records a 120s TTL while this loop checks every 600s, so any
compliant cache has expired by the time the next check runs. It would be cold every
single time.

So the monitor keeps its own. Each hostname's IPv4 address is remembered **in memory**
(never on disk — a stale entry outliving the process is the `/etc/hosts` failure
mode), and both `ping` and `ssh` use it, so a healthy check performs no name lookup
at all. Every add and drop is printed, so `tmux attach` shows exactly what the
monitor believes each camera's address to be:

```
[resolve] feedercama.local -> 192.168.178.52
[resolve] dropped feedercama.local (was 192.168.178.52); will re-resolve by name
```

It is never trusted for long, so a Pi rebooting onto a new DHCP lease self-heals with
no reserved leases and nothing to edit:

- the **last ping attempt of every check goes by name**, so a moved lease is picked up
  within the same tick;
- an address that stops answering, or that an ssh transport failure rejects, is
  dropped and re-resolved on the next check;
- ssh connects to the IP but passes `-o HostKeyAlias=<hostname>`, so `known_hosts`
  still matches — and a *recycled* lease now pointing at a different machine fails
  loudly on a host-key mismatch rather than silently monitoring the wrong box.

Set `systemcheck_cache_addresses = False` to disable and resolve on every check.

Retries are spaced so the attempts outlast a hiccup on the monitor's own link rather
than all landing inside it: `(ping_attempts - 1) * ping_retry_delay_seconds`, 10s by
default. Keep `ping_timeout_seconds >= 1`: Linux `ping -W 0` waits forever.

### Tests

The decision logic (two-tick confirmation, clock-skew bounds, heartbeat parsing) lives in `src/systemcheck_core.py` and is pure, so it runs without a network or a Pi:

```bash
python -m pytest tests/
```

### Running

Configuration lives in a separate file from the monitor config so the two can use different Telegram bots/chats. Copy `default_config_systemcheck.py` to `user_config_systemcheck.py` (gitignored) and edit:

```bash
python bb_monitor_systemcheck.py /path/to/my_systemcheck_config.py
```

If no config is passed on the command line, the script loads `user_config_systemcheck.py`, falling back to `default_config_systemcheck.py`.
