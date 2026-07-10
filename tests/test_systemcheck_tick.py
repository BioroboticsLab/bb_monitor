"""End-to-end tests of one system-check tick against a simulated Raspberry Pi.

These cover the interactions the unit tests can't: that a blip stays silent, that a
blip mid-incident never fakes a recovery, that a wedged camera actually gets killed,
and that a camera someone stopped by hand never does.
"""
import datetime as _datetime
import re
import subprocess
import time

import pytest

import bb_monitor_systemcheck as sc


TICK_MINUTES = 10
# Start at :15 so that no tick in a short run lands on the hourly summary
# (is_hourly_tick is `minute < fast_interval_minutes`): 15, 25, 35, 45, 55.
START = _datetime.datetime(2026, 7, 10, 12, 15, 0)


class _StopLoop(Exception):
    """Raised from the patched time.sleep to end main()'s infinite loop."""


class FakePi:
    """Answers the exact remote commands bb_monitor sends over SSH."""

    def __init__(self):
        self.reachable = True
        self.raspicam_active = True
        self.heartbeat_exists = True
        self.heartbeat_age = 0        # seconds; > 30 is "stale", negative is "future"
        self.clock_offset = 0         # seconds ahead of the monitor
        self.kills = 0
        self.heals_on_kill = True     # False models a Pi a restart cannot fix
        # Simulates a human running `systemctl stop raspicam` in the window between
        # the heartbeat probe and the kill.
        self.stop_after_heartbeat_probe = False

    @property
    def now(self):
        # The clock check compares this against the monitor's real time.time(), so
        # it has to track it. Simulated elapsed time lives in heartbeat_age instead.
        return int(time.time())

    def ssh(self, target, cmd, timeout):
        if not self.reachable:
            return None, f"ssh timeout after {timeout}s"
        if "pkill" in cmd:
            self.kills += 1
            if self.heals_on_kill:    # systemd restarts it; heartbeat goes fresh
                self.heartbeat_age = 0
                self.heartbeat_exists = True
            return _proc(0, "killed 1"), None
        if cmd == "date +%s":
            return _proc(0, str(self.now + self.clock_offset)), None
        if "raspicam_heartbeat" in cmd:
            reply = (_proc(0, f"MISSING {self.now}") if not self.heartbeat_exists
                     else _proc(0, f"OK {self.now - self.heartbeat_age} {self.now}"))
            if self.stop_after_heartbeat_probe:
                self.raspicam_active = False
            return reply, None
        if "systemctl is-active raspicam.service" in cmd:
            return _proc(0 if self.raspicam_active else 3,
                         "active" if self.raspicam_active else "inactive"), None
        if "systemctl is-active" in cmd:
            return _proc(0, "active"), None
        raise AssertionError(f"unexpected remote command: {cmd!r}")


def _proc(rc, stdout, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=rc, stdout=stdout.encode(), stderr=stderr.encode())


def _fake_ping(reachable, monitor_side=False):
    """check_ping returns a PingResult."""
    def ping(host, ping_cfg=None):
        if reachable(host):
            return sc.PingResult(True)
        return sc.PingResult(False, "no reply within 2s", monitor_side)
    return ping


FAST = sc.PingSettings(timeout_seconds=2, attempts=1, retry_delay_seconds=0)


@pytest.fixture(autouse=True)
def _empty_address_cache():
    """The address cache lives for the life of the process; don't let it leak
    between tests."""
    sc._addresses.clear()
    yield
    sc._addresses.clear()


@pytest.fixture
def pi(monkeypatch):
    fake = FakePi()

    cfg = sc.config
    monkeypatch.setattr(cfg, "systemcheck_cameras",
                        [{"hostname": "exitcamd.local", "type": "exitcam"}], raising=False)
    for name, value in [
        ("systemcheck_ping_hosts", []), ("systemcheck_process_hosts", []),
        ("systemcheck_temploggers", []), ("systemcheck_transfer_hosts", []),
        ("systemcheck_trigger_monitor_configs", []),
        ("systemcheck_fast_interval_minutes", TICK_MINUTES),
        ("systemcheck_max_clock_skew_seconds", 60),
        ("systemcheck_remediation_enabled", True),
        ("systemcheck_remediation_cooldown_minutes", 60),
        ("systemcheck_remediation_max_attempts", 3),
        ("systemcheck_remediation_hourly_only", False),
    ]:
        monkeypatch.setattr(cfg, name, value, raising=False)

    monkeypatch.setattr(sc, "_ssh_run", fake.ssh)
    monkeypatch.setattr(sc, "check_ping", _fake_ping(lambda h: fake.reachable))
    return fake


