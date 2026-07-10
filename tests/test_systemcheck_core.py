"""Tests for the system check's decision logic.

These exercise the cases that motivated the two-tick confirmation and the clock
check: a transient blip must never be reported, a real fault must never be lost,
and a slow SSH round trip must never look like clock skew.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.systemcheck_core import (  # noqa: E402
    MONITOR_HOST,
    Finding,
    HostAddresses,
    PingSettings,
    clock_findings,
    clock_skew,
    collapse_unreachable,
    confirm,
    parse_heartbeat,
    parse_ping_address,
    ping_targets,
)


def heartbeat(host="exitcamd.local", message="stale"):
    return Finding(host, "heartbeat", message, remediable=True, ssh_target=f"pi@{host}")


def ping(host="feedercama.local"):
    return Finding(host, "ping", f"Cannot reach {host}")


# ---------- confirm() ----------

def test_single_tick_blip_is_never_reported():
    pending = set()
    confirmed, pending = confirm(pending, [ping()])
    assert confirmed == []          # tick 1: seen once, stays quiet
    confirmed, pending = confirm(pending, [])
    assert confirmed == []          # tick 2: gone, nothing was ever said
    assert pending == set()


def test_issue_on_two_consecutive_ticks_is_reported():
    confirmed, pending = confirm(set(), [heartbeat()])
    assert confirmed == []
    confirmed, pending = confirm(pending, [heartbeat()])
    assert [f.key for f in confirmed] == [("exitcamd.local", "heartbeat")]


def test_key_is_stable_while_the_message_changes():
    """The age in a stale-heartbeat message grows every tick. Matching on message
    text would re-arm the counter forever and nothing would ever be reported."""
    confirmed, pending = confirm(set(), [heartbeat(message="stale (166s old, max 30s)")])
    assert confirmed == []
    confirmed, pending = confirm(pending, [heartbeat(message="stale (767s old, max 30s)")])
    assert len(confirmed) == 1
    assert "767s" in confirmed[0].message      # report the fresh detail...
    assert confirmed[0].key == ("exitcamd.local", "heartbeat")   # ...matched by key


def test_reappearing_issue_must_be_confirmed_again():
    _, pending = confirm(set(), [heartbeat()])
    confirmed, pending = confirm(pending, [heartbeat()])
    assert len(confirmed) == 1
    confirmed, pending = confirm(pending, [])          # clears
    assert confirmed == []
    confirmed, pending = confirm(pending, [heartbeat()])   # comes back
    assert confirmed == [], "a re-appearing issue starts the two-tick count over"


def test_distinct_probes_on_one_host_confirm_independently():
    _, pending = confirm(set(), [heartbeat(), ping("exitcamd.local")])
    confirmed, pending = confirm(pending, [heartbeat()])
    assert [f.kind for f in confirmed] == ["heartbeat"]


def test_confirmed_findings_carry_the_current_tick_payload():
    """Remediation reads .remediable and .ssh_target off the confirmed finding, so
    it must be this tick's object, not the stale one the key was first seen on."""
    stale = Finding("h", "heartbeat", "stale", remediable=True, ssh_target="pi@h")
    future = Finding("h", "heartbeat", "clock stepped", remediable=False, ssh_target="pi@h")
    _, pending = confirm(set(), [stale])
    confirmed, _ = confirm(pending, [future])
    assert confirmed[0].remediable is False


# ---------- the recovery rule ----------

def test_a_ping_blip_must_not_look_like_recovery():
    """The bug this rule exists to prevent: tick 3's ping failure short-circuits the
    camera's checks, so the wedged camera's heartbeat key disappears from `found`.
    A "no confirmed findings -> recover" rule would post 'All systems OK' about a
    camera that is still wedged. Recovery therefore requires an *empty* found set.
    """
    _, pending = confirm(set(), [heartbeat()])
    confirmed, pending = confirm(pending, [heartbeat()])
    assert confirmed, "tick 2: alerted"

    # tick 3: transient ping failure; _camera_checks returns early, heartbeat unprobed
    confirmed, pending = confirm(pending, [ping("exitcamd.local")])
    found_keys = {("exitcamd.local", "ping")}
    assert confirmed == []
    assert found_keys, "not empty -> no recovery message is sent"


