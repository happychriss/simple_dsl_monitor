"""PostgreSQL storage layer for DSL Monitor.

Shared by probe.py (write) and web.py (read).
Drop-in replacement for the previous SQLite layer — same public interface.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

DB_HOST = os.environ.get("DB_HOST", "zo")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "dsl-monitor")
DB_USER = os.environ.get("DB_USER", "dsl-monitor")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

# DB_PATH kept for backward-compat (probe.py / web.py use it for logging).
DB_PATH = f"postgresql://{DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

DB_RETENTION_DAYS = int(os.environ.get("DSL_MONITOR_DB_RETENTION_DAYS", "0"))


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS measurements (
    timestamp                   TIMESTAMPTZ NOT NULL,
    ping_target                 TEXT NOT NULL,
    ping_ok                     INTEGER NOT NULL,
    latency_ms                  REAL,
    consecutive_failures        INTEGER NOT NULL DEFAULT 0,
    dsl_event_active            INTEGER NOT NULL DEFAULT 0,
    dsl_event_trigger           TEXT NOT NULL DEFAULT '',
    dsl_event_duration_seconds  REAL,
    dsl_event_end_reason        TEXT NOT NULL DEFAULT '',
    connection_type             TEXT NOT NULL DEFAULT 'unknown',
    mobile_duration_seconds     REAL,
    http_probe_ok               INTEGER,
    http_probe_error            TEXT NOT NULL DEFAULT '',
    snr_down_db                 REAL,
    snr_up_db                   REAL,
    ds_attenuation_db           REAL,
    us_attenuation_db           REAL,
    ds_curr_rate_kbps           INTEGER,
    us_curr_rate_kbps           INTEGER,
    link_retrains               INTEGER,
    crc_errors                  INTEGER,
    fec_errors                  INTEGER,
    errored_secs                INTEGER,
    severely_errored_secs       INTEGER,
    ppp_uptime_seconds          INTEGER
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_measurements_ts ON measurements(timestamp)
"""


def get_connection(db_path: str | None = None) -> "psycopg2.extensions.connection":
    """Open a new PostgreSQL connection. The db_path argument is ignored."""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def ensure_schema(conn: "psycopg2.extensions.connection") -> None:
    """Create the measurements table and index if they don't exist."""
    with conn.cursor() as cur:
        cur.execute(_CREATE_TABLE)
        cur.execute(_CREATE_INDEX)
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
            cur.execute(
                f"ALTER TABLE measurements ADD COLUMN IF NOT EXISTS {col} {typedef}"
            )
    conn.commit()


def insert_measurement(conn: "psycopg2.extensions.connection", row: Dict[str, Any]) -> None:
    """Insert a single measurement row."""
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
    with conn.cursor() as cur:
        cur.execute(
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
                %(timestamp)s, %(ping_target)s, %(ping_ok)s, %(latency_ms)s,
                %(consecutive_failures)s, %(dsl_event_active)s, %(dsl_event_trigger)s,
                %(dsl_event_duration_seconds)s, %(dsl_event_end_reason)s,
                %(connection_type)s, %(mobile_duration_seconds)s,
                %(http_probe_ok)s, %(http_probe_error)s,
                %(snr_down_db)s, %(snr_up_db)s,
                %(ds_attenuation_db)s, %(us_attenuation_db)s,
                %(ds_curr_rate_kbps)s, %(us_curr_rate_kbps)s,
                %(link_retrains)s, %(crc_errors)s, %(fec_errors)s,
                %(errored_secs)s, %(severely_errored_secs)s,
                %(ppp_uptime_seconds)s
            )""",
            full_row,
        )
    conn.commit()


def prune_old_rows(conn: "psycopg2.extensions.connection", retention_days: int) -> int:
    """Delete rows older than *retention_days*. Returns deleted count."""
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM measurements WHERE timestamp < %s", (cutoff,))
        deleted = cur.rowcount
    conn.commit()
    return deleted


def query_measurements(
    conn: "psycopg2.extensions.connection",
    since_utc: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Return measurements as a list of plain dicts, ordered by timestamp."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if since_utc is not None:
            cur.execute(
                "SELECT * FROM measurements WHERE timestamp >= %s ORDER BY timestamp",
                (since_utc,),
            )
        else:
            cur.execute("SELECT * FROM measurements ORDER BY timestamp")
        rows = cur.fetchall()

    result = []
    for r in rows:
        d = dict(r)
        # Convert TIMESTAMPTZ → ISO string so web.py can call datetime.fromisoformat()
        if isinstance(d.get("timestamp"), datetime):
            d["timestamp"] = d["timestamp"].isoformat()
        result.append(d)
    return result