def run_ticks(monkeypatch, pi, script):
    """Run main() for len(script) ticks; script[i] mutates the Pi(s) before tick i.

    `pi` is a FakePi or a list of them, and is handed to each script step as-is.

    Returns [(tick_index, message), ...]. The tick index matters: "did a recovery
    message go out at all" is a much weaker assertion than "did it go out on the
    right tick", and only the latter catches a wrong recovery condition.
    """
    clock = {"t": START, "i": 0}
    sent_at = []
    pis = pi if isinstance(pi, list) else [pi]

    class FakeDatetime:
        @staticmethod
        def now():
            return clock["t"]

    def before_each_tick():
        if clock["i"] < len(script):
            script[clock["i"]](pi)

    real_run_checks = sc.run_checks
    real_notify = sc._notify

    def run_checks():
        before_each_tick()
        return real_run_checks()

    def notify(text):
        sent_at.append((clock["i"], text))
        return real_notify(text)

    def fake_sleep(_seconds):
        clock["i"] += 1
        clock["t"] += _datetime.timedelta(minutes=TICK_MINUTES)
        for p in pis:                      # a wedged camera's heartbeat keeps aging
            if p.heartbeat_age > 0:
                p.heartbeat_age += TICK_MINUTES * 60
        if clock["i"] >= len(script):
            raise _StopLoop

    monkeypatch.setattr(sc, "datetime", FakeDatetime)
    monkeypatch.setattr(sc, "run_checks", run_checks)
    monkeypatch.setattr(sc, "_notify", notify)
    monkeypatch.setattr(sc.time, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        sc.main()
    return sent_at


def wedge(p):
    p.heartbeat_age = 200      # alive, "active", but not capturing


def healthy(p):
    p.reachable = True
    p.raspicam_active = True
    p.heartbeat_exists = True
    p.heartbeat_age = 0


def unreachable(p):
    p.reachable = False


# ---------- the three requested behaviours ----------

def test_a_transient_blip_is_never_reported(monkeypatch, pi):
    """'Cannot reach feedercama.local' that clears by the next tick: total silence."""
    sent = run_ticks(monkeypatch, pi, [unreachable, healthy, healthy])
    assert sent == []


def test_a_persistent_fault_is_reported_on_the_second_tick(monkeypatch, pi):
    sent = run_ticks(monkeypatch, pi, [unreachable, unreachable])
    assert [tick for tick, _ in sent] == [1], "silent on tick 0, alert on tick 1"
    assert "Cannot reach exitcamd.local" in sent[0][1]


def test_the_ping_failure_reason_reaches_the_alert(monkeypatch, pi):
    """Without the reason, eight simultaneous 'Cannot reach' lines look like eight
    dead cameras rather than one broken resolver on the monitor host."""
    monkeypatch.setattr(sc, "check_ping",
                        lambda host, ping_cfg=None: sc.PingResult(
                            False, "ping: %s: Temporary failure in name resolution" % host))
    sent = run_ticks(monkeypatch, pi, [unreachable, unreachable])
    assert "Cannot reach exitcamd.local" in sent[0][1]
    assert "Temporary failure in name resolution" in sent[0][1]


def test_a_standing_issue_keeps_being_reported_every_tick(monkeypatch, pi):
    """Confirmation delays the first alert; it does not mute the ones after it."""
    sent = run_ticks(monkeypatch, pi, [unreachable] * 4)
    assert [tick for tick, _ in sent] == [1, 2, 3]
    assert all("Cannot reach exitcamd.local" in m for _, m in sent)


def test_a_wedged_camera_is_confirmed_then_restarted_then_recovers(monkeypatch, pi):
    """The production symptom: process alive, service active, heartbeat stale."""
    sent = run_ticks(monkeypatch, pi, [wedge, lambda p: None, lambda p: None, lambda p: None])

    assert pi.kills == 1, "killed exactly once"
    assert [tick for tick, _ in sent] == [1, 2], sent
    assert "stale" in sent[0][1] and "Issues found" in sent[0][1]
    assert "restarted raspicam on exitcamd.local" in sent[0][1]
    assert sent[1][1] == "All systems OK"


def test_a_blip_mid_incident_does_not_fake_a_recovery(monkeypatch, pi):
    """tick 2's ping failure hides the wedged camera's heartbeat finding, because
    _camera_checks returns early when ping fails. A "no *confirmed* findings ->
    recover" rule would post 'All systems OK' there, about a camera that is still
    wedged. Recovery must instead wait for a tick with no findings at all.

    Pinning the tick index is what gives this test teeth: both the correct and the
    buggy version send exactly ["Issues found", "All systems OK"] — they differ only
    in whether the OK lands on tick 2 (wrong) or tick 3 (right).
    """
    monkeypatch.setattr(sc.config, "systemcheck_remediation_enabled", False)
    sent = run_ticks(monkeypatch, pi, [
        wedge,                 # tick 0: pending, silent
        lambda p: None,        # tick 1: confirmed -> alert
        unreachable,           # tick 2: blip hides the heartbeat finding
        healthy,               # tick 3: genuinely clean -> recovery
    ])
    assert [tick for tick, _ in sent] == [1, 3], sent
    assert "Issues found" in sent[0][1] and "stale" in sent[0][1]
    assert sent[1] == (3, "All systems OK"), "the OK must arrive at tick 3, not tick 2"


# ---------- check_ping ----------

@pytest.mark.parametrize("system", ["Linux", "Darwin"])
@pytest.mark.parametrize("timeout", [0, 0.4, 0.5, 0.99, 1, 2])
def test_ping_timeout_argument_is_never_zero(monkeypatch, system, timeout):
    """Linux `ping -W 0` waits forever, so a sub-second config value truncating to 0
    would hang against exactly the unreachable host it is meant to time out on
    (verified: `ping -c 1 -W 0 192.0.2.1` never returns).

    Pinned for both platforms: on macOS -W is milliseconds, so the truncation is
    harmless there and a Darwin-only test would not catch a missing clamp.
    """
    monkeypatch.setattr(sc.platform, "system", lambda: system)
    flag, value = sc._ping_args(timeout)
    assert flag == "-W"
    assert int(value) >= 1


def test_ping_reports_name_resolution_failure_distinctly(monkeypatch):
    """8 hosts failing at once is a resolver problem on this machine, not 8 dead
    cameras — but only if the message says so. iputils: exit 2 + stderr."""
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(
            argv, 2, b"", b"ping: feedercama.local: Temporary failure in name resolution\n")
    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    result = sc.check_ping("feedercama.local", FAST)
    assert result.ok is False
    assert "Temporary failure in name resolution" in result.reason
    assert result.monitor_side is True, "resolution happens on this machine"


def test_ping_reports_a_silent_host_as_no_reply(monkeypatch):
    """No ICMP answer: iputils exits 1 and prints nothing to stderr."""
    monkeypatch.setattr(sc.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b""))
    result = sc.check_ping("exitcamd.local", FAST)
    assert (result.ok, result.reason) == (False, "no reply within 2s")
    assert result.monitor_side is False, "a silent host is the host's problem"