def test_recovery_requires_a_completely_clean_tick():
    _, pending = confirm(set(), [heartbeat()])
    confirmed, pending = confirm(pending, [heartbeat()])
    assert confirmed
    confirmed, pending = confirm(pending, [])
    assert confirmed == [] and pending == set()   # only now may "All systems OK" go out


# ---------- clock_skew() ----------

def test_perfectly_synced_clock_has_zero_skew():
    skew, offset = clock_skew(t0=1000.0, remote_epoch=1000, t1=1000.4)
    assert skew == 0.0
    assert offset == pytest.approx(-0.2, abs=0.01)


@pytest.mark.parametrize("rtt", [0.1, 1.0, 5.0, 20.0])
def test_a_slow_round_trip_can_never_manufacture_skew(rtt):
    """The whole point of the RTT bound: a 20-second SSH must not read as 20s of
    clock error against a 60s threshold."""
    t0 = 1_000_000.0
    remote = int(t0 + rtt / 2)     # remote clock is right; it answered mid-flight
    skew, _ = clock_skew(t0, remote, t0 + rtt)
    assert skew <= 1.0
    assert skew < 60


def test_slow_round_trip_only_shrinks_a_real_skew():
    """A device 300s ahead still reports ~300s, minus at most the RTT — never more."""
    t0 = 1_000_000.0
    skew, offset = clock_skew(t0, remote_epoch=int(t0 + 300), t1=t0 + 20.0)
    assert 280 <= skew <= 300
    assert offset > 0     # remote is ahead of us


def test_skew_is_signed_correctly_when_remote_is_behind():
    t0 = 1_000_000.0
    skew, offset = clock_skew(t0, remote_epoch=int(t0 - 300), t1=t0 + 1.0)
    assert skew == pytest.approx(300, abs=1)
    assert offset < 0     # remote is behind us


# ---------- clock_findings() ----------

def test_healthy_fleet_produces_no_findings():
    samples = [("a", 0.0, 0.1), ("b", 2.0, -2.0)]
    assert clock_findings(samples, max_skew_seconds=60) == []


def test_one_bad_device_is_blamed_on_the_device():
    samples = [("a", 0.0, 0.1), ("b", 0.0, -0.2), ("exitcamd", 137.0, 137.0)]
    findings = clock_findings(samples, max_skew_seconds=60)
    assert [f.key for f in findings] == [("exitcamd", "clock")]
    assert "clock off by 137s" in findings[0].message


def test_whole_fleet_skewed_the_same_way_blames_the_monitor():
    samples = [("a", 137.0, 137.2), ("b", 136.0, 136.5), ("c", 138.0, 138.1)]
    findings = clock_findings(samples, max_skew_seconds=60)
    assert len(findings) == 1
    assert findings[0].host == MONITOR_HOST
    assert "Monitor host clock may be wrong" in findings[0].message
    assert "all 3 devices" in findings[0].message


def test_fleet_skewed_in_opposite_directions_blames_the_devices():
    """Opposite signs cannot be explained by one wrong monitor clock."""
    samples = [("a", 137.0, 137.0), ("b", 200.0, -200.0)]
    findings = clock_findings(samples, max_skew_seconds=60)
    assert {f.host for f in findings} == {"a", "b"}


def test_a_lone_skewed_device_is_not_blamed_on_the_monitor():
    findings = clock_findings([("a", 137.0, 137.0)], max_skew_seconds=60)
    assert findings[0].host == "a"


def test_monitor_is_not_blamed_when_some_devices_are_fine():
    samples = [("a", 137.0, 137.0), ("b", 137.0, 137.0), ("c", 0.0, 0.0)]
    findings = clock_findings(samples, max_skew_seconds=60)
    assert {f.host for f in findings} == {"a", "b"}


# ---------- PingSettings ----------

