from datetime import datetime, timedelta, timezone

import probe
from probe import OutageState, compute_durations, update_state


def dt(n: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=n)


def test_outage_starts_after_three_failures(monkeypatch):
    monkeypatch.setattr("probe.get_connection_type_if_outage", lambda in_outage: "unknown")

    s = OutageState()
    s = update_state(s, ping_ok=False, now_utc=dt(0), http_timeout_trigger=False)
    assert not s.dsl_event_active
    s = update_state(s, ping_ok=False, now_utc=dt(1), http_timeout_trigger=False)
    assert not s.dsl_event_active
    s = update_state(s, ping_ok=False, now_utc=dt(2), http_timeout_trigger=False)
    assert s.dsl_event_active
    assert s.dsl_event_start_utc == dt(2)
    assert s.dsl_event_trigger == "ping_failures"


def test_failure_threshold_1_triggers_on_first_failed_ping(monkeypatch):
    """DSL_MONITOR_FAILURE_THRESHOLD=1 must start an event on the first failed ping."""
    monkeypatch.setattr("probe.get_connection_type_if_outage", lambda in_outage: "unknown")
    monkeypatch.setattr(probe, "CONSECUTIVE_FAILURES_THRESHOLD", 1)

    s = OutageState()
    s = update_state(s, ping_ok=False, now_utc=dt(0), http_timeout_trigger=False)
    assert s.dsl_event_active
    assert s.dsl_event_start_utc == dt(0)
    assert s.dsl_event_trigger == "ping_failures"


def test_outage_ends_on_first_success(monkeypatch):
    # End condition depends on Fritz reporting 'dsl'.
    # So we simulate: during event -> 'mobile', then on recovery tick -> 'dsl'.
    seq = iter(["mobile", "dsl"])

    def fake_conn(_in_outage: bool):
        try:
            return next(seq)
        except StopIteration:
            return "dsl"

    monkeypatch.setattr("probe.get_connection_type_if_outage", fake_conn)

    s = OutageState()
    s = update_state(s, ping_ok=False, now_utc=dt(0), http_timeout_trigger=False)
    s = update_state(s, ping_ok=False, now_utc=dt(1), http_timeout_trigger=False)
    s = update_state(s, ping_ok=False, now_utc=dt(2), http_timeout_trigger=False)
    assert s.dsl_event_active

    s = update_state(s, ping_ok=True, now_utc=dt(3), http_timeout_trigger=False)
    assert not s.dsl_event_active
    assert s.dsl_event_end_reason == "recovered_to_dsl"


def test_fritz_connection_type_is_rate_limited(monkeypatch):
    # Reset module-level cache so the test is deterministic even if other tests ran before.
    monkeypatch.setattr(probe, "_conn_type_last_fetch_mono", 0.0)
    monkeypatch.setattr(probe, "_conn_type_last_value", "unknown")

    # We test the real rate-limit logic by patching get_fritz_status() and time.monotonic().
    calls = {"n": 0}

    def fake_status():
        calls["n"] += 1
        return {"connection_type": "mobile"}

    # monotonic timeline (seconds)
    t = {"v": 0.0}

    def fake_monotonic():
        return t["v"]

    monkeypatch.setattr(probe, "get_fritz_status", fake_status)
    monkeypatch.setattr(probe.time, "monotonic", fake_monotonic)

    # Ensure we have a deterministic 60s interval for the test.
    monkeypatch.setattr(probe, "CONN_TYPE_POLL_INTERVAL_SECONDS", 60)

    s = OutageState()

    # Two failures: no outage -> no Fritz poll
    update_state(s, ping_ok=False, now_utc=dt(0), http_timeout_trigger=False)
    update_state(s, ping_ok=False, now_utc=dt(1), http_timeout_trigger=False)
    assert calls["n"] == 0

    # Third failure: event starts -> first poll
    # Use monotonic>interval so a fetch is guaranteed even if last_fetch was 0.0.
    t["v"] = 61.0
    s = update_state(s, ping_ok=False, now_utc=dt(2), http_timeout_trigger=False)
    assert s.dsl_event_active
    assert s.mobile_start_utc == dt(2)
    assert calls["n"] == 1

    # Still in event, but within 60s -> MUST NOT poll again
    t["v"] = 70.0
    s = update_state(s, ping_ok=False, now_utc=dt(10), http_timeout_trigger=False)
    outage_dur, mobile_dur = compute_durations(s, now_utc=dt(10))
    assert outage_dur == 8.0
    assert mobile_dur == 8.0
    assert calls["n"] == 1

    # After 60s -> poll allowed
    t["v"] = 130.0
    s = update_state(s, ping_ok=False, now_utc=dt(70), http_timeout_trigger=False)
    assert calls["n"] == 2

    # End outage -> should stop polling (and stop event if Fritz says dsl; here we just ensure no crash)
    t["v"] = 131.0
    s = update_state(s, ping_ok=True, now_utc=dt(71), http_timeout_trigger=False)