def test_ping_retries_before_declaring_a_host_unreachable(monkeypatch):
    """A single ICMP packet to a power-saving Pi over WiFi is occasionally dropped."""
    calls = []

    def flaky(argv, **kw):
        calls.append(argv)
        rc = 1 if len(calls) == 1 else 0
        return subprocess.CompletedProcess(argv, rc, b"", b"")

    monkeypatch.setattr(sc.subprocess, "run", flaky)
    result = sc.check_ping("feedercama.local", FAST._replace(attempts=2))
    assert (result.ok, result.reason) == (True, None)
    assert len(calls) == 2, "must actually retry"


def test_ping_gives_up_after_the_configured_attempts(monkeypatch):
    calls = []

    def always_down(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(sc.subprocess, "run", always_down)
    assert sc.check_ping("exitcamd.local", FAST._replace(attempts=3)).ok is False
    assert len(calls) == 3


def test_ping_retries_are_spaced_and_never_sleep_after_the_last_attempt(monkeypatch):
    """The delays are the only thing covering a monitor-host WiFi dropout, because a
    dead resolver makes ping return instantly rather than burning its timeout."""
    sleeps = []
    monkeypatch.setattr(sc.time, "sleep", sleeps.append)
    monkeypatch.setattr(sc.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(
                            argv, 2, b"", b"ping: h: Temporary failure in name resolution\n"))
    sc.check_ping("h", sc.PingSettings(timeout_seconds=2, attempts=3, retry_delay_seconds=5))
    assert sleeps == [5, 5]


def test_ping_does_not_sleep_when_the_first_attempt_succeeds(monkeypatch):
    """A healthy fleet must not pay the retry delay on every tick."""
    sleeps = []
    monkeypatch.setattr(sc.time, "sleep", sleeps.append)
    monkeypatch.setattr(sc.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"", b""))
    assert sc.check_ping("h", sc.PingSettings(attempts=3, retry_delay_seconds=5)).ok is True
    assert sleeps == []


