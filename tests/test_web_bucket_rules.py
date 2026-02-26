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


def test_bucket_yellow_if_mobile_longer_than_threshold():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    pts = [
        {
            "timestamp_utc": base,
            "ping_ok": False,
            "dsl_event_active": True,
            "latency_ms": None,
            "connection_type": "mobile",
            "mobile_duration_seconds": 301.0,
        }
    ]

    buckets = _aggregate_buckets(pts, bucket_minutes=5)
    assert len(buckets) == 1
    # outage has precedence in current rules
    assert buckets[0]["status"] == "outage"

    # if there's no outage but mobile duration is present, it becomes mobile
    pts2 = [
        {
            "timestamp_utc": base,
            "ping_ok": True,
            "dsl_event_active": False,
            "latency_ms": 12.0,
            "connection_type": "mobile",
            "mobile_duration_seconds": 301.0,
        }
    ]
    buckets2 = _aggregate_buckets(pts2, bucket_minutes=5)
    assert buckets2[0]["status"] == "mobile"