def test_dsl_event_stays_active_while_pings_fail_even_if_fritz_reports_dsl(monkeypatch):
    """Regression: a 10min outage was not recorded because Fritz kept reporting
    'dsl' (DSL sync up, but internet routing broken) and the old code ended
    the event immediately.

    The event must remain active as long as pings keep failing, regardless of
    what Fritz reports.
    """
    monkeypatch.setattr("probe.get_connection_type_if_outage", lambda in_outage: "dsl")

    s = OutageState()
    # 3 failures to start the event
    s = update_state(s, ping_ok=False, now_utc=dt(0), http_timeout_trigger=False)
    s = update_state(s, ping_ok=False, now_utc=dt(10), http_timeout_trigger=False)
    s = update_state(s, ping_ok=False, now_utc=dt(20), http_timeout_trigger=False)
    assert s.dsl_event_active
    assert s.dsl_event_trigger == "ping_failures"

    # Continue failing for 10 minutes (600s) – Fritz says "dsl" throughout
    for i in range(4, 64):  # ~60 more ticks at 10s intervals = 600s
        s = update_state(s, ping_ok=False, now_utc=dt(i * 10), http_timeout_trigger=False)
        assert s.dsl_event_active, f"Event ended prematurely at tick {i} (t={i*10}s)"

    # Finally, pings recover → event should end because Fritz says "dsl"
    s = update_state(s, ping_ok=True, now_utc=dt(640), http_timeout_trigger=False)
    assert not s.dsl_event_active
    assert s.dsl_event_end_reason == "recovered_to_dsl"


def test_dsl_event_stays_active_while_pings_fail_fritz_reports_mobile(monkeypatch):
    """While pings fail and Fritz reports 'mobile', the event must stay active."""
    monkeypatch.setattr("probe.get_connection_type_if_outage", lambda in_outage: "mobile")

    s = OutageState()
    s = update_state(s, ping_ok=False, now_utc=dt(0), http_timeout_trigger=False)
    s = update_state(s, ping_ok=False, now_utc=dt(10), http_timeout_trigger=False)
    s = update_state(s, ping_ok=False, now_utc=dt(20), http_timeout_trigger=False)
    assert s.dsl_event_active

    for i in range(4, 20):
        s = update_state(s, ping_ok=False, now_utc=dt(i * 10), http_timeout_trigger=False)
        assert s.dsl_event_active
        assert s.mobile_start_utc is not None


def test_high_latency_ping_triggers_dsl_event_immediately(monkeypatch):
    """Production incident 2026-02-28: FritzBox switched to mobile fallback.
    Pings succeeded but with 158ms latency (normally ~16ms).  The program
    saw ping_ok=1 and did nothing.

    High-latency pings (>100ms) must be treated as failures and immediately
    start a DSL event (no 3-fail threshold needed – a single high-latency
    ping is conclusive evidence of mobile routing).
    """
    monkeypatch.setattr("probe.get_connection_type_if_outage", lambda in_outage: "mobile")
    monkeypatch.setattr("probe.PING_LATENCY_THRESHOLD_MS", 100.0)

    s = OutageState()

    # Normal ping – no event
    s = update_state(s, ping_ok=True, now_utc=dt(0), latency_ms=16.0)
    assert not s.dsl_event_active

    # Suddenly high latency (158ms) – mobile fallback!
    s = update_state(s, ping_ok=True, now_utc=dt(10), latency_ms=158.0)
    assert s.dsl_event_active
    assert s.dsl_event_trigger == "high_latency"
    assert s.in_outage

    # Continues with high latency – event stays active
    s = update_state(s, ping_ok=True, now_utc=dt(20), latency_ms=160.0)
    assert s.dsl_event_active

    s = update_state(s, ping_ok=True, now_utc=dt(30), latency_ms=155.0)
    assert s.dsl_event_active


