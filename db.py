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
    http_probe_error    TEXT NOT NULL DEFAULT ''
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
    conn.commit()


def insert_measurement(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    """Insert a single measurement row."""
    conn.execute(
        """INSERT INTO measurements (
            timestamp, ping_target, ping_ok, latency_ms,
            consecutive_failures, dsl_event_active, dsl_event_trigger,
            dsl_event_duration_seconds, dsl_event_end_reason,
            connection_type, mobile_duration_seconds,
            http_probe_ok, http_probe_error
        ) VALUES (
            :timestamp, :ping_target, :ping_ok, :latency_ms,
            :consecutive_failures, :dsl_event_active, :dsl_event_trigger,
            :dsl_event_duration_seconds, :dsl_event_end_reason,
            :connection_type, :mobile_duration_seconds,
            :http_probe_ok, :http_probe_error
        )""",
        row,
    )
    conn.commit()


def prune_old_rows(conn: sqlite3.Connection, retention_days: int) -> int:
    """Delete rows older than *retention_days*.  Returns deleted count."""
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    cur = conn.execute("DELETE FROM measurements WHERE timestamp < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def query_measurements(
    conn: sqlite3.Connection,
    since_utc: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Return measurements as a list of plain dicts, ordered by timestamp."""
    if since_utc is not None:
        rows = conn.execute(
            "SELECT * FROM measurements WHERE timestamp >= ? ORDER BY timestamp",
            (since_utc.isoformat(),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM measurements ORDER BY timestamp"
        ).fetchall()
    return [dict(r) for r in rows]