def test_a_stalled_ping_is_classified_as_the_monitors_fault(monkeypatch):
    """The production symptom: `ping did not return within 7s`. Not exit 2 — ping
    never returned at all, because the mDNS lookup stalled over a WiFi link in power
    save. Resolution happens here, so this says nothing about the camera."""
    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(sc.subprocess, "run", hang)
    result = sc.check_ping("exitcamd.local", FAST)
    assert result.ok is False
    assert result.monitor_side is True
    assert "did not return" in result.reason


def test_a_network_unreachable_error_is_the_monitors_fault(monkeypatch):
    monkeypatch.setattr(sc.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(
                            argv, 2, b"", b"ping: connect: Network is unreachable\n"))
    assert sc.check_ping("exitcamd.local", FAST).monitor_side is True


def test_ping_gives_resolution_headroom_beyond_the_icmp_timeout(monkeypatch):
    """`ping -W` bounds only the wait for a reply; a slow mDNS lookup happens before
    that and would otherwise be killed by the subprocess timeout."""
    seen = {}
    monkeypatch.setattr(sc.subprocess, "run",
                        lambda argv, **kw: seen.update(kw) or
                        subprocess.CompletedProcess(argv, 0, b"", b""))
    sc.check_ping("h", sc.PingSettings(timeout_seconds=2, attempts=1))
    assert seen["timeout"] > 2


# ---------- the address cache in the hot path ----------

def _ping_stub(monkeypatch, handler):
    """Replace subprocess.run with `handler(target) -> (rc, stdout)`."""
    calls = []

    def fake(argv, **kw):
        target = argv[-1]
        calls.append(target)
        rc, stdout = handler(target)
        return subprocess.CompletedProcess(argv, rc, stdout.encode(), b"")

    monkeypatch.setattr(sc.subprocess, "run", fake)
    return calls


def test_a_cached_address_is_pinged_directly_and_no_name_is_resolved(monkeypatch):
    """The whole point: no mDNS lookup in the per-check hot path."""
    calls = _ping_stub(monkeypatch, lambda t: (0, f"PING {t} ({t}) 56 bytes"))
    addresses = sc.HostAddresses()
    addresses.remember("feedercama.local", "192.168.178.52")

    assert sc.check_ping("feedercama.local", FAST, addresses).ok is True
    assert calls == ["192.168.178.52"], "the hostname must never be looked up"


def test_the_address_ping_resolved_is_remembered(monkeypatch):
    _ping_stub(monkeypatch,
               lambda t: (0, "PING feedercama.local (192.168.178.52) 56(84) bytes of data."))
    addresses = sc.HostAddresses()
    sc.check_ping("feedercama.local", FAST, addresses)
    assert addresses.get("feedercama.local") == "192.168.178.52"


def test_a_moved_dhcp_lease_is_picked_up_within_the_same_tick(monkeypatch):
    """A Pi reboots onto a new lease. The cached address goes quiet, the final
    attempt-by-name finds it again, and the cache corrects itself — no /etc/hosts to
    edit, no reserved lease needed."""
    def handler(target):
        if target == "192.168.178.52":
            return 1, ""                       # old lease: silent
        return 0, "PING feedercama.local (192.168.178.99) 56(84) bytes of data."

    calls = _ping_stub(monkeypatch, handler)
    addresses = sc.HostAddresses()
    addresses.remember("feedercama.local", "192.168.178.52")

    result = sc.check_ping("feedercama.local",
                           FAST._replace(attempts=2, retry_delay_seconds=0), addresses)
    assert result.ok is True
    assert calls == ["192.168.178.52", "feedercama.local"]
    assert addresses.get("feedercama.local") == "192.168.178.99"


def test_a_host_that_stays_unreachable_drops_its_cached_address(monkeypatch):
    """So the next tick starts from a fresh lookup rather than a wrong address."""
    _ping_stub(monkeypatch, lambda t: (1, ""))
    addresses = sc.HostAddresses()
    addresses.remember("feedercama.local", "192.168.178.52")

    assert sc.check_ping("feedercama.local", FAST._replace(retry_delay_seconds=0),
                         addresses).ok is False
    assert addresses.get("feedercama.local") is None


