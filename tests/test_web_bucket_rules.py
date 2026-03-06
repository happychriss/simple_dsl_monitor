from datetime import datetime, timezone

from web import _aggregate_buckets, _detect_outages


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
            "dsl_event_trigger": "",
        },
        {
            "timestamp_utc": base.replace(second=10),
            "ping_ok": False,
            "dsl_event_active": True,
            "latency_ms": None,
            "connection_type": "unknown",
            "mobile_duration_seconds": None,
            "dsl_event_trigger": "ping_failures",
        },
    ]

    buckets = _aggregate_buckets(pts, bucket_minutes=5)
    assert len(buckets) == 1
    assert buckets[0]["status"] == "outage"
    assert buckets[0]["max_outage_duration_seconds"] is None


def test_bucket_outage_can_still_be_dsl_for_yellow_ui_rule():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    pts = [
        {
            "timestamp_utc": base,
            "ping_ok": False,
            "dsl_event_active": True,
            "latency_ms": None,
            "connection_type": "dsl",
            "mobile_duration_seconds": None,
            "dsl_event_trigger": "high_latency",
        }
    ]

    buckets = _aggregate_buckets(pts, bucket_minutes=5)
    assert len(buckets) == 1
    assert buckets[0]["status"] == "outage"
    assert buckets[0]["connection_type"] == "dsl"


def test_bucket_mobile_event_wins_over_dsl_event_within_same_bucket():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    pts = [
        {
            "timestamp_utc": base,
            "ping_ok": False,
            "dsl_event_active": True,
            "latency_ms": None,
            "connection_type": "dsl",
            "mobile_duration_seconds": None,
            "dsl_event_trigger": "ping_failures",
        },
        {
            "timestamp_utc": base.replace(second=30),
            "ping_ok": False,
            "dsl_event_active": True,
            "latency_ms": None,
            "connection_type": "mobile",
            "mobile_duration_seconds": 30.0,
            "dsl_event_trigger": "fritz_mobile",
        },
    ]

    buckets = _aggregate_buckets(pts, bucket_minutes=5)
    assert len(buckets) == 1
    assert buckets[0]["status"] == "outage"
    assert buckets[0]["connection_type"] == "mobile"


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
            "dsl_event_trigger": "",
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
            "dsl_event_trigger": "",
        },
        {
            "timestamp_utc": base.replace(minute=4, second=55),
            "ping_ok": True,
            "dsl_event_active": False,
            "latency_ms": 30.0,
            "connection_type": "dsl",
            "mobile_duration_seconds": None,
            "dsl_event_trigger": "",
        },
    ]

    buckets = _aggregate_buckets(pts, bucket_minutes=5)
    assert len(buckets) == 1
    # Line value is P50 (median). With two samples, median is the midpoint.
    assert buckets[0]["latency_ms"] == 20.0
    assert buckets[0]["latency_max"] == 30.0


def test_bucket_event_hover_metadata_is_aggregated():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    pts = [
        {
            "timestamp_utc": base,
            "ping_ok": True,
            "dsl_event_active": False,
            "latency_ms": 15.0,
            "connection_type": "dsl",
            "mobile_duration_seconds": None,
            "dsl_event_trigger": "",
        },
        {
            "timestamp_utc": base.replace(second=10),
            "ping_ok": False,
            "dsl_event_active": True,
            "latency_ms": None,
            "connection_type": "mobile",
            "mobile_duration_seconds": 10.0,
            "dsl_event_trigger": "http_timeout",
        },
        {
            "timestamp_utc": base.replace(second=20),
            "ping_ok": True,
            "dsl_event_active": True,
            "latency_ms": 120.0,
            "connection_type": "mobile",
            "mobile_duration_seconds": 20.0,
            "dsl_event_trigger": "fritz_mobile",
        },
    ]

    buckets = _aggregate_buckets(pts, bucket_minutes=5)
    assert len(buckets) == 1
    assert buckets[0]["status"] == "outage"
    assert buckets[0]["connection_type"] == "mobile"
    assert buckets[0]["dsl_event_trigger"] == "http_timeout"
    assert buckets[0]["max_mobile_duration_seconds"] == 20.0
    assert buckets[0]["latency_max"] == 120.0