def test_high_latency_event_ends_when_latency_normalizes_and_fritz_dsl(monkeypatch):
    """After high latency clears and Fritz reports DSL, event should end."""
    seq = iter(["mobile", "mobile", "mobile", "dsl"])

    def fake_conn(_in_outage: bool):
        try:
            return next(seq)
        except StopIteration:
            return "dsl"

    monkeypatch.setattr("probe.get_connection_type_if_outage", fake_conn)
    monkeypatch.setattr("probe.PING_LATENCY_THRESHOLD_MS", 100.0)

    s = OutageState()

    # High latency starts event
    s = update_state(s, ping_ok=True, now_utc=dt(0), latency_ms=158.0)
    assert s.dsl_event_active
    assert s.dsl_event_trigger == "high_latency"

    # Still high latency
    s = update_state(s, ping_ok=True, now_utc=dt(10), latency_ms=150.0)
    assert s.dsl_event_active

    # Still high latency
    s = update_state(s, ping_ok=True, now_utc=dt(20), latency_ms=140.0)
    assert s.dsl_event_active

    # Latency normalizes AND Fritz says "dsl" → event ends
    s = update_state(s, ping_ok=True, now_utc=dt(30), latency_ms=15.0)
    assert not s.dsl_event_active
    assert s.dsl_event_end_reason == "recovered_to_dsl"


def test_normal_latency_no_false_positives(monkeypatch):
    """Normal latencies (even slightly elevated) must not trigger events."""
    monkeypatch.setattr("probe.get_connection_type_if_outage", lambda in_outage: "unknown")
    monkeypatch.setattr("probe.PING_LATENCY_THRESHOLD_MS", 100.0)

    s = OutageState()
    for ms in [15.0, 20.0, 50.0, 85.0, 99.0, 100.0]:
        s = update_state(s, ping_ok=True, now_utc=dt(int(ms)), latency_ms=ms)
        assert not s.dsl_event_active, f"False positive at {ms}ms"


def test_high_latency_disabled_when_threshold_zero(monkeypatch):
    """When threshold is 0, high-latency detection is disabled."""
    monkeypatch.setattr("probe.get_connection_type_if_outage", lambda in_outage: "unknown")
    monkeypatch.setattr("probe.PING_LATENCY_THRESHOLD_MS", 0.0)

    s = OutageState()
    s = update_state(s, ping_ok=True, now_utc=dt(0), latency_ms=500.0)
    assert not s.dsl_event_active


def test_fritz_mobile_trigger_starts_dsl_event(monkeypatch):
    """When periodic Fritz check detects 'mobile', a DSL event starts immediately.

    Pings are OK (mobile routing works), so in_outage (ping-level) stays False.
    The DSL event is active and mobile_start_utc is tracked.
    """
    monkeypatch.setattr("probe.get_connection_type_if_outage", lambda in_outage: "mobile")

    s = OutageState()

    # Normal pings are fine, but Fritz reports mobile
    s = update_state(s, ping_ok=True, now_utc=dt(0), fritz_mobile_trigger=True)
    assert s.dsl_event_active
    assert s.dsl_event_trigger == "fritz_mobile"
    assert s.last_connection_type == "mobile"
    assert s.mobile_start_utc == dt(0)
    # Ping is OK so in_outage should NOT be set
    assert not s.in_outage


def test_fritz_mobile_event_ends_when_fritz_dsl(monkeypatch):
    """fritz_mobile event ends when pings OK and Fritz reports dsl."""
    seq = iter(["mobile", "dsl"])

    def fake_conn(_in_outage: bool):
        try:
            return next(seq)
        except StopIteration:
            return "dsl"

    monkeypatch.setattr("probe.get_connection_type_if_outage", fake_conn)

    s = OutageState()
    s = update_state(s, ping_ok=True, now_utc=dt(0), fritz_mobile_trigger=True)
    assert s.dsl_event_active

    # Ping OK + Fritz says dsl → event ends
    s = update_state(s, ping_ok=True, now_utc=dt(60))
    assert not s.dsl_event_active
    assert s.dsl_event_end_reason == "recovered_to_dsl"