def test_a_disabled_cache_resolves_by_name_every_time(monkeypatch):
    calls = _ping_stub(monkeypatch, lambda t: (0, f"PING x (192.168.178.52) 56 bytes"))
    addresses = sc.HostAddresses(enabled=False)
    sc.check_ping("feedercama.local", FAST, addresses)
    sc.check_ping("feedercama.local", FAST, addresses)
    assert calls == ["feedercama.local", "feedercama.local"]


# ---------- ssh over the cached address ----------

def _capture_argv(monkeypatch, returncode=0):
    seen = {}

    def fake(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode, b"ok", b"")

    monkeypatch.setattr(sc.subprocess, "run", fake)
    return seen

def test_ssh_connects_to_the_ip_but_checks_the_host_key_under_the_name(monkeypatch):
    """Otherwise known_hosts would not match and BatchMode ssh would refuse. It also
    means a recycled lease fails loudly on a host-key mismatch."""
    seen = _capture_argv(monkeypatch)
    sc._ssh_run(sc.SshTarget("pi@192.168.178.52", "feedercama.local"), "true", 5)
    assert "pi@192.168.178.52" in seen["argv"]
    assert "HostKeyAlias=feedercama.local" in seen["argv"]


def test_ssh_by_name_passes_no_host_key_alias(monkeypatch):
    seen = _capture_argv(monkeypatch)
    sc._ssh_run("pi@feedercama.local", "true", 5)
    assert not any("HostKeyAlias" in str(a) for a in seen["argv"])


def test_an_ssh_transport_failure_drops_the_cached_address(monkeypatch):
    """exit 255 against a cached IP: wrong host, moved lease, bad key. Re-resolve."""
    monkeypatch.setattr(sc.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 255, b"", b"boom"))
    sc._addresses.remember("feedercama.local", "192.168.178.52")
    sc._ssh_run(sc.SshTarget("pi@192.168.178.52", "feedercama.local"), "true", 5)
    assert sc._addresses.get("feedercama.local") is None


def test_an_ssh_timeout_drops_the_cached_address(monkeypatch):
    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(sc.subprocess, "run", hang)
    sc._addresses.remember("feedercama.local", "192.168.178.52")
    sc._ssh_run(sc.SshTarget("pi@192.168.178.52", "feedercama.local"), "true", 5)
    assert sc._addresses.get("feedercama.local") is None


def test_a_domain_level_ssh_failure_keeps_the_cached_address(monkeypatch):
    """`systemctl is-active` returning 3 says nothing about the address."""
    monkeypatch.setattr(sc.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 3, b"inactive", b""))
    sc._addresses.remember("feedercama.local", "192.168.178.52")
    sc._ssh_run(sc.SshTarget("pi@192.168.178.52", "feedercama.local"), "true", 5)
    assert sc._addresses.get("feedercama.local") == "192.168.178.52"


def test_ssh_target_prefers_the_cached_address():
    addresses = sc.HostAddresses()
    assert sc._ssh_target_for("cam.local", "pi", addresses) == sc.SshTarget("pi@cam.local", None)
    addresses.remember("cam.local", "10.0.0.5")
    assert sc._ssh_target_for("cam.local", "pi", addresses) == sc.SshTarget("pi@10.0.0.5", "cam.local")


def test_ssh_target_without_a_user_still_uses_the_cache():
    addresses = sc.HostAddresses()
    addresses.remember("cirrus", "10.0.0.9")
    assert sc._ssh_target_for("cirrus", None, addresses) == sc.SshTarget("10.0.0.9", "cirrus")


# ---------- the heartbeat probe command itself ----------
#
# The FakePi answers a canned reply to any command mentioning the heartbeat path, so
# it never exercises the shell the monitor actually sends. These run it for real.

