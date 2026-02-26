#!/usr/bin/env python3

import csv
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template_string

LOG_PATH = os.environ.get("DSL_MONITOR_LOG", os.path.join(os.path.dirname(__file__), "dsl_log.csv"))
RETENTION_DAYS = int(os.environ.get("DSL_MONITOR_RETENTION_DAYS", "7"))

BUCKET_MINUTES = int(os.environ.get("DSL_MONITOR_BUCKET_MINUTES", "5"))
MOBILE_YELLOW_THRESHOLD_SECONDS = int(os.environ.get("DSL_MONITOR_MOBILE_YELLOW_THRESHOLD_SECONDS", "300"))

# Fritz status fetch timeout (seconds). Older/slow TR-064 responses can take a bit.
FRITZ_STATUS_TIMEOUT_SECONDS = float(os.environ.get("DSL_MONITOR_FRITZ_STATUS_TIMEOUT_SECONDS", "10"))

app = Flask(__name__)

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>DSL Stability Monitor</title>
  <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 20px; background: #111; color: #eee; }
    h1 { margin-bottom: 0.2rem; }
    .subtitle { color: #aaa; margin-bottom: 1.5rem; }
    #latency { width: 100%; height: 400px; }
    #status { width: 100%; height: 120px; margin-top: 30px; }
    table { width: 100%; border-collapse: collapse; margin-top: 30px; font-size: 0.9rem; }
    th, td { padding: 6px 8px; border-bottom: 1px solid #333; text-align: left; }
    th { background: #1a202c; }
    tr:nth-child(even) { background: #1a1a1a; }
    .outage { color: #f56565; font-weight: 500; }
    a { color: #4fd1c5; }
  </style>
</head>
<body>
  <h1>DSL Stability Monitor</h1>
  <div class="subtitle">Last 7 days – ping probe every 10s, aggregated in 5‑minute buckets. Green = OK · Red = DSL outage event · Yellow = mobile > 5min.</div>

  <div style="margin-bottom: 0.75rem; font-size: 0.9rem; color: #ccc;">
    <span id="current-datetime"></span>
    <span style="margin-left: 1.5rem;">Last data update: <span id="last-updated"></span></span>
    <span style="margin-left: 1.5rem;">Fritz status: <span id="fritz-status">checking…</span></span>
  </div>

  <div id="latency"></div>
  <div id="status"></div>

  <h2 style="margin-top:2rem;">Outage Events (last 7 days)</h2>
  <table id="events-table">
    <thead>
      <tr>
        <th>#</th>
        <th>Start (local)</th>
        <th>End (local)</th>
        <th>Duration</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>

  <script>
    let lastFritzStatus = null;

    function formatDuration(seconds) {
      if (seconds == null) return '';
      const s = Math.round(seconds);
      const mins = Math.floor(s / 60);
      const rem = s % 60;
      if (mins === 0) return `${rem}s`;
      if (rem === 0) return `${mins} min`;
      return `${mins}m ${rem}s`;
    }

    function updateCurrentDateTime() {
      const el = document.getElementById('current-datetime');
      if (!el) return;
      const now = new Date();
      el.textContent = `Now: ${now.toLocaleString()}`;
    }

    async function loadFritzStatus() {
      const fritzEl = document.getElementById('fritz-status');
      if (!fritzEl) return;
      try {
        const resp = await fetch('/api/fritz_status');
        const data = await resp.json();
        if (data.ok) {
          let text, color;
          if (data.connection_type === 'mobile') {
            text = 'mobile';
            color = '#ecc94b';
          } else if (data.connection_type === 'dsl') {
            text = 'dsl';
            color = '#48bb78';
          } else {
            text = 'unknown';
            color = '#a0aec0';
          }
          lastFritzStatus = { text, color };
        } else if (!lastFritzStatus) {
          lastFritzStatus = { text: 'disconnected', color: '#f56565' };
        }
      } catch {
        if (!lastFritzStatus) {
          lastFritzStatus = { text: 'disconnected', color: '#f56565' };
        }
      }

      if (lastFritzStatus) {
        fritzEl.textContent = lastFritzStatus.text;
        fritzEl.style.color = lastFritzStatus.color;
      }
    }

    async function loadData() {
      const resp = await fetch('/api/data');
      const data = await resp.json();

      // Also refresh Fritz status on every page refresh/data refresh.
      loadFritzStatus();

      const lastEl = document.getElementById('last-updated');
      if (lastEl && data.last_updated_utc) {
        const d = new Date(data.last_updated_utc);
        lastEl.textContent = d.toLocaleString();
      }

      const times = data.points.map(p => p.timestamp);
      const latencies = data.points.map(p => p.latency_ms);
      const colors = data.points.map(p => {
        if (p.status === 'outage') return '#f56565';
        if (p.status === 'mobile') return '#ecc94b';
        return '#48bb78';
      });

      const latencyTrace = {
        x: times,
        y: latencies,
        mode: 'lines+markers',
        marker: { color: colors, size: 6 },
        line: { color: '#63b3ed', width: 1 },
        name: `Ping latency (ms) – ${data.bucket_minutes} min buckets`
      };

      const latencyLayout = {
        paper_bgcolor: '#111',
        plot_bgcolor: '#111',
        font: { color: '#eee' },
        xaxis: { title: 'Time' },
        yaxis: { title: 'Ping latency (ms)', rangemode: 'tozero' },
        margin: { t: 40, r: 10, b: 40, l: 50 }
      };

      Plotly.newPlot('latency', [latencyTrace], latencyLayout, {responsive: true});

      const statusTrace = {
        x: times,
        y: new Array(times.length).fill(1),
        mode: 'markers',
        marker: {
          color: colors,
          size: 10,
          symbol: 'square'
        },
        hoverinfo: 'x+text',
        text: data.points.map(p => {
          if (p.status === 'outage') return `DSL OUTAGE in bucket (max ${formatDuration(p.max_outage_duration_seconds)})`;
          const avg = p.latency_ms != null ? p.latency_ms.toFixed(1) : 'n/a';
          if (p.status === 'mobile') return `MOBILE >5min (avg ${avg} ms, max mobile ${formatDuration(p.max_mobile_duration_seconds)})`;
          return `OK (avg ${avg} ms)`;
        })
      };

      const statusLayout = {
        paper_bgcolor: '#111',
        plot_bgcolor: '#111',
        font: { color: '#eee' },
        xaxis: { showgrid: false, zeroline: false, showticklabels: true },
        yaxis: { showgrid: false, zeroline: false, showticklabels: false },
        margin: { t: 20, r: 10, b: 40, l: 40 },
        height: 120
      };

      Plotly.newPlot('status', [statusTrace], statusLayout, {responsive: true});

      const tbody = document.querySelector('#events-table tbody');
      tbody.innerHTML = '';

      if (data.events && data.events.length > 0) {
        data.events.forEach((ev, idx) => {
          const tr = document.createElement('tr');
          tr.classList.add('outage');

          const tdIdx = document.createElement('td');
          tdIdx.textContent = idx + 1;

          const tdStart = document.createElement('td');
          tdStart.textContent = ev.start_local;

          const tdEnd = document.createElement('td');
          tdEnd.textContent = ev.end_local;

          const tdDur = document.createElement('td');
          tdDur.textContent = formatDuration(ev.duration_seconds);

          tr.appendChild(tdIdx);
          tr.appendChild(tdStart);
          tr.appendChild(tdEnd);
          tr.appendChild(tdDur);
          tbody.appendChild(tr);
        });
      }
    }

    updateCurrentDateTime();
    setInterval(updateCurrentDateTime, 1000);
    loadData();
    setInterval(loadFritzStatus, 15000);
  </script>
</body>
</html>"""


def _bucket_start(ts: datetime, minutes: int = 5) -> datetime:
    minutes_since_hour = (ts.minute // minutes) * minutes
    return ts.replace(minute=minutes_since_hour, second=0, microsecond=0)


def _parse_bool01(value: str | None) -> bool:
    return str(value or "").strip() == "1"


def _load_raw_points() -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []
    if not os.path.exists(LOG_PATH):
        return points

    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    with open(LOG_PATH, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts_utc = datetime.fromisoformat(row.get("timestamp", ""))
            except Exception:
                continue
            if ts_utc.tzinfo is None:
                ts_utc = ts_utc.replace(tzinfo=timezone.utc)
            if ts_utc < cutoff_utc:
                continue

            # keep timestamps as UTC internally; UI converts via JS Date()
            ping_ok = _parse_bool01(row.get("ping_ok"))
            # Backwards compatible: some old logs might have "ok"
            if "ping_ok" not in row and "ok" in row:
                ping_ok = _parse_bool01(row.get("ok"))

            in_outage = _parse_bool01(row.get("in_outage"))
            if "in_outage" not in row and ("ok" in row or "ping_ok" in row):
                # old logic: ok==False was outage
                in_outage = not ping_ok

            latency_ms: float | None
            latency_str = (row.get("latency_ms") or "").strip()
            try:
                latency_ms = float(latency_str) if latency_str else None
            except ValueError:
                latency_ms = None

            conn_type = (row.get("connection_type") or "unknown").lower() or "unknown"

            outage_dur: float | None
            outage_dur_str = (row.get("outage_duration_seconds") or "").strip()
            try:
                outage_dur = float(outage_dur_str) if outage_dur_str else None
            except ValueError:
                outage_dur = None

            mobile_dur: float | None
            mobile_dur_str = (row.get("mobile_duration_seconds") or "").strip()
            try:
                mobile_dur = float(mobile_dur_str) if mobile_dur_str else None
            except ValueError:
                mobile_dur = None

            points.append(
                {
                    "timestamp_utc": ts_utc,
                    "ping_ok": ping_ok,
                    "in_outage": in_outage,
                    "latency_ms": latency_ms,
                    "target": row.get("target", ""),
                    "connection_type": conn_type,
                    "outage_duration_seconds": outage_dur,
                    "mobile_duration_seconds": mobile_dur,
                }
            )

    points.sort(key=lambda p: p["timestamp_utc"])
    return points


def _aggregate_buckets(raw_points: List[Dict[str, Any]], bucket_minutes: int = 5) -> List[Dict[str, Any]]:
    buckets: dict[datetime, Dict[str, Any]] = {}

    for p in raw_points:
        ts_utc: datetime = p["timestamp_utc"]
        bucket_ts = _bucket_start(ts_utc, minutes=bucket_minutes)

        b = buckets.setdefault(
            bucket_ts,
            {
                "ok": True,
                "lat_sum": 0.0,
                "lat_count": 0,
                "has_outage": False,
                "max_outage_dur": 0.0,
                "max_mobile_dur": 0.0,
            },
        )

        if p.get("latency_ms") is not None and p.get("ping_ok"):
            b["lat_sum"] += float(p["latency_ms"])
            b["lat_count"] += 1

        if p.get("in_outage"):
            b["has_outage"] = True
            b["ok"] = False
            if p.get("outage_duration_seconds") is not None:
                b["max_outage_dur"] = max(b["max_outage_dur"], float(p["outage_duration_seconds"]))

        if (p.get("connection_type") == "mobile") and p.get("mobile_duration_seconds") is not None:
            b["max_mobile_dur"] = max(b["max_mobile_dur"], float(p["mobile_duration_seconds"]))

    agg_points: List[Dict[str, Any]] = []
    for ts in sorted(buckets.keys()):
        b = buckets[ts]
        lat = b["lat_sum"] / b["lat_count"] if b["lat_count"] > 0 else None

        status = "ok"
        if b["has_outage"]:
            status = "outage"
        elif b["max_mobile_dur"] >= MOBILE_YELLOW_THRESHOLD_SECONDS:
            status = "mobile"

        agg_points.append(
            {
                "timestamp": ts.isoformat(),
                "latency_ms": lat,
                "status": status,
                "max_outage_duration_seconds": b["max_outage_dur"] if b["has_outage"] else None,
                "max_mobile_duration_seconds": b["max_mobile_dur"] if b["max_mobile_dur"] > 0 else None,
            }
        )

    return agg_points


def _format_event(start_utc: datetime, end_utc: datetime) -> Dict[str, Any]:
    duration = max(0.0, (end_utc - start_utc).total_seconds())
    start_local = start_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    end_local = end_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
        "start_local": start_local,
        "end_local": end_local,
        "duration_seconds": duration,
    }


def _detect_outages(raw_points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detect outage events as contiguous sequences where `in_outage` is True."""

    events: List[Dict[str, Any]] = []
    in_outage = False
    start_ts: datetime | None = None

    for p in raw_points:
        ts_utc: datetime = p["timestamp_utc"]
        if p.get("in_outage"):
            if not in_outage:
                in_outage = True
                start_ts = ts_utc
        else:
            if in_outage and start_ts is not None:
                end_ts = ts_utc
                events.append(_format_event(start_ts, end_ts))
                in_outage = False
                start_ts = None

    if in_outage and start_ts is not None and raw_points:
        end_ts = raw_points[-1]["timestamp_utc"]
        events.append(_format_event(start_ts, end_ts))

    events.sort(key=lambda e: e["start_utc"], reverse=True)
    return events


def load_data() -> Dict[str, Any]:
    raw_points = _load_raw_points()
    agg_points = _aggregate_buckets(raw_points, bucket_minutes=BUCKET_MINUTES)
    events = _detect_outages(raw_points)

    last_updated_utc: str | None = None
    if raw_points:
        last_updated_utc = raw_points[-1]["timestamp_utc"].isoformat()

    return {
        "points": agg_points,
        "events": events,
        "last_updated_utc": last_updated_utc,
        "bucket_minutes": BUCKET_MINUTES,
    }


@app.route("/")
def index() -> str:
    return render_template_string(INDEX_HTML)


@app.route("/api/data")
def api_data():
    try:
        return jsonify(load_data())
    except Exception as exc:  # noqa: BLE001
        return jsonify(
            {
                "error": str(exc),
                "points": [],
                "events": [],
                "last_updated_utc": None,
                "bucket_minutes": BUCKET_MINUTES,
            }
        ), 500


@app.route("/api/fritz_status")
def api_fritz_status():
    url = os.environ.get("DSL_CONN_STATUS_URL", "http://127.0.0.1:9077/status")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=FRITZ_STATUS_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        ct = str(data.get("connection_type", "unknown")).lower()
        if ct not in {"dsl", "mobile", "unknown"}:
            ct = "unknown"

        # Pass through optional fields if present.
        payload = {"ok": True, "connection_type": ct}
        if "dsl_sync_up" in data:
            payload["dsl_sync_up"] = data.get("dsl_sync_up")
        if "dsl_sync_source" in data:
            payload["dsl_sync_source"] = data.get("dsl_sync_source")
        return jsonify(payload)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
