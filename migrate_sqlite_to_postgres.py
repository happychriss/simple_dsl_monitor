#!/usr/bin/env python3
"""One-time migration: copy all rows from dsl_log.db (SQLite) into PostgreSQL.

Run from the project directory after updating .env with DB_HOST/DB_NAME/DB_USER/DB_PASSWORD.
"""

import os
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    pass

import psycopg2
import psycopg2.extras

SQLITE_PATH = os.path.join(HERE, "dsl_log.db")

DB_HOST = os.environ.get("DB_HOST", "zo")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "dsl-monitor")
DB_USER = os.environ.get("DB_USER", "dsl-monitor")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

COLUMNS = [
    "timestamp", "ping_target", "ping_ok", "latency_ms",
    "consecutive_failures", "dsl_event_active", "dsl_event_trigger",
    "dsl_event_duration_seconds", "dsl_event_end_reason",
    "connection_type", "mobile_duration_seconds",
    "http_probe_ok", "http_probe_error",
    "snr_down_db", "snr_up_db",
    "ds_attenuation_db", "us_attenuation_db",
    "ds_curr_rate_kbps", "us_curr_rate_kbps",
    "link_retrains", "crc_errors", "fec_errors",
    "errored_secs", "severely_errored_secs",
    "ppp_uptime_seconds",
]

INSERT_SQL = (
    f"INSERT INTO measurements ({', '.join(COLUMNS)}) VALUES %s "
    f"ON CONFLICT DO NOTHING"
)

BATCH_SIZE = 500


def main() -> int:
    if not os.path.exists(SQLITE_PATH):
        print(f"SQLite file not found: {SQLITE_PATH}", file=sys.stderr)
        return 1

    # --- Read SQLite ---
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    rows = sqlite_conn.execute(
        "SELECT * FROM measurements ORDER BY timestamp"
    ).fetchall()
    sqlite_conn.close()
    print(f"SQLite: {len(rows)} rows  ({rows[0]['timestamp']} … {rows[-1]['timestamp']})")

    # --- Connect to PostgreSQL ---
    print(f"Connecting to postgresql://{DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME} …")
    try:
        pg_conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
            user=DB_USER, password=DB_PASSWORD,
        )
    except Exception as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        return 1
    print("Connected.")

    # --- Ensure schema ---
    from db import ensure_schema
    ensure_schema(pg_conn)
    print("Schema verified.")

    # --- Check existing rows ---
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM measurements")
        existing = cur.fetchone()[0]
    print(f"PostgreSQL already contains {existing} rows.")

    if existing > 0:
        answer = input("Rows already exist. Proceed anyway (duplicates skipped)? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            pg_conn.close()
            return 0

    # --- Migrate in batches ---
    batch: list[tuple] = []
    total = 0

    for row in rows:
        d = dict(row)

        # Parse ISO timestamp → Python datetime (psycopg2 needs a datetime for TIMESTAMPTZ)
        ts_raw = d.get("timestamp", "")
        try:
            d["timestamp"] = datetime.fromisoformat(ts_raw)
        except Exception:
            d["timestamp"] = ts_raw

        # Fill missing columns (older SQLite DBs may lack newer columns)
        for col in COLUMNS:
            if col not in d:
                d[col] = None

        batch.append(tuple(d.get(col) for col in COLUMNS))

        if len(batch) >= BATCH_SIZE:
            with pg_conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, INSERT_SQL, batch)
            pg_conn.commit()
            total += len(batch)
            print(f"  {total}/{len(rows)} rows inserted…")
            batch = []

    if batch:
        with pg_conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, INSERT_SQL, batch)
        pg_conn.commit()
        total += len(batch)

    # --- Verify ---
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM measurements")
        pg_count = cur.fetchone()[0]
    pg_conn.close()

    print(f"\nDone. Migrated {total} rows.")
    print(f"PostgreSQL total: {pg_count}  (SQLite had: {len(rows)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