def _run_probe(path):
    """Run the real probe command through a shell, with `stat` shimmed so the test is
    portable (macOS ships BSD stat, which has no -c)."""
    shim = 'stat() { echo 1700000000; }; '
    proc = subprocess.run(["sh", "-c", shim + sc._heartbeat_probe_cmd(str(path))],
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip()


def test_probe_reports_a_present_heartbeat(tmp_path):
    hb = tmp_path / "raspicam_heartbeat"
    hb.touch()
    rc, out = _run_probe(hb)
    assert rc == 0
    assert sc.parse_heartbeat(out, max_age_seconds=30)[0] in ("ok", "stale", "future")
    assert out.startswith("OK 1700000000 ")


def test_probe_reports_a_missing_heartbeat_and_still_exits_zero(tmp_path):
    rc, out = _run_probe(tmp_path / "nope")
    assert rc == 0, "a non-zero exit with empty stdout is what _ssh_failed misreads"
    assert out.startswith("MISSING ")
    assert sc.parse_heartbeat(out, max_age_seconds=30) == ("missing", None)


def test_the_old_probe_form_is_what_produced_the_ssh_exec_failed_message(tmp_path):
    """Regression witness: this is the command the code used to send."""
    missing = tmp_path / "nope"
    proc = subprocess.run(["sh", "-c", f"stat -c %Y -- {missing} && date +%s"],
                          capture_output=True, text=True)
    assert proc.returncode != 0 and proc.stdout.strip() == ""
    fake = subprocess.CompletedProcess([], proc.returncode, b"", b"stat: cannot statx")
    assert sc._ssh_failed(fake, "") is True, "misclassified as a transport error"


# ---------- the kill command itself ----------

def test_the_kill_command_can_never_match_its_own_shell():
    """`ssh host "pkill -f raspicam.py"` kills the login shell that runs it, because
    the shell's own /proc/pid/cmdline contains the pattern. The bracket trick is the
    only thing preventing that, so no bare token may appear anywhere in the command.
    Verified against procps-ng on Debian bookworm."""
    cmd = sc._REMEDIATION_CMD
    assert r"[r]aspicam\.py" in cmd, "bracket trick must be present"
    assert "raspicam.py" not in cmd.replace(r"[r]aspicam\.py", ""), \
        "a bare 'raspicam.py' token anywhere would make pkill -f kill the ssh shell"


def test_the_kill_command_uses_sigkill():
    """systemd counts SIGTERM as a *clean* exit, so a unit with Restart=on-failure —
    which is what setup_autostart.sh deployed for years — would not restart after a
    SIGTERM. SIGKILL restarts under both policies."""
    assert "-KILL" in sc._REMEDIATION_CMD
    assert "-TERM" not in sc._REMEDIATION_CMD


def test_the_kill_command_is_idempotent_and_never_looks_like_an_ssh_failure():
    """pkill exits 1 when nothing matched; a non-zero exit with empty stdout is
    exactly what _ssh_failed reads as a transport error."""
    cmd = sc._REMEDIATION_CMD
    assert cmd.rstrip().endswith("exit 0")
    assert 'echo "killed $n"' in cmd


# ---------- remediation guards ----------

def test_never_kills_a_camera_someone_stopped_by_hand(monkeypatch, pi):
    def stopped(p):
        p.raspicam_active = False
    sent = run_ticks(monkeypatch, pi, [stopped, lambda p: None, lambda p: None])
    assert pi.kills == 0, "a stopped service means a human is working on the device"
    assert "not active" in sent[0][1]
    assert "restarted raspicam" not in sent[0][1]


def test_a_service_stopped_between_probe_and_kill_aborts_the_kill(monkeypatch, pi):
    """The narrow race the pre-kill re-check exists for: the heartbeat probe already
    reported stale when someone runs `systemctl stop raspicam` and starts the camera
    by hand. Without the re-check we would kill their process."""
    def race(p):
        p.stop_after_heartbeat_probe = True

    sent = run_ticks(monkeypatch, pi, [wedge, race, lambda p: None])
    assert pi.kills == 0, "must not kill a camera whose service was just stopped"
    assert "is stopped; skipping auto-restart" in sent[0][1]


def test_a_missing_heartbeat_is_reported_as_missing_not_as_an_ssh_error(monkeypatch, pi):
    """Regression: `stat ... && date +%s` short-circuited, and the empty stdout plus
    non-zero exit made _ssh_failed report 'ssh exec failed (stat: cannot statx ...)'."""
    def gone(p):
        p.heartbeat_exists = False
    sent = run_ticks(monkeypatch, pi, [gone, lambda p: None, lambda p: None])
    assert "missing (raspicam not writing heartbeat)" in sent[0][1]
    assert "ssh exec failed" not in sent[0][1]
    assert pi.kills == 1, "a missing heartbeat is remediable too"


def test_a_backwards_clock_step_is_reported_but_never_remediated(monkeypatch, pi):
    """An mtime in the future means the remote clock jumped backwards. The camera is
    fine; restarting it fixes nothing and loses footage."""
    def stepped(p):
        p.heartbeat_age = -600
    sent = run_ticks(monkeypatch, pi, [stepped, lambda p: None, lambda p: None])
    assert "in the future" in sent[0][1]
    assert pi.kills == 0


def test_remediation_gives_up_after_max_attempts(monkeypatch, pi):
    """A Pi running a raspicam too old to write a heartbeat can be restarted forever
    without ever producing one. Stop, and say what actually needs doing."""
    monkeypatch.setattr(sc.config, "systemcheck_remediation_cooldown_minutes", 0)

    def unfixable(p):
        p.heals_on_kill = False
        p.heartbeat_age = max(p.heartbeat_age, 200)

    sent = run_ticks(monkeypatch, pi, [unfixable] * 6)
    assert pi.kills == 3, f"max_attempts is 3, got {pi.kills} kills"
    assert "auto-restart did not help after 3 attempts" in sent[-1][1]
    assert "up to date" in sent[-1][1]


def test_remediation_respects_the_cooldown(monkeypatch, pi):
    """Ticks are 10 min apart and the cooldown is 60 min: only one kill in 5 ticks,
    even though the camera stays wedged the whole time."""
    def stay_wedged(p):
        p.heartbeat_age = max(p.heartbeat_age, 200)
        p.heartbeat_exists = True

    # the kill "succeeds" but the camera comes straight back wedged
    run_ticks(monkeypatch, pi, [wedge] + [stay_wedged] * 5)
    assert pi.kills == 1, f"cooldown should have suppressed later kills, got {pi.kills}"


def test_hourly_only_gate_suppresses_the_kill_off_the_hour(monkeypatch, pi):
    monkeypatch.setattr(sc.config, "systemcheck_remediation_hourly_only", True)
    run_ticks(monkeypatch, pi, [wedge, lambda p: None, lambda p: None])
    assert pi.kills == 0, "ticks at :15/:25/:35 are not hourly ticks"


# ---------- clock ----------

def test_a_skewed_device_clock_is_reported(monkeypatch, pi):
    def skew(p):
        p.clock_offset = 300
    sent = run_ticks(monkeypatch, pi, [skew, lambda p: None, lambda p: None])
    assert "exitcamd.local" in sent[0][1]
    # The reported skew is bounded below by the true offset minus the round trip,
    # and `date +%s` truncates to whole seconds, so don't pin the exact integer.
    match = re.search(r"clock off by (\d+)s", sent[0][1])
    assert match, sent[0][1]
    assert 295 <= int(match.group(1)) <= 300


def test_a_healthy_clock_is_silent(monkeypatch, pi):
    sent = run_ticks(monkeypatch, pi, [healthy, healthy, healthy])
    assert sent == []


# ---------- more than one camera ----------

@pytest.fixture
def fleet(monkeypatch):
    """Two cameras behind one monitor. Single-camera tests cannot distinguish a
    per-host finding key from a constant one, nor exercise the clock fan-out."""
    pis = {"exitcama.local": FakePi(), "exitcamb.local": FakePi()}

    def ssh(target, cmd, timeout):
        destination = sc._as_ssh_target(target).destination
        return pis[destination.split("@")[-1]].ssh(target, cmd, timeout)

    cfg = sc.config
    monkeypatch.setattr(cfg, "systemcheck_cameras",
                        [{"hostname": h, "type": "exitcam"} for h in pis], raising=False)
    for name, value in [
        ("systemcheck_ping_hosts", []), ("systemcheck_process_hosts", []),
        ("systemcheck_temploggers", []), ("systemcheck_transfer_hosts", []),
        ("systemcheck_trigger_monitor_configs", []),
        ("systemcheck_fast_interval_minutes", TICK_MINUTES),
        ("systemcheck_max_clock_skew_seconds", 60),
        ("systemcheck_remediation_enabled", True),
        ("systemcheck_remediation_cooldown_minutes", 60),
        ("systemcheck_remediation_max_attempts", 3),
        ("systemcheck_remediation_hourly_only", False),
    ]:
        monkeypatch.setattr(cfg, name, value, raising=False)

    monkeypatch.setattr(sc, "_ssh_run", ssh)
    monkeypatch.setattr(sc, "check_ping", _fake_ping(lambda h: pis[h].reachable))
    return pis


def test_only_the_broken_camera_is_named_and_restarted(monkeypatch, fleet):
    a, b = fleet["exitcama.local"], fleet["exitcamb.local"]

    def wedge_a(_):
        a.heartbeat_age = 200

    sent = run_ticks(monkeypatch, list(fleet.values()),
                     [wedge_a, lambda _: None, lambda _: None])
    assert "exitcama.local" in sent[0][1]
    assert "exitcamb.local" not in sent[0][1]
    assert (a.kills, b.kills) == (1, 0)


def test_blips_on_different_cameras_do_not_confirm_each_other(monkeypatch, fleet):
    """Two unrelated one-tick blips must stay silent. If findings were keyed by
    anything less specific than the host, camera B's blip would 'confirm' camera A's
    and alert after a single tick each — reintroducing the false positives."""
    a, b = fleet["exitcama.local"], fleet["exitcamb.local"]

    def down_a(_):
        a.reachable, b.reachable = False, True

    def down_b(_):
        a.reachable, b.reachable = True, False

    def all_up(_):
        a.reachable = b.reachable = True

    sent = run_ticks(monkeypatch, list(fleet.values()), [down_a, down_b, all_up])
    assert sent == []


def test_a_fleet_wide_outage_produces_one_message_not_one_per_camera(monkeypatch, fleet):
    """The reported symptom: every camera 'Cannot reach' at once, when in fact the
    monitor host's WiFi had dropped. Blame the one machine they have in common."""
    def all_down(_):
        for p in fleet.values():
            p.reachable = False

    sent = run_ticks(monkeypatch, list(fleet.values()), [all_down, all_down])
    assert len(sent) == 1, sent
    message = sent[0][1]
    assert "Monitor host cannot reach any of its 2 devices" in message
    assert "exitcama.local" not in message and "exitcamb.local" not in message


def test_a_fleet_wide_outage_still_needs_two_ticks(monkeypatch, fleet):
    def all_down(_):
        for p in fleet.values():
            p.reachable = False

    def all_up(_):
        for p in fleet.values():
            p.reachable = True

    assert run_ticks(monkeypatch, list(fleet.values()), [all_down, all_up, all_up]) == []


def test_stalled_lookups_collapse_even_though_a_local_host_still_answers(monkeypatch, fleet):
    """Reproduces the 12:34 alert: both cameras' lookups stalled, but a templogger
    running on the monitor itself answered its own ping. The old "every pinged host
    must fail" rule was vetoed by that one reachable host, so eight identical lines
    went out. A stalled lookup is monitor-side on its own evidence."""
    monkeypatch.setattr(sc.config, "systemcheck_temploggers",
                        [{"hostname": "thria", "csv_glob": "/tmp/t_*.csv"}], raising=False)

    def stall_cameras(_):
        for p in fleet.values():
            p.reachable = False

    # cameras stall (monitor-side); "thria" resolves locally and answers.
    monkeypatch.setattr(sc, "check_ping", _fake_ping(
        lambda h: h == "thria", monitor_side=True))
    # the templogger's own SSH checks pass
    monkeypatch.setattr(sc, "check_remote_service", lambda *a, **k: (True, None))
    monkeypatch.setattr(sc, "check_remote_csv_freshness", lambda *a, **k: (True, None))
    monkeypatch.setattr(sc, "check_remote_clock", lambda *a, **k: None)

    sent = run_ticks(monkeypatch, list(fleet.values()), [stall_cameras, stall_cameras])
    assert len(sent) == 1, sent
    message = sent[0][1]
    assert "could not resolve or reach 2 of its devices" in message
    assert "exitcama.local" not in message and "exitcamb.local" not in message


def test_a_fleet_wide_skew_collapses_to_one_monitor_clock_message(monkeypatch, fleet):
    """If every device disagrees with us in the same direction, the wrong clock is
    ours. Report it once, not once per camera."""
    def skew_all(_):
        for p in fleet.values():
            p.clock_offset = 300

    sent = run_ticks(monkeypatch, list(fleet.values()),
                     [skew_all, lambda _: None, lambda _: None])
    message = sent[0][1]
    assert "Monitor host clock may be wrong" in message
    assert "all 2 devices" in message
    assert "clock off by" not in message, "must not also blame each device"


def test_one_skewed_device_among_many_is_blamed_on_that_device(monkeypatch, fleet):
    def skew_one(_):
        fleet["exitcamb.local"].clock_offset = 300

    sent = run_ticks(monkeypatch, list(fleet.values()),
                     [skew_one, lambda _: None, lambda _: None])
    message = sent[0][1]
    assert "exitcamb.local: clock off by" in message
    assert "Monitor host clock" not in message
