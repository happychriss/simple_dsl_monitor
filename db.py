"""SQLite storage layer for DSL Monitor.

Shared by probe.py (write) and web.py (read).  Uses WAL mode for safe
concurrent access from multiple threads/processes.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


DB_PATH = os.environ.get(
    "DSL_MONITOR_LOG",
    os.path.join(os.path.dirname(__file__), "dsl_log.db"),
)

# Optional DB pruning retention (0 = keep forever).
DB_RETENTION_DAYS = int(os.environ.get("DSL_MONITOR_DB_RETENTION_DAYS", "0"))


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS measurements (
    timestamp           TEXT NOT NULL,
    ping_target         TEXT NOT NULL,
    ping_ok             INTEGER NOT NULL,
    latency_ms          REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    dsl_event_active    INTEGER NOT NULL DEFAULT 0,
    dsl_event_trigger   TEXT NOT NULL DEFAULT '',
    dsl_event_duration_seconds REAL,
    dsl_event_end_reason TEXT NOT NULL DEFAULT '',
    connection_type     TEXT NOT NULL DEFAULT 'unknown',
    mobile_duration_seconds REAL,
    http_probe_ok       INTEGER,
    http_probe_error    TEXT NOT NULL DEFAULT '',
    snr_down_db         REAL,
    snr_up_db           REAL,
    ds_attenuation_db   REAL,
    us_attenuation_db   REAL,
    ds_curr_rate_kbps   INTEGER,
    us_curr_rate_kbps   INTEGER,
    link_retrains       INTEGER,
    crc_errors          INTEGER,
    fec_errors          INTEGER,
    errored_secs        INTEGER,
    severely_errored_secs INTEGER,
    ppp_uptime_seconds  INTEGER
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_measurements_ts ON measurements(timestamp)
"""


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Open (or create) the SQLite database with WAL mode."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the measurements table and index if they don't exist."""
    conn.execute(_CREATE_TABLE)
    conn.execute(_CREATE_INDEX)
    # Migrate existing databases: add new columns if absent.
    for col, typedef in [
        ("snr_down_db", "REAL"),
        ("snr_up_db", "REAL"),
        ("ds_attenuation_db", "REAL"),
        ("us_attenuation_db", "REAL"),
        ("ds_curr_rate_kbps", "INTEGER"),
        ("us_curr_rate_kbps", "INTEGER"),
        ("link_retrains", "INTEGER"),
        ("crc_errors", "INTEGER"),
        ("fec_errors", "INTEGER"),
        ("errored_secs", "INTEGER"),
        ("severely_errored_secs", "INTEGER"),
        ("ppp_uptime_seconds", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE measurements ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()


def insert_measurement(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    """Insert a single measurement row."""
    # Merge with defaults so callers don't need to supply every column.
    full_row = {
        "snr_down_db": None,
        "snr_up_db": None,
        "ds_attenuation_db": None,
        "us_attenuation_db": None,
        "ds_curr_rate_kbps": None,
        "us_curr_rate_kbps": None,
        "link_retrains": None,
        "crc_errors": None,
        "fec_errors": None,
        "errored_secs": None,
        "severely_errored_secs": None,
        "ppp_uptime_seconds": None,
        **row,
    }
    conn.execute(
        """INSERT INTO measurements (
            timestamp, ping_target, ping_ok, latency_ms,
            consecutive_failures, dsl_event_active, dsl_event_trigger,
            dsl_event_duration_seconds, dsl_event_end_reason,
            connection_type, mobile_duration_seconds,
            http_probe_ok, http_probe_error,
            snr_down_db, snr_up_db,
            ds_attenuation_db, us_attenuation_db,
            ds_curr_rate_kbps, us_curr_rate_kbps,
            link_retrains, crc_errors, fec_errors,
            errored_secs, severely_errored_secs,
            ppp_uptime_seconds
        ) VALUES (
            :timestamp, :ping_target, :ping_ok, :latency_ms,
            :consecutive_failures, :dsl_event_active, :dsl_event_trigger,
            :dsl_event_duration_seconds, :dsl_event_end_reason,
            :connection_type, :mobile_duration_seconds,
            :http_probe_ok, :http_probe_error,
            :snr_down_db, :snr_up_db,
            :ds_attenuation_db, :us_attenuation_db,
            :ds_curr_rate_kbps, :us_curr_rate_kbps,
            :link_retrains, :crc_errors, :fec_errors,
            :errored_secs, :severely_errored_secs,
            :ppp_uptime_seconds
        )""",
        full_row,
    )
    conn.commit()


def prune_old_rows(conn: sqlite3.Connection, retention_days: int) -> int:
    """Delete rows older than *retention_days*.  Returns deleted count."""
    if retention_days <= 0:
        return 0
    # Timestamps are stored as ISO8601 strings (tz-aware). To be robust across
    # offsets (local time vs UTC), compare via SQLite's julianday() conversion.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    cur = conn.execute(
        "DELETE FROM measurements WHERE julianday(timestamp) < julianday(?)",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def query_measurements(
    conn: sqlite3.Connection,
    since_utc: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Return measurements as a list of plain dicts, ordered by timestamp."""
    if since_utc is not None:
        rows = conn.execute(
            "SELECT * FROM measurements WHERE julianday(timestamp) >= julianday(?) ORDER BY julianday(timestamp)",
            (since_utc.isoformat(),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM measurements ORDER BY julianday(timestamp)"
        ).fetchall()
    return [dict(r) for r in rows]

