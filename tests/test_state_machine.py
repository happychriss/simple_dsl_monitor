from datetime import datetime, timezone

from probe import OutageState, compute_durations, update_state


def dt(n: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, n, tzinfo=timezone.utc)


def test_outage_starts_after_three_failures(monkeypatch):
    monkeypatch.setattr("probe.get_connection_type", lambda: "unknown")

    s = OutageState()
    s = update_state(s, ping_ok=False, now_utc=dt(0))
    assert not s.in_outage
    s = update_state(s, ping_ok=False, now_utc=dt(1))
    assert not s.in_outage
    s = update_state(s, ping_ok=False, now_utc=dt(2))
    assert s.in_outage
    assert s.outage_start_utc == dt(2)


def test_outage_ends_on_first_success(monkeypatch):
    monkeypatch.setattr("probe.get_connection_type", lambda: "unknown")

    s = OutageState()
    s = update_state(s, ping_ok=False, now_utc=dt(0))
    s = update_state(s, ping_ok=False, now_utc=dt(1))
    s = update_state(s, ping_ok=False, now_utc=dt(2))
    assert s.in_outage

    s = update_state(s, ping_ok=True, now_utc=dt(3))
    assert not s.in_outage
    assert s.outage_start_utc is None
    assert s.mobile_start_utc is None
    assert s.consecutive_failures == 0


def test_mobile_duration_tracks_only_while_mobile(monkeypatch):
    # Note: update_state() queries fritz twice when outage starts:
    # 1) on outage start
    # 2) again in the ongoing-outage block
    seq = iter(["mobile", "mobile", "mobile", "dsl"])
    monkeypatch.setattr("probe.get_connection_type", lambda: next(seq))

    s = OutageState()
    s = update_state(s, ping_ok=False, now_utc=dt(0))
    s = update_state(s, ping_ok=False, now_utc=dt(1))
    s = update_state(s, ping_ok=False, now_utc=dt(2))  # outage start; mobile
    assert s.mobile_start_utc == dt(2)

    # still in outage, still mobile
    s = update_state(s, ping_ok=False, now_utc=dt(10))
    outage_dur, mobile_dur = compute_durations(s, now_utc=dt(10))
    assert outage_dur == 8.0
    assert mobile_dur == 8.0

    # still in outage, but now dsl -> reset mobile
    s = update_state(s, ping_ok=False, now_utc=dt(11))
    assert s.mobile_start_utc is None

