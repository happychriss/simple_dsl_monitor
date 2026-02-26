#!/usr/bin/env python3

import csv
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests
from flask import Flask, jsonify, render_template_string

LOG_PATH = os.environ.get("DSL_MONITOR_LOG", os.path.join(os.path.dirname(__file__), "dsl_log.csv"))
RETENTION_DAYS = int(os.environ.get("DSL_MONITOR_RETENTION_DAYS", "7"))

BUCKET_MINUTES = int(os.environ.get("DSL_MONITOR_BUCKET_MINUTES", "5"))
MOBILE_YELLOW_THRESHOLD_SECONDS = int(os.environ.get("DSL_MONITOR_MOBILE_YELLOW_THRESHOLD_SECONDS", "300"))

# Fritz status fetch timeout (seconds). Older/slow TR-064 responses can take a bit.
FRITZ_STATUS_TIMEOUT_SECONDS = float(os.environ.get("DSL_MONITOR_FRITZ_STATUS_TIMEOUT_SECONDS", "10"))

# HTTP probe – same env vars as probe.py so they stay in sync
HTTP_PROBE_URL = os.environ.get("DSL_MONITOR_HTTP_PROBE_URL", "https://www.tagesschau.de/tagesthemen")
HTTP_PROBE_TIMEOUT_SECONDS = float(os.environ.get("DSL_MONITOR_HTTP_PROBE_TIMEOUT_SECONDS", "15"))

