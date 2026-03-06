"""Tests for the SQLite storage layer (db.py) and the web.py reader."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import db as db_mod
from db import ensure_schema, get_connection, insert_measurement, prune_old_rows, query_measurements
from web import _aggregate_buckets, _load_raw_points


def _make_row(ts: datetime, ping_ok: bool = True, latency_ms: float = 15.0,
              dsl_event_active: bool = False, connection_type: str = "dsl",
              **overrides):
    """Build a measurement dict matching the DB schema."""
    row = {
        "timestamp": ts.isoformat(),
        "ping_target": "8.8.8.8",
        "ping_ok": 1 if ping_ok else 0,
        "latency_ms": latency_ms if ping_ok else None,
        "consecutive_failures": 0,
        "dsl_event_active": 1 if dsl_event_active else 0,
        "dsl_event_trigger": "",
        "dsl_event_duration_seconds": None,
        "dsl_event_end_reason": "",
        "connection_type": connection_type,
        "mobile_duration_seconds": None,
        "http_probe_ok": 1,
        "http_probe_error": "",
    }
    row.update(overrides)
    return row


class TestDbRoundtrip:
    """Insert rows and read them back."""

    def test_insert_and_query(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        conn = get_connection(db_file)
        ensure_schema(conn)

        t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t2 = t1 + timedelta(seconds=10)
        insert_measurement(conn, _make_row(t1, latency_ms=14.5))
        insert_measurement(conn, _make_row(t2, latency_ms=16.2))

        rows = query_measurements(conn)
        assert len(rows) == 2
        assert rows[0]["latency_ms"] == 14.5
        assert rows[1]["latency_ms"] == 16.2
        conn.close()

    def test_query_with_since_filter(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        conn = get_connection(db_file)
        ensure_schema(conn)

        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        for i in range(10):
            insert_measurement(conn, _make_row(base + timedelta(hours=i)))

        since = base + timedelta(hours=5)
        rows = query_measurements(conn, since_utc=since)
        assert len(rows) == 5  # hours 5,6,7,8,9
        conn.close()

    def test_prune_old_rows(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        conn = get_connection(db_file)
        ensure_schema(conn)

        old = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        recent = datetime.now(timezone.utc) - timedelta(hours=1)

        insert_measurement(conn, _make_row(old))
        insert_measurement(conn, _make_row(recent))

        deleted = prune_old_rows(conn, retention_days=7)
        assert deleted == 1

        rows = query_measurements(conn)
        assert len(rows) == 1
        conn.close()

    def test_null_latency_on_failure(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        conn = get_connection(db_file)
        ensure_schema(conn)

        t = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        insert_measurement(conn, _make_row(t, ping_ok=False))

        rows = query_measurements(conn)
        assert rows[0]["latency_ms"] is None
        assert rows[0]["ping_ok"] == 0
        conn.close()


class TestWebReadsDb:
    """Verify web.py's _load_raw_points and _aggregate_buckets work with SQLite data."""

    def test_load_and_aggregate(self, tmp_path, monkeypatch):
        db_file = str(tmp_path / "test.db")
        conn = get_connection(db_file)
        ensure_schema(conn)

        base = datetime.now(timezone.utc) - timedelta(hours=1)
        for i in range(12):
            t = base + timedelta(seconds=i * 10)
            insert_measurement(conn, _make_row(t, latency_ms=15.0 + i))
        conn.close()

        # Patch web module to use our temp DB
        monkeypatch.setattr("web.LOG_PATH", db_file)

        points = _load_raw_points()
        assert len(points) == 12
        assert all(p["ping_ok"] for p in points)

        buckets = _aggregate_buckets(points, bucket_minutes=5)
        assert len(buckets) >= 1
        assert buckets[0]["status"] == "ok"

    def test_load_raw_points_reads_local_sqlite(self, tmp_path, monkeypatch):
        db_file = str(tmp_path / "test.db")
        conn = get_connection(db_file)
        ensure_schema(conn)

        ts = datetime.now(timezone.utc) - timedelta(minutes=5)
        insert_measurement(conn, _make_row(ts, latency_ms=12.3))
        conn.close()

        monkeypatch.setattr("web.LOG_PATH", db_file)

        points = _load_raw_points()
        assert len(points) == 1
        assert points[0]["latency_ms"] == 12.3

    def test_load_raw_points_reads_dsl_event_trigger(self, tmp_path, monkeypatch):
        db_file = str(tmp_path / "test.db")
        conn = get_connection(db_file)
        ensure_schema(conn)

        ts = datetime.now(timezone.utc) - timedelta(minutes=5)
        insert_measurement(conn, _make_row(ts, ping_ok=False, dsl_event_active=True, dsl_event_trigger="http_timeout"))
        conn.close()

        monkeypatch.setattr("web.LOG_PATH", db_file)

        points = _load_raw_points()
        assert len(points) == 1
        assert points[0]["dsl_event_trigger"] == "http_timeout"

    def test_outage_bucket(self, tmp_path, monkeypatch):
        db_file = str(tmp_path / "test.db")
        conn = get_connection(db_file)
        ensure_schema(conn)

        base = datetime.now(timezone.utc) - timedelta(minutes=10)
        # 3 normal, then 3 with dsl_event_active
        for i in range(3):
            insert_measurement(conn, _make_row(base + timedelta(seconds=i * 10)))
        for i in range(3, 6):
            insert_measurement(conn, _make_row(
                base + timedelta(seconds=i * 10),
                ping_ok=False,
                dsl_event_active=True,
                connection_type="mobile",
            ))
        conn.close()

        monkeypatch.setattr("web.LOG_PATH", db_file)

        points = _load_raw_points()
        assert len(points) == 6

        buckets = _aggregate_buckets(points, bucket_minutes=5)
        # At least one bucket should be outage
        statuses = {b["status"] for b in buckets}
        assert "outage" in statuses


