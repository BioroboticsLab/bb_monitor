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
    clock_findings,
    clock_skew,
    confirm,
    parse_heartbeat,
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