def test_bucket_uses_first_sample_timestamp_for_tooltip():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    pts = [
        {
            "timestamp_utc": base.replace(second=34),
            "ping_ok": True,
            "dsl_event_active": False,
            "latency_ms": 20.0,
            "connection_type": "dsl",
            "mobile_duration_seconds": None,
            "dsl_event_trigger": "",
        },
        {
            "timestamp_utc": base.replace(minute=4, second=59),
            "ping_ok": True,
            "dsl_event_active": False,
            "latency_ms": 30.0,
            "connection_type": "dsl",
            "mobile_duration_seconds": None,
            "dsl_event_trigger": "",
        },
    ]

    buckets = _aggregate_buckets(pts, bucket_minutes=5)
    assert len(buckets) == 1
    assert buckets[0]["first_sample_utc"] == base.replace(second=34).isoformat()


def test_detect_outages_keeps_trigger_and_mobile_duration():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    pts = [
        {
            "timestamp_utc": base,
            "ping_ok": False,
            "dsl_event_active": True,
            "latency_ms": None,
            "connection_type": "mobile",
            "mobile_duration_seconds": 0.0,
            "dsl_event_trigger": "http_timeout",
        },
        {
            "timestamp_utc": base.replace(second=12),
            "ping_ok": False,
            "dsl_event_active": True,
            "latency_ms": None,
            "connection_type": "dsl",
            "mobile_duration_seconds": 11.7,
            "dsl_event_trigger": "http_timeout",
        },
        {
            "timestamp_utc": base.replace(second=15),
            "ping_ok": True,
            "dsl_event_active": False,
            "latency_ms": 18.0,
            "connection_type": "dsl",
            "mobile_duration_seconds": 11.7,
            "dsl_event_trigger": "",
        },
    ]

    events = _detect_outages(pts)
    assert len(events) == 1
    assert events[0]["duration_seconds"] == 15.0
    assert events[0]["dsl_event_trigger"] == "http_timeout"
    assert events[0]["mobile_duration_seconds"] == 11.7
    assert events[0]["connection_type"] == "dsl"


def test_bucket_extreme_marker_triggers_only_if_enough_outside(monkeypatch):
    # Configure trigger threshold so this test is deterministic.
    monkeypatch.setenv("DSL_MONITOR_OUTSIDE_FRACTION_THRESHOLD", "0.05")
    # Reload module-level constants by re-importing web with env already set.
    import importlib
    import web as web_mod

    importlib.reload(web_mod)

    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Case A: 1 spike out of 60 => 1.67% < 5% => no marker
    pts = []
    for i in range(59):
        pts.append(
            {
                "timestamp_utc": base.replace(second=0),
                "ping_ok": True,
                "dsl_event_active": False,
                "latency_ms": 10.0,
                "connection_type": "dsl",
                "mobile_duration_seconds": None,
                "dsl_event_trigger": "",
            }
        )
    pts.append(
        {
            "timestamp_utc": base.replace(second=1),
            "ping_ok": True,
            "dsl_event_active": False,
            "latency_ms": 124.0,
            "connection_type": "dsl",
            "mobile_duration_seconds": None,
            "dsl_event_trigger": "",
        }
    )
    buckets = web_mod._aggregate_buckets(pts, bucket_minutes=5)
    assert len(buckets) == 1
    assert buckets[0]["marker_triggered"] is False
    assert buckets[0]["latency_max"] == 124.0

    # Case B: 3 spikes out of 60 => 5% => marker triggers and shows max
    pts2 = []
    for i in range(57):
        pts2.append(
            {
                "timestamp_utc": base.replace(second=0),
                "ping_ok": True,
                "dsl_event_active": False,
                "latency_ms": 10.0,
                "connection_type": "dsl",
                "mobile_duration_seconds": None,
                "dsl_event_trigger": "",
            }
        )
    for s in (38.0, 63.0, 124.0):
        pts2.append(
            {
                "timestamp_utc": base.replace(second=1),
                "ping_ok": True,
                "dsl_event_active": False,
                "latency_ms": s,
                "connection_type": "dsl",
                "mobile_duration_seconds": None,
                "dsl_event_trigger": "",
            }
        )

    buckets2 = web_mod._aggregate_buckets(pts2, bucket_minutes=5)
    assert len(buckets2) == 1
    assert buckets2[0]["marker_triggered"] is True
    assert buckets2[0]["latency_max"] == 124.0


