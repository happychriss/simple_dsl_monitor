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