def test_default_retries_outlast_an_observed_wifi_dropout():
    """thria's wlp3s0 left and rejoined the mDNS group over ~9s (avahi journal,
    10:26:37 -> 10:26:46). During that window ping fails instantly, so only the
    delays between attempts cover it — the timeouts contribute nothing."""
    assert PingSettings().retry_span_seconds() >= 10


def test_retry_span_ignores_the_timeout_because_a_dead_resolver_fails_instantly():
    fast_failing = PingSettings(timeout_seconds=60, attempts=2, retry_delay_seconds=1)
    assert fast_failing.retry_span_seconds() == 1, "a huge timeout buys no dropout coverage"


def test_a_single_attempt_spans_nothing():
    assert PingSettings(attempts=1).retry_span_seconds() == 0


def test_worst_case_bounds_the_time_spent_on_one_unreachable_host():
    p = PingSettings(timeout_seconds=2, attempts=3, retry_delay_seconds=5)
    assert p.worst_case_seconds() == 3 * (2 + 3) + 2 * 5


# ---------- parse_ping_address() ----------

def test_parses_the_address_iputils_prints():
    line = "PING feedercama.local (192.168.178.52) 56(84) bytes of data."
    assert parse_ping_address(line) == "192.168.178.52"


def test_parses_the_address_macos_prints():
    line = "PING feedercama.local (192.168.178.52): 56 data bytes"
    assert parse_ping_address(line) == "192.168.178.52"


def test_the_byte_counts_are_not_mistaken_for_an_address():
    """`56(84)` sits in parentheses too; four dotted octets are required."""
    assert parse_ping_address("PING host (56(84)) bytes") is None


def test_an_ipv6_ping_yields_no_ipv4_address():
    assert parse_ping_address("PING6(56=40+8+8 bytes) fe80::1 --> fe80::2") is None


def test_an_out_of_range_octet_is_rejected():
    assert parse_ping_address("PING x (999.1.1.1) 56 bytes") is None


@pytest.mark.parametrize("stdout", ["", None, "no address here"])
def test_no_address_to_parse(stdout):
    assert parse_ping_address(stdout) is None


# ---------- ping_targets() ----------

def test_without_a_cached_address_every_attempt_uses_the_name():
    assert ping_targets("cam.local", None, 3) == ["cam.local"] * 3


def test_with_a_cached_address_only_the_last_attempt_pays_for_dns():
    assert ping_targets("cam.local", "10.0.0.5", 3) == ["10.0.0.5", "10.0.0.5", "cam.local"]


def test_the_final_attempt_by_name_finds_a_moved_dhcp_lease_in_the_same_tick():
    targets = ping_targets("cam.local", "10.0.0.5", 2)
    assert targets[-1] == "cam.local"
    assert targets[0] == "10.0.0.5"


def test_a_single_attempt_still_gets_a_name_fallback():
    """Otherwise a stale address could never be corrected within a tick."""
    assert ping_targets("cam.local", "10.0.0.5", 1) == ["10.0.0.5", "cam.local"]


def test_attempts_are_clamped_to_at_least_one():
    assert ping_targets("cam.local", None, 0) == ["cam.local"]


# ---------- HostAddresses ----------

def test_an_address_is_remembered_and_returned():
    addresses = HostAddresses()
    addresses.remember("cam.local", "10.0.0.5")
    assert addresses.get("cam.local") == "10.0.0.5"


def test_forgetting_an_address_returns_it_and_clears_it():
    addresses = HostAddresses()
    addresses.remember("cam.local", "10.0.0.5")
    assert addresses.forget("cam.local") == "10.0.0.5"
    assert addresses.get("cam.local") is None


def test_forgetting_an_unknown_host_is_harmless():
    assert HostAddresses().forget("nobody.local") is None


def test_a_disabled_cache_stores_nothing():
    addresses = HostAddresses(enabled=False)
    addresses.remember("cam.local", "10.0.0.5")
    assert addresses.get("cam.local") is None


