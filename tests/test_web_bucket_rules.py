from datetime import datetime, timezone

from web import _aggregate_buckets


def test_bucket_red_if_any_outage_in_bucket():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    pts = [
        {
            "timestamp_utc": base,
            "ping_ok": True,
            "dsl_event_active": False,
            "latency_ms": 10.0,
            "connection_type": "unknown",
            "mobile_duration_seconds": None,
        },
        {
            "timestamp_utc": base.replace(second=10),
            "ping_ok": False,
            "dsl_event_active": True,
            "latency_ms": None,
            "connection_type": "unknown",
            "mobile_duration_seconds": None,
        },
    ]

    buckets = _aggregate_buckets(pts, bucket_minutes=5)
    assert len(buckets) == 1
    assert buckets[0]["status"] == "outage"
    assert buckets[0]["max_outage_duration_seconds"] is None


def test_bucket_ok_if_no_outage_even_when_mobile_present():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    pts = [
        {
            "timestamp_utc": base,
            "ping_ok": True,
            "dsl_event_active": False,
            "latency_ms": 12.0,
            "connection_type": "mobile",
            "mobile_duration_seconds": 999.0,
        }
    ]

    buckets = _aggregate_buckets(pts, bucket_minutes=5)
    assert len(buckets) == 1
    assert buckets[0]["status"] == "ok"


def test_bucket_latency_is_max_of_successful_pings_in_bucket():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    pts = [
        {
            "timestamp_utc": base.replace(second=5),
            "ping_ok": True,
            "dsl_event_active": False,
            "latency_ms": 10.0,
            "connection_type": "dsl",
            "mobile_duration_seconds": None,
        },
        {
            "timestamp_utc": base.replace(minute=4, second=55),
            "ping_ok": True,
            "dsl_event_active": False,
            "latency_ms": 30.0,
            "connection_type": "dsl",
            "mobile_duration_seconds": None,
        },
    ]

    buckets = _aggregate_buckets(pts, bucket_minutes=5)
    assert len(buckets) == 1
    assert buckets[0]["latency_ms"] == 30.0