# Fritz polling in the UI: only when an outage is active, and then max once/min.
FRITZ_UI_POLL_INTERVAL_SECONDS = float(os.environ.get("DSL_MONITOR_FRITZ_UI_POLL_INTERVAL_SECONDS", "60"))

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Live HTTP probe cache (web process runs its own check on demand, cached 5 min)
# ---------------------------------------------------------------------------
_live_probe_lock = threading.Lock()
_live_probe_last_ok: bool | None = None
_live_probe_last_error: str | None = None
_live_probe_last_ts: float = 0.0   # monotonic
_LIVE_PROBE_TTL = 300.0            # seconds – matches probe interval

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
    <span style="margin-left: 1.5rem;">Fritz status: <span id="fritz-status">—</span></span>
    <span style="margin-left: 1.5rem;">HTTP probe: <span id="http-probe-status">—</span></span>
    <span style="margin-left: 1.5rem;">
      <button id="check-dsl" style="padding: 4px 10px; background:#1a202c; color:#eee; border:1px solid #333; border-radius:4px; cursor:pointer;">Check DSL now</button>
      <span id="check-dsl-result" style="margin-left: 0.75rem; color:#a0aec0;"></span>
    </span>
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
    let lastFritzFetchMs = 0;
    let outageActive = false;

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

    function setFritzUi(text, color) {
      const fritzEl = document.getElementById('fritz-status');
      if (!fritzEl) return;
      fritzEl.textContent = text;
      fritzEl.style.color = color;
    }

    async function loadFritzStatusIfNeeded(force=false) {
      // Hard throttle: only poll Fritz while a DSL outage event is active, and then max 1/min.
      if (!outageActive) {
        lastFritzStatus = null;
        setFritzUi('—', '#a0aec0');
        return;
      }

      const now = Date.now();
      const minIntervalMs = 60000; // keep in sync with backend expectations
      if (!force && lastFritzFetchMs && (now - lastFritzFetchMs) < minIntervalMs) {
        // Keep showing cached status.
        if (lastFritzStatus) setFritzUi(lastFritzStatus.text, lastFritzStatus.color);
        return;
      }

      lastFritzFetchMs = now;
      setFritzUi('checking…', '#a0aec0');

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
        } else {
          lastFritzStatus = { text: 'disconnected', color: '#f56565' };
        }
      } catch {
        lastFritzStatus = { text: 'disconnected', color: '#f56565' };
      }

      if (lastFritzStatus) setFritzUi(lastFritzStatus.text, lastFritzStatus.color);
    }

    async function loadHttpProbeStatus() {
      const el = document.getElementById('http-probe-status');
      if (!el) return;
      // show a short transient state so we don't look "stuck" on first load
      if (el.textContent === '—') {
        el.textContent = 'checking…';
        el.style.color = '#a0aec0';
      }
      try {
        const resp = await fetch('/api/http_probe_status');
        const data = await resp.json();
        if (data.last_ok === null || data.last_ok === undefined) {
          el.textContent = 'pending';
          el.style.color = '#a0aec0';
        } else if (data.last_ok) {
          el.textContent = 'OK';
          el.style.color = '#48bb78';
        } else {
          const err = data.last_error ? ` (${data.last_error})` : '';
          el.textContent = `ERROR${err}`;
          el.style.color = '#f56565';
        }
      } catch {
        el.textContent = 'unreachable';
        el.style.color = '#f56565';
      }
    }

    async function loadData() {
      const resp = await fetch('/api/data');
      const data = await resp.json();

      // Determine whether a DSL event is currently active.
      // Prefer backend's raw-sample based flag.
      outageActive = !!data.dsl_event_active;
      if (!outageActive && data.points && data.points.length > 0) {
        const last = data.points[data.points.length - 1];
        outageActive = (last.status === 'outage');
      }

      // Refresh Fritz status only if outageActive (rate-limited).
      loadFritzStatusIfNeeded(false);

      // Also refresh HTTP probe status on every data refresh.
      loadHttpProbeStatus();

      const lastEl = document.getElementById('last-updated');
      if (lastEl && data.last_updated_utc) {
        const d = new Date(data.last_updated_utc);
        lastEl.textContent = d.toLocaleString();
      }

      const times = data.points.map(p => new Date(p.timestamp));
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

    // Initial load
    loadData();

    // Refresh data (and derived statuses) every 30s.
    setInterval(loadData, 30000);

    // NOTE: Do NOT poll Fritz in a separate 10s loop.
    // Fritz is refreshed exclusively from loadData(), and that function already
    // applies a hard 1/min throttle while an outage is active.

    async function checkDslNow() {
      const btn = document.getElementById('check-dsl');
      const out = document.getElementById('check-dsl-result');
      if (!btn || !out) return;

      btn.disabled = true;
      out.textContent = 'checking…';
      out.style.color = '#a0aec0';

      try {
        const resp = await fetch('/api/check_dsl_now', { method: 'POST' });
        const data = await resp.json();

        // Update status line components immediately
        if (data.fritz && data.fritz.ok) {
          let text, color;
          if (data.fritz.connection_type === 'dsl') { text = 'dsl'; color = '#48bb78'; }
          else if (data.fritz.connection_type === 'mobile') { text = 'mobile'; color = '#ecc94b'; }
          else { text = 'unknown'; color = '#a0aec0'; }
          setFritzUi(text, color);
        } else {
          setFritzUi('disconnected', '#f56565');
        }

        if (data.http_probe) {
          const el = document.getElementById('http-probe-status');
          if (el) {
            if (data.http_probe.ok) {
              el.textContent = 'OK';
              el.style.color = '#48bb78';
            } else {
              el.textContent = `ERROR (${data.http_probe.error || 'unknown'})`;
              el.style.color = '#f56565';
            }
          }
        }

        // Human-friendly overall result for the manual check
        const fritzOk = !!(data.fritz && data.fritz.ok);
        const fritzCt = fritzOk ? (data.fritz.connection_type || 'unknown') : 'error';
        const httpOk = !!(data.http_probe && data.http_probe.ok);
        const httpErr = (data.http_probe && !data.http_probe.ok) ? (data.http_probe.error || 'error') : null;

        if (fritzOk && fritzCt === 'dsl' && httpOk) {
          out.textContent = 'OK';
          out.style.color = '#48bb78';
        } else if (fritzOk && fritzCt === 'mobile') {
          // Mobile fallback detected (important to show explicitly)
          out.textContent = httpOk ? 'MOBILE (http ok)' : `MOBILE (http ${httpErr || 'error'})`;
          out.style.color = '#ecc94b';
        } else if (!httpOk || !fritzOk) {
          const parts = [];
          if (!fritzOk) parts.push('fritz error');
          if (!httpOk) parts.push(`http ${httpErr}`);
          out.textContent = `ERROR (${parts.join(', ')})`;
          out.style.color = '#f56565';
        } else {
          // e.g. fritz unknown but http ok
          out.textContent = `UNKNOWN (fritz ${fritzCt}${httpOk ? ', http ok' : ''})`;
          out.style.color = '#a0aec0';
        }
      } catch {
        out.textContent = 'failed';
        out.style.color = '#f56565';
      } finally {
        btn.disabled = false;
      }
    }

    const btn = document.getElementById('check-dsl');
    if (btn) btn.addEventListener('click', checkDslNow);
  </script>
</body>
</html>"""


def _bucket_start(ts: datetime, minutes: int = 5) -> datetime:
    minutes_since_hour = (ts.minute // minutes) * minutes
    out = ts.replace(minute=minutes_since_hour, second=0, microsecond=0)
    # Always return timezone-aware UTC timestamps so the frontend can display
    # everything consistently in the browser's local timezone.
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    else:
        out = out.astimezone(timezone.utc)
    return out


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
                ts_utc = datetime.fromisoformat(row["timestamp"])
            except Exception:
                continue
            if ts_utc.tzinfo is None:
                ts_utc = ts_utc.replace(tzinfo=timezone.utc)
            if ts_utc < cutoff_utc:
                continue

            ping_ok = _parse_bool01(row.get("ping_ok"))

            latency_ms: float | None
            latency_str = (row.get("latency_ms") or "").strip()
            try:
                latency_ms = float(latency_str) if latency_str else None
            except ValueError:
                latency_ms = None

            mobile_dur: float | None
            mobile_dur_str = (row.get("mobile_duration_seconds") or "").strip()
            try:
                mobile_dur = float(mobile_dur_str) if mobile_dur_str else None
            except ValueError:
                mobile_dur = None

            dsl_event_active = _parse_bool01(row.get("dsl_event_active"))

            points.append(
                {
                    "timestamp_utc": ts_utc,
                    "ping_ok": ping_ok,
                    "latency_ms": latency_ms,
                    "ping_target": row.get("ping_target", ""),
                    "connection_type": (row.get("connection_type") or "unknown").lower(),
                    "mobile_duration_seconds": mobile_dur,
                    "dsl_event_active": dsl_event_active,
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
                "lat_sum": 0.0,
                "lat_count": 0,
                "has_outage": False,
                "max_mobile_dur": 0.0,
            },
        )

        if p.get("latency_ms") is not None and p.get("ping_ok"):
            b["lat_sum"] += float(p["latency_ms"])
            b["lat_count"] += 1

        if p.get("dsl_event_active"):
            b["has_outage"] = True

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
                "max_outage_duration_seconds": None,
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
    """Detect events as contiguous sequences where `dsl_event_active` is True."""

    events: List[Dict[str, Any]] = []
    in_evt = False
    start_ts: datetime | None = None

    for p in raw_points:
        ts_utc: datetime = p["timestamp_utc"]
        if p.get("dsl_event_active"):
            if not in_evt:
                in_evt = True
                start_ts = ts_utc
        else:
            if in_evt and start_ts is not None:
                end_ts = ts_utc
                events.append(_format_event(start_ts, end_ts))
                in_evt = False
                start_ts = None

    if in_evt and start_ts is not None and raw_points:
        end_ts = raw_points[-1]["timestamp_utc"]
        events.append(_format_event(start_ts, end_ts))

    events.sort(key=lambda e: e["start_utc"], reverse=True)
    return events


def load_data() -> Dict[str, Any]:
    raw_points = _load_raw_points()
    agg_points = _aggregate_buckets(raw_points, bucket_minutes=BUCKET_MINUTES)
    events = _detect_outages(raw_points)

    last_updated_utc: str | None = None
    dsl_event_active = False
    if raw_points:
        last_updated_utc = raw_points[-1]["timestamp_utc"].isoformat()
        dsl_event_active = bool(raw_points[-1].get("dsl_event_active"))

    return {
        "points": agg_points,
        "events": events,
        "last_updated_utc": last_updated_utc,
        "bucket_minutes": BUCKET_MINUTES,
        "dsl_event_active": dsl_event_active,
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


@app.route("/api/http_probe_status")
def api_http_probe_status():
    """Return the HTTP probe status.

    Priority:
    1. Live check (cached for _LIVE_PROBE_TTL seconds) – always gives a fresh
       result without waiting for the probe process to write the first CSV row.
    2. Falls back to the last CSV entry if the live check itself fails.
    """
    global _live_probe_last_ok, _live_probe_last_error, _live_probe_last_ts

    now_mono = time.monotonic()

    with _live_probe_lock:
        cache_age = now_mono - _live_probe_last_ts
        need_refresh = cache_age >= _LIVE_PROBE_TTL

    if need_refresh:
        # Run a fresh check (outside the lock so we don't block other requests)
        try:
            resp = requests.get(
                HTTP_PROBE_URL,
                timeout=HTTP_PROBE_TIMEOUT_SECONDS,
                headers={"User-Agent": "dsl-monitor/1.0"},
                allow_redirects=True,
            )
            ok = resp.status_code < 400
            err = None if ok else f"HTTP {resp.status_code}"
        except requests.Timeout:
            ok, err = False, "timeout"
        except Exception as exc:  # noqa: BLE001
            ok, err = False, str(exc)

        with _live_probe_lock:
            _live_probe_last_ok = ok
            _live_probe_last_error = err
            _live_probe_last_ts = time.monotonic()

    with _live_probe_lock:
        last_ok = _live_probe_last_ok
        last_error = _live_probe_last_error

    return jsonify({"last_ok": last_ok, "last_error": last_error, "last_check_utc": datetime.now(timezone.utc).isoformat()})


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

        return jsonify({"ok": True, "connection_type": ct})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/check_dsl_now", methods=["POST"])
def api_check_dsl_now():
    """Ad-hoc DSL check.

    Runs a Fritz status fetch + an HTTP probe download right now and returns results.
    This is independent from the probe daemon and meant for manual verification.
    """

    # Fritz status
    try:
        url = os.environ.get("DSL_CONN_STATUS_URL", "http://127.0.0.1:9077/status")
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=FRITZ_STATUS_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        ct = str(data.get("connection_type", "unknown")).lower()
        if ct not in {"dsl", "mobile", "unknown"}:
            ct = "unknown"
        fritz: dict[str, object] = {"ok": True, "connection_type": ct}
    except Exception as exc:  # noqa: BLE001
        fritz = {"ok": False, "error": str(exc)}

    # HTTP probe (forced refresh)
    try:
        resp = requests.get(
            HTTP_PROBE_URL,
            timeout=HTTP_PROBE_TIMEOUT_SECONDS,
            headers={"User-Agent": "dsl-monitor/1.0"},
            allow_redirects=True,
        )
        http_ok = resp.status_code < 400
        http_probe: dict[str, object] = {"ok": http_ok, "status": int(resp.status_code)}
        if not http_ok:
            http_probe["error"] = f"HTTP {resp.status_code}"
    except requests.Timeout:
        http_probe = {"ok": False, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        http_probe = {"ok": False, "error": str(exc)}

    return jsonify(
        {
            "fritz": fritz,
            "http_probe": http_probe,
            "checked_utc": datetime.now(timezone.utc).isoformat(),
        }
    )


def main() -> int:
    host = os.environ.get("HOST", os.environ.get("DSL_MONITOR_WEB_HOST", "0.0.0.0"))
    port = int(os.environ.get("PORT", os.environ.get("DSL_MONITOR_WEB_PORT", "9076")))

    # Keep Flask logging reasonable under systemd/IDE.
    print(f"Starting web.py – UI on: http://{host}:{port}", flush=True)
    app.run(host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