def test_a_disabled_cache_answers_nothing_even_if_it_holds_an_address():
    """Turning the knob off must take effect immediately, not once the entries age
    out — that is the whole point of having a kill switch."""
    addresses = HostAddresses()
    addresses.remember("cam.local", "10.0.0.5")
    addresses.enabled = False
    assert addresses.get("cam.local") is None


def test_every_change_is_logged_so_the_cache_is_never_a_mystery():
    lines = []
    addresses = HostAddresses(log=lines.append)
    addresses.remember("cam.local", "10.0.0.5")
    addresses.remember("cam.local", "10.0.0.5")     # unchanged: no new noise
    addresses.remember("cam.local", "10.0.0.9")     # moved lease
    addresses.forget("cam.local")
    assert len(lines) == 3
    assert "cam.local -> 10.0.0.5" in lines[0]
    assert "cam.local -> 10.0.0.9" in lines[1]
    assert "dropped cam.local (was 10.0.0.9)" in lines[2]


def test_remembering_nothing_is_a_no_op():
    addresses = HostAddresses()
    addresses.remember("cam.local", None)
    assert addresses.get("cam.local") is None


# ---------- collapse_unreachable() ----------

def unreachable(host, reason="no reply within 2s"):
    return Finding(host, "ping", f"Cannot reach {host} ({reason})", reason=reason)


def stalled(host, reason="ping did not return within 7s (name resolution stalled?)"):
    """A lookup that stalled or failed: monitor-side by construction."""
    return Finding(host, "ping", f"Cannot reach {host} ({reason})",
                   monitor_side=True, reason=reason)


def test_a_whole_fleet_silent_points_at_the_access_point_not_the_monitor():
    """Every device silent while our own resolver is fine means the shared thing is
    down. The last time this happened it was the router. An alert that says "check
    this host's network" would send a tired scientist to the wrong box."""
    hosts = [f"cam{i}.local" for i in range(8)]
    findings = collapse_unreachable([unreachable(h) for h in hosts], hosts_pinged=8)
    assert len(findings) == 1
    assert findings[0].key == (MONITOR_HOST, "network")
    assert "None of the 8 devices answer" in findings[0].message
    assert "access point / router" in findings[0].message
    # and it must still say WHICH hosts, so a partial outage is distinguishable
    assert "cam0.local" in findings[0].message and "cam7.local" in findings[0].message


def test_resolver_stalls_collapse_even_when_another_host_answers():
    """The bug seen in production: a templogger running *on the monitor* always
    answers its own ping, so `unreachable == hosts_pinged` never held and eight
    identical resolver-stall lines went out every tick. A stalled lookup happens on
    this machine, so it needs no help from the other hosts to indict it."""
    cams = [stalled(f"cam{i}.local") for i in range(8)]
    findings = collapse_unreachable(cams, hosts_pinged=9)   # 9th host was reachable
    assert len(findings) == 1
    assert findings[0].key == (MONITOR_HOST, "network")
    assert "8 of its devices" in findings[0].message
    assert "name resolution stalled" in findings[0].message


def test_a_resolver_stall_collapse_keeps_unrelated_findings():
    findings = collapse_unreachable(
        [stalled("cam0.local"), stalled("cam1.local"),
         Finding("thria", "proc:bb_imgacquisition", "count=0, expected >=4")],
        hosts_pinged=9)
    assert {f.kind for f in findings} == {"network", "proc:bb_imgacquisition"}


def test_a_genuinely_silent_host_is_not_swept_into_the_monitor_finding():
    """Two stalled lookups plus one camera that is really down: the down camera must
    keep its own line, or a real outage hides behind a resolver complaint."""
    findings = collapse_unreachable(
        [stalled("cam0.local"), stalled("cam1.local"), unreachable("cam2.local")],
        hosts_pinged=9)
    keys = {f.key for f in findings}
    assert (MONITOR_HOST, "network") in keys
    assert ("cam2.local", "ping") in keys