def test_normal_mode_polls_fritz_every_20min(monkeypatch):
    """In normal mode, Fritz is polled every 20min (not every tick)."""
    monkeypatch.setattr(probe, "_conn_type_last_fetch_mono", 0.0)
    monkeypatch.setattr(probe, "_conn_type_last_value", "unknown")

    calls = {"n": 0}

    def fake_status():
        calls["n"] += 1
        return {"connection_type": "dsl"}

    t = {"v": 0.0}
    monkeypatch.setattr(probe, "get_fritz_status", fake_status)
    monkeypatch.setattr(probe.time, "monotonic", lambda: t["v"])
    monkeypatch.setattr(probe, "CONN_TYPE_NORMAL_POLL_INTERVAL_SECONDS", 1200)
    monkeypatch.setattr(probe, "CONN_TYPE_POLL_INTERVAL_SECONDS", 60)

    # First call at t=0 → fetch (since last_fetch=0.0, delta=0, but 0<1200 → no fetch)
    # Actually 0.0 - 0.0 = 0.0 < 1200 → cached. Need t > 1200.
    t["v"] = 1201.0
    from probe import get_connection_type_if_outage
    ct = get_connection_type_if_outage(False)
    assert calls["n"] == 1
    assert ct == "dsl"

    # Within 20min → no new fetch
    t["v"] = 1500.0
    ct = get_connection_type_if_outage(False)
    assert calls["n"] == 1  # still cached

    # After another 20min → new fetch
    t["v"] = 2402.0
    ct = get_connection_type_if_outage(False)
    assert calls["n"] == 2


def test_event_mode_polls_fritz_every_60s(monkeypatch):
    """During an event, Fritz is polled every 60s (faster than normal mode)."""
    monkeypatch.setattr(probe, "_conn_type_last_fetch_mono", 0.0)
    monkeypatch.setattr(probe, "_conn_type_last_value", "unknown")

    calls = {"n": 0}

    def fake_status():
        calls["n"] += 1
        return {"connection_type": "mobile"}

    t = {"v": 61.0}
    monkeypatch.setattr(probe, "get_fritz_status", fake_status)
    monkeypatch.setattr(probe.time, "monotonic", lambda: t["v"])
    monkeypatch.setattr(probe, "CONN_TYPE_POLL_INTERVAL_SECONDS", 60)

    from probe import get_connection_type_if_outage
    ct = get_connection_type_if_outage(True)  # in_outage=True → 60s rate
    assert calls["n"] == 1

    # Within 60s → cached
    t["v"] = 100.0
    ct = get_connection_type_if_outage(True)
    assert calls["n"] == 1

    # After 60s → new fetch
    t["v"] = 122.0
    ct = get_connection_type_if_outage(True)
    assert calls["n"] == 2


def test_trigger_and_end_reason_persist_in_state_after_event(monkeypatch):
    """After an event ends, trigger/end_reason remain in OutageState (for the
    CSV closing row).  The main loop is responsible for clearing them after
    writing.  This test documents the state machine behaviour."""
    monkeypatch.setattr("probe.get_connection_type_if_outage", lambda _: "dsl")
    monkeypatch.setattr("probe.PING_LATENCY_THRESHOLD_MS", 100.0)

    s = OutageState()

    # High-latency starts event
    s = update_state(s, ping_ok=True, now_utc=dt(0), latency_ms=158.0)
    assert s.dsl_event_active
    assert s.dsl_event_trigger == "high_latency"
    assert s.dsl_event_end_reason == ""

    # Normal latency + Fritz=dsl → event ends, end_reason set
    s = update_state(s, ping_ok=True, now_utc=dt(10), latency_ms=15.0)
    assert not s.dsl_event_active
    assert s.dsl_event_trigger == "high_latency"
    assert s.dsl_event_end_reason == "recovered_to_dsl"

    # Simulate main-loop clearing (as the new code does after CSV write)
    s.dsl_event_end_reason = ""
    s.dsl_event_trigger = ""

    # Subsequent ticks must have clean fields
    s = update_state(s, ping_ok=True, now_utc=dt(20), latency_ms=15.0)
    assert not s.dsl_event_active
    assert s.dsl_event_trigger == ""
    assert s.dsl_event_end_reason == ""
