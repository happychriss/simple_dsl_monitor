from datetime import datetime, timezone

from probe import OutageState, update_state


def dt(n: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, n, tzinfo=timezone.utc)


def test_http_timeout_starts_dsl_event():
    s = OutageState()
    s = update_state(s, ping_ok=True, now_utc=dt(0), http_timeout_trigger=True)
    assert s.dsl_event_active
    assert s.dsl_event_trigger == "http_timeout"