def test_a_single_resolver_stall_is_not_enough_to_blame_the_monitor():
    """One host failing to resolve may simply have been renamed or removed."""
    findings = collapse_unreachable([stalled("cam0.local")], hosts_pinged=9)
    assert findings[0].key == ("cam0.local", "ping")


def test_the_collapsed_message_carries_the_underlying_reason():
    findings = collapse_unreachable(
        [unreachable("a.local", "Temporary failure in name resolution"),
         unreachable("b.local", "Temporary failure in name resolution")],
        hosts_pinged=2)
    assert "Temporary failure in name resolution" in findings[0].message


def test_a_reason_containing_parentheses_survives_the_collapse():
    """Regression: the reason used to be recovered by slicing the rendered message
    at its first "(" and rstripping ")", which ate the closing paren of reasons that
    contain their own — the alert read "...(name resolution stalled?" with no close."""
    reason = "ping did not return within 7s (name resolution stalled?)"
    findings = collapse_unreachable([stalled("a.local", reason), stalled("b.local", reason)],
                                    hosts_pinged=2)
    assert findings[0].message.endswith(reason)
    assert findings[0].message.count("(") == findings[0].message.count(")")


def test_one_dead_camera_among_many_is_still_blamed_on_that_camera():
    findings = collapse_unreachable([unreachable("cam3.local")], hosts_pinged=8)
    assert [f.key for f in findings] == [("cam3.local", "ping")]


def test_a_lone_configured_host_is_never_blamed_on_the_monitor():
    """With one camera, "everything is unreachable" carries no information."""
    findings = collapse_unreachable([unreachable("cam0.local")], hosts_pinged=1)
    assert findings[0].key == ("cam0.local", "ping")


def test_collapse_preserves_non_ping_findings():
    findings = collapse_unreachable(
        [unreachable("a.local"), unreachable("b.local"),
         Finding("c.local", "heartbeat", "stale")],
        hosts_pinged=2)
    kinds = {f.kind for f in findings}
    assert kinds == {"network", "heartbeat"}


def test_a_fleet_wide_outage_is_one_key_so_it_confirms_as_one_issue():
    """Two consecutive ticks of total outage => a single alert, not eight."""
    hosts = [f"cam{i}.local" for i in range(8)]
    tick = lambda: collapse_unreachable([unreachable(h) for h in hosts], hosts_pinged=8)
    confirmed, pending = confirm(set(), tick())
    assert confirmed == []
    confirmed, _ = confirm(pending, tick())
    assert len(confirmed) == 1


# ---------- parse_heartbeat() ----------

def test_missing_heartbeat_is_a_domain_state_not_a_transport_error():
    """The old probe ran `stat ... && date +%s`; a missing file short-circuited the
    `&&`, leaving empty stdout and a non-zero exit, which _ssh_failed reported as
    'ssh exec failed (stat: cannot statx ...)'."""
    assert parse_heartbeat("MISSING 1700000000", max_age_seconds=30) == ("missing", None)


def test_fresh_heartbeat_is_ok():
    state, age = parse_heartbeat("OK 1700000000 1700000005", max_age_seconds=30)
    assert (state, age) == ("ok", 5)


def test_stale_heartbeat_reports_its_age():
    state, age = parse_heartbeat("OK 1700000000 1700000767", max_age_seconds=30)
    assert (state, age) == ("stale", 767)


def test_mtime_in_the_future_is_flagged_rather_than_silently_passing():
    """`age > max_age` alone lets a backwards clock step read as healthy."""
    state, age = parse_heartbeat("OK 1700000600 1700000000", max_age_seconds=30)
    assert state == "future"
    assert age == -600


def test_tiny_future_skew_is_tolerated():
    state, _ = parse_heartbeat("OK 1700000002 1700000000", max_age_seconds=30)
    assert state == "ok"


@pytest.mark.parametrize("stdout", ["", "garbage", "OK notanumber 1700000000", "OK 1700000000"])
def test_unparseable_probe_output(stdout):
    state, age = parse_heartbeat(stdout, max_age_seconds=30)
    assert state == "unparseable"
    assert age is None
