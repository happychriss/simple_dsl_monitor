#!/usr/bin/env python3

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests
from flask import Flask, jsonify, render_template_string

from db import DB_PATH, ensure_schema, get_connection, query_measurements

LOG_PATH = DB_PATH
RETENTION_DAYS = int(os.environ.get("DSL_MONITOR_RETENTION_DAYS", "7"))

# Keep these in sync with probe.py to display current settings in the UI.
PING_INTERVAL_SECONDS = int(os.environ.get("DSL_MONITOR_PING_INTERVAL_SECONDS", "15"))
FAILURE_THRESHOLD = int(os.environ.get("DSL_MONITOR_FAILURE_THRESHOLD", "3"))
PING_LATENCY_THRESHOLD_MS = float(os.environ.get("DSL_MONITOR_PING_LATENCY_THRESHOLD_MS", "100"))

BUCKET_MINUTES = int(os.environ.get("DSL_MONITOR_BUCKET_MINUTES", "5"))
MOBILE_YELLOW_THRESHOLD_SECONDS = int(os.environ.get("DSL_MONITOR_MOBILE_YELLOW_THRESHOLD_SECONDS", "300"))

# Marker/"spike" detection in bucketed latency display
# outside_fraction >= threshold => show extreme markers
OUTSIDE_FRACTION_THRESHOLD = float(os.environ.get("DSL_MONITOR_OUTSIDE_FRACTION_THRESHOLD", "0.05"))
# Also show a P95 dot when an extreme is triggered (1=true, 0=false)
SHOW_P95_MARKER = os.environ.get("DSL_MONITOR_SHOW_P95_MARKER", "0") in {"1", "true", "True", "yes", "on"}

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
    .layout { display: flex; flex-direction: column; gap: 24px; }
    .chart-panel { width: 100%; min-width: 0; }
    .table-panel { width: 100%; min-width: 0; }
    #latency { width: 100%; height: 70vh; min-height: 400px; }
    table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; font-size: 0.9rem; }
    th, td { padding: 6px 8px; border-bottom: 1px solid #333; text-align: left; }
    th { background: #1a202c; }
    tr:nth-child(even) { background: #1a1a1a; }
    .outage { color: #f56565; font-weight: 500; }
    .warning { color: #ecc94b; font-weight: 500; }
    a { color: #4fd1c5; }
    @media (max-width: 1000px) {
      .chart-panel, .table-panel { width: 100%; }
      #latency { height: 50vh; }
    }
  </style>
</head>
<body>
  <h1 id="page-title">DSL Stability Monitor</h1>
  <div class="subtitle" id="page-subtitle"></div>

  <div style="margin-bottom: 0.75rem; font-size: 0.9rem; color: #ccc;">
    <span id="current-datetime"></span>
    <span style="margin-left: 1.5rem;">Last data update: <span id="last-updated"></span></span>
    <span style="margin-left: 1.5rem;">Fritz status: <span id="fritz-status"></span></span>
    <span style="margin-left: 1.5rem;">HTTP probe: <span id="http-probe-status"></span></span>
    <span style="margin-left: 1.5rem;">
      <label for="auto-refresh-toggle" style="display:inline-flex; align-items:center; gap:0.35rem; color:#ccc; cursor:pointer;">
        <input id="auto-refresh-toggle" type="checkbox" checked>
        <span id="auto-refresh-label">Refresh on</span>
      </label>
    </span>
    <span style="margin-left: 1.5rem;">
      <button id="check-dsl" style="padding: 4px 10px; background:#1a202c; color:#eee; border:1px solid #333; border-radius:4px; cursor:pointer;">Check DSL now</button>
      <span id="check-dsl-result" style="margin-left: 0.75rem; color:#a0aec0;"></span>
    </span>
  </div>

  <div class="layout">
    <section class="chart-panel">
      <div id="latency"></div>
    </section>

    <section class="table-panel">
      <h2 style="margin-top:0;">Outage Events (last 7 days)</h2>
      <table id="events-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Start (local)</th>
            <th>Duration</th>
            <th>Trig</th>
            <th>Mobile</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </section>
  </div>

  <script>
    let lastFritzStatus = null;
    let lastFritzFetchMs = 0;
    let outageActive = false;
    let latencyChartInitialized = false;
    const AUTO_REFRESH_STORAGE_KEY = 'dsl-monitor-auto-refresh-enabled';
    let autoRefreshEnabled = true;
    let autoRefreshIntervalId = null;
    // Explicitly tracked user zoom range (null = show full range / autorange).
    let userZoomRange = null;

    function formatDuration(seconds) {
      if (seconds == null) return '';
      const s = Math.round(seconds);
      const mins = Math.floor(s / 60);
      const rem = s % 60;
      if (mins === 0) return `${rem}s`;
      if (rem === 0) return `${mins} min`;
      return `${mins}m ${rem}s`;
    }

    function formatTrigger(trigger) {
      const labels = {
        ping_failures: 'Ping',
        high_latency: 'Latenz',
        http_timeout: 'HTTP',
        fritz_mobile: 'Fritz',
      };
      return labels[trigger] || (trigger || '');
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

    function updateAutoRefreshUi() {
      const toggle = document.getElementById('auto-refresh-toggle');
      const label = document.getElementById('auto-refresh-label');
      if (toggle) toggle.checked = autoRefreshEnabled;
      if (label) label.textContent = autoRefreshEnabled ? 'Refresh on' : 'Refresh off';
    }

    function loadAutoRefreshPreference() {
      try {
        const stored = window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY);
        if (stored === 'true') return true;
        if (stored === 'false') return false;
      } catch {
        // Ignore storage access issues and fall back to default.
      }
      return true;
    }

    function persistAutoRefreshPreference() {
      try {
        window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, String(autoRefreshEnabled));
      } catch {
        // Ignore storage access issues; the toggle still works for the session.
      }
    }

    function startAutoRefresh() {
      if (!autoRefreshEnabled) return;
      if (autoRefreshIntervalId !== null) return;
      autoRefreshIntervalId = setInterval(loadData, 30000);
    }

    function stopAutoRefresh() {
      if (autoRefreshIntervalId === null) return;
      clearInterval(autoRefreshIntervalId);
      autoRefreshIntervalId = null;
    }

    function setAutoRefreshEnabled(enabled) {
      autoRefreshEnabled = !!enabled;
      if (autoRefreshEnabled) {
        startAutoRefresh();
      } else {
        stopAutoRefresh();
      }
      persistAutoRefreshPreference();
      updateAutoRefreshUi();
    }

    async function loadFritzStatusIfNeeded(force=false) {
      // Hard throttle: only poll Fritz while a ping event is active, and then max 1/min.
      if (!outageActive) {
        lastFritzStatus = null;
        setFritzUi('', '#a0aec0');
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
      setFritzUi('checking', '#a0aec0');

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
            color = '#63b3ed';
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

      // Dynamic header/subtitle from backend config (no hardcoding).
      const titleEl = document.getElementById('page-title');
      if (titleEl && data.ui_title) titleEl.textContent = data.ui_title;

      const subtitleEl = document.getElementById('page-subtitle');
      if (subtitleEl && data.ui_subtitle) subtitleEl.textContent = data.ui_subtitle;

      // Determine whether a ping event is currently active.
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

      // Keep both charts perfectly aligned by using the exact same x-range.
      // This avoids the "shift to the right" effect, especially on mobile.
      const xMin = times.length ? times[0] : null;
      const xMax = times.length ? times[times.length - 1] : null;
      const tickValues = [];
      const tickTexts = [];
      if (xMin && xMax) {
        const tick = new Date(xMin);
        tick.setMinutes(0, 0, 0);
        const hour = tick.getHours();
        const remainder = hour % 6;
        if (remainder !== 0) {
          tick.setHours(hour + (6 - remainder));
        }
        while (tick <= xMax) {
          tickValues.push(new Date(tick));
          const hh = String(tick.getHours()).padStart(2, '0');
          const mm = String(tick.getMinutes()).padStart(2, '0');
          if (tick.getHours() === 0) {
            const dd = String(tick.getDate()).padStart(2, '0');
            const mo = String(tick.getMonth() + 1).padStart(2, '0');
            tickTexts.push(`${hh}:${mm}<br>${dd}.${mo}.`);
          } else {
            tickTexts.push(`${hh}:${mm}`);
          }
          tick.setHours(tick.getHours() + 6);
        }
      }
      const pingColors = data.points.map(p => {
        if (p.status !== 'outage') {
          return '#48bb78';
        }
        if (p.connection_type === 'mobile') {
          return '#ecc94b';
        }
        return '#f56565';
      });
      const pingSizes = data.points.map(p => (p.status === 'outage' || p.connection_type === 'mobile' ? 8 : 3));

      const latencyTrace = {
        x: times,
        y: latencies,
        mode: 'lines+markers',
        marker: { color: pingColors, size: pingSizes },
        line: { color: '#718096', width: 1 },
        name: `Ping latency P50 (ms)   ${data.bucket_minutes} min buckets`,
        customdata: data.points.map(p => [
          p.first_sample_local || '',
          p.connection_type || 'unknown',
          p.status || 'ok',
          p.latency_max == null ? '—' : `${Number(p.latency_max).toFixed(1)} ms`,
          formatTrigger(p.dsl_event_trigger) || '—',
          p.max_mobile_duration_seconds == null ? '—' : formatDuration(p.max_mobile_duration_seconds),
        ]),
        hovertemplate:
          'Zeit: %{customdata[0]}<br>' +
          'Latenz P50: %{y:.1f} ms<br>' +
          'Max: %{customdata[3]}<br>' +
          'Status: %{customdata[2]}<br>' +
          'Verbindung: %{customdata[1]}<br>' +
          'Trigger: %{customdata[4]}<br>' +
          'Mobile: %{customdata[5]}' +
          '<extra></extra>'
      };

      // Extreme markers: only for buckets where marker_triggered==true, capped at 300ms
      const PEAK_CAP_MS = 300;
      const p95MarkerX = [];
      const p95MarkerY = [];
      const p95MarkerSizes = [];
      for (const p of data.points) {
        if (!p.marker_triggered) continue;
        const t = new Date(p.timestamp);
        if (p.latency_p95 != null) {
          const capped = p.latency_p95 > PEAK_CAP_MS;
          p95MarkerX.push(t);
          p95MarkerY.push(capped ? PEAK_CAP_MS : p.latency_p95);
          p95MarkerSizes.push(capped ? 5 : 3);
        }
      }

      const p95MarkerTrace = {
        x: p95MarkerX,
        y: p95MarkerY,
        mode: 'markers',
        marker: { color: '#444', size: p95MarkerSizes, symbol: 'diamond' },
        name: 'P95 (triggered)'
      };

      const latencyLayout = {
        paper_bgcolor: '#111',
        plot_bgcolor: '#111',
        font: { color: '#eee' },
        uirevision: 'dsl-monitor',
        showlegend: true,
        legend: {
          orientation: 'h',
          x: 0,
          xanchor: 'left',
          y: 1.25,
          yanchor: 'bottom',
          bgcolor: 'rgba(0,0,0,0)'
        },
        xaxis: {
          title: 'Time',
          range: userZoomRange ? userZoomRange
               : (!latencyChartInitialized && xMin && xMax) ? [xMin, xMax]
               : undefined,
          domain: [0, 1],
          automargin: true,
          ticklabeloverflow: 'hide past domain',
          tickmode: tickValues.length > 0 ? 'array' : 'auto',
          tickvals: tickValues.length > 0 ? tickValues : undefined,
          ticktext: tickTexts.length > 0 ? tickTexts : undefined,
        },
        yaxis: { title: 'Ping latency (ms)', type: 'log' },
        // Use identical margins across both plots so the plot AREA aligns.
        // Reserve space at the top for the legend (above plot area).
        margin: { t: 70, r: 10, b: 40, l: 60 }
      };

      const traces = [latencyTrace];
      if (p95MarkerX.length > 0) traces.push(p95MarkerTrace);
      Plotly.react('latency', traces, latencyLayout, {responsive: true});
      latencyChartInitialized = true;

      // Attach zoom-tracking listener once after the chart is first created.
      const plotEl = document.getElementById('latency');
      if (plotEl && !plotEl._zoomListenerAttached) {
        plotEl._zoomListenerAttached = true;
        plotEl.on('plotly_relayout', (eventData) => {
          const r0 = eventData['xaxis.range[0]'];
          const r1 = eventData['xaxis.range[1]'];
          if (r0 !== undefined && r1 !== undefined) {
            // User zoomed or panned – remember their range.
            userZoomRange = [r0, r1];
          } else if (eventData['xaxis.autorange'] === true) {
            // User double-clicked to reset – go back to full range on next refresh.
            userZoomRange = null;
          }
        });
      }

      const tbody = document.querySelector('#events-table tbody');
      tbody.innerHTML = '';

      if (data.events && data.events.length > 0) {
        data.events
          .filter(ev => (ev.duration_seconds || 0) >= 10)
          .forEach((ev, idx) => {
          const tr = document.createElement('tr');
          tr.classList.add(ev.connection_type === 'dsl' ? 'warning' : 'outage');

          const tdIdx = document.createElement('td');
          tdIdx.textContent = idx + 1;

          const tdStart = document.createElement('td');
          tdStart.textContent = ev.start_local;

          const tdDur = document.createElement('td');
          tdDur.textContent = formatDuration(ev.duration_seconds);

          const tdTrigger = document.createElement('td');
          tdTrigger.textContent = formatTrigger(ev.dsl_event_trigger);

          const tdMobile = document.createElement('td');
          tdMobile.textContent = formatDuration(ev.mobile_duration_seconds);

          tr.appendChild(tdIdx);
          tr.appendChild(tdStart);
          tr.appendChild(tdDur);
          tr.appendChild(tdTrigger);
          tr.appendChild(tdMobile);
          tbody.appendChild(tr);
        });
      }
    }

    updateCurrentDateTime();
    setInterval(updateCurrentDateTime, 1000);

    // Initial load (always runs once regardless of refresh setting).
    loadData();

    // Restore auto-refresh preference and start timer only when enabled.
    setAutoRefreshEnabled(loadAutoRefreshPreference());

    // NOTE: Do NOT poll Fritz in a separate 10s loop.
    // Fritz is refreshed exclusively from loadData(), and that function already
    // applies a hard 1/min throttle while an outage is active.

    async function checkDslNow() {
      const btn = document.getElementById('check-dsl');
      const out = document.getElementById('check-dsl-result');
      if (!btn || !out) return;

      btn.disabled = true;
      out.textContent = 'checking';
      out.style.color = '#a0aec0';

      try {
        const resp = await fetch('/api/check_dsl_now', { method: 'POST' });
        const data = await resp.json();

        // Update status line components immediately
        if (data.fritz && data.fritz.ok) {
          let text, color;
          if (data.fritz.connection_type === 'dsl') { text = 'dsl'; color = '#63b3ed'; }
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

    const autoRefreshToggle = document.getElementById('auto-refresh-toggle');
    if (autoRefreshToggle) {
      autoRefreshToggle.addEventListener('change', (event) => {
        setAutoRefreshEnabled(event.target.checked);
      });
    }
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


def _load_raw_points() -> List[Dict[str, Any]]:
    """Load measurement rows from local SQLite, filtered to RETENTION_DAYS."""
    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    conn = get_connection(LOG_PATH)
    ensure_schema(conn)
    rows = query_measurements(conn, since_utc=cutoff_utc)

    points: List[Dict[str, Any]] = []
    for row in rows:
        try:
            ts_utc = datetime.fromisoformat(row["timestamp"])
        except Exception:
            continue
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=timezone.utc)

        points.append(
            {
                "timestamp_utc": ts_utc,
                "ping_ok": bool(row.get("ping_ok")),
                "latency_ms": row.get("latency_ms"),
                "ping_target": row.get("ping_target", ""),
                "connection_type": (row.get("connection_type") or "unknown").lower(),
                "mobile_duration_seconds": row.get("mobile_duration_seconds"),
                "dsl_event_active": bool(row.get("dsl_event_active")),
                "dsl_event_trigger": row.get("dsl_event_trigger") or "",
            }
        )

    points.sort(key=lambda p: p["timestamp_utc"])
    return points


def _aggregate_buckets(raw_points: List[Dict[str, Any]], bucket_minutes: int = 5) -> List[Dict[str, Any]]:
    buckets: dict[datetime, Dict[str, Any]] = {}

    def _percentile(sorted_vals: list[float], p: float) -> float | None:
        """Linear-interpolated percentile (inclusive endpoints).

        p in [0, 1]. Returns None if no values.
        """
        if not sorted_vals:
            return None
        if p <= 0:
            return float(sorted_vals[0])
        if p >= 1:
            return float(sorted_vals[-1])

        n = len(sorted_vals)
        if n == 1:
            return float(sorted_vals[0])

        # Similar to numpy.percentile with linear interpolation.
        pos = p * (n - 1)
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)

    for p in raw_points:
        ts_utc: datetime = p["timestamp_utc"]
        bucket_ts = _bucket_start(ts_utc, minutes=bucket_minutes)

        b = buckets.setdefault(
            bucket_ts,
            {
                # Keep raw successful ping latencies for percentile/marker logic.
                "latencies": [],
                "has_outage": False,
                "has_mobile": False,
                "has_dsl": False,
                "first_sample_utc": ts_utc,
                "event_trigger": "",
                "max_mobile_duration_seconds": None,
            },
        )

        if ts_utc < b["first_sample_utc"]:
            b["first_sample_utc"] = ts_utc

        if p.get("latency_ms") is not None and p.get("ping_ok"):
            b["latencies"].append(float(p["latency_ms"]))

        if p.get("dsl_event_active"):
            b["has_outage"] = True
            if not b["event_trigger"]:
                b["event_trigger"] = str(p.get("dsl_event_trigger") or "")

        mobile_duration_seconds = p.get("mobile_duration_seconds")
        if mobile_duration_seconds is not None:
            current_mobile_max = b["max_mobile_duration_seconds"]
            mobile_duration = float(mobile_duration_seconds)
            if current_mobile_max is None or mobile_duration > current_mobile_max:
                b["max_mobile_duration_seconds"] = mobile_duration

        ct = str(p.get("connection_type") or "unknown").lower()
        if ct == "mobile":
            b["has_mobile"] = True
        elif ct == "dsl":
            b["has_dsl"] = True

    agg_points: List[Dict[str, Any]] = []
    for ts in sorted(buckets.keys()):
        b = buckets[ts]

        lats = sorted([float(x) for x in b.get("latencies", [])])
        p50 = _percentile(lats, 0.50)
        p90 = _percentile(lats, 0.90)
        p95 = _percentile(lats, 0.95)
        lat_max = float(lats[-1]) if lats else None

        # Bucket baseline line value (P50 / median)
        lat = p50

        # Spike threshold logic:
        # U = m + max(0.2m, 3(P90-P50), 5ms)
        # Trigger markers only if outside_fraction >= OUTSIDE_FRACTION_THRESHOLD
        U = None
        outside_fraction = 0.0
        marker_triggered = False
        if p50 is not None and p90 is not None and lats:
            m = float(p50)
            spread = float(p90) - float(p50)
            bump = max(0.2 * m, 3.0 * spread, 5.0)
            U = m + bump
            outside_count = sum(1 for v in lats if v > U)
            outside_fraction = outside_count / max(1, len(lats))
            marker_triggered = outside_fraction >= OUTSIDE_FRACTION_THRESHOLD

        status = "outage" if b["has_outage"] else "ok"
        first_sample_utc: datetime = b["first_sample_utc"]

        # Pick a representative connection type for the bucket.
        # If we have no Fritz info in the CSV for that bucket, we default to DSL.
        if b["has_mobile"]:
            bucket_ct = "mobile"
        elif b["has_dsl"]:
            bucket_ct = "dsl"
        else:
            bucket_ct = "dsl"

        agg_points.append(
            {
                "timestamp": ts.isoformat(),
                "first_sample_utc": first_sample_utc.isoformat(),
                "first_sample_local": first_sample_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "latency_ms": lat,
                "latency_u": U,
                "outside_fraction": outside_fraction,
                "marker_triggered": marker_triggered,
                "latency_max": lat_max,
                "latency_p95": (p95 if (marker_triggered and SHOW_P95_MARKER) else None),
                "status": status,
                "connection_type": bucket_ct,
                "max_outage_duration_seconds": None,
                "max_mobile_duration_seconds": b["max_mobile_duration_seconds"],
                "dsl_event_trigger": b["event_trigger"],
            }
        )

    return agg_points


def _format_event(
    start_utc: datetime,
    end_utc: datetime,
    dsl_event_trigger: str = "",
    mobile_duration_seconds: float | None = None,
    connection_type: str = "unknown",
) -> Dict[str, Any]:
    duration = max(0.0, (end_utc - start_utc).total_seconds())
    start_local = start_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    end_local = end_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
        "start_local": start_local,
        "end_local": end_local,
        "duration_seconds": duration,
        "dsl_event_trigger": dsl_event_trigger,
        "mobile_duration_seconds": mobile_duration_seconds,
        "connection_type": connection_type,
    }


def _detect_outages(raw_points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detect events as contiguous sequences where `dsl_event_active` is True."""

    events: List[Dict[str, Any]] = []
    in_evt = False
    start_ts: datetime | None = None
    start_trigger = ""
    event_mobile_duration_seconds: float | None = None
    last_connection_type = "unknown"

    for p in raw_points:
        ts_utc: datetime = p["timestamp_utc"]
        if p.get("dsl_event_active"):
            if not in_evt:
                in_evt = True
                start_ts = ts_utc
                start_trigger = str(p.get("dsl_event_trigger") or "")
                event_mobile_duration_seconds = p.get("mobile_duration_seconds")
                last_connection_type = str(p.get("connection_type") or "unknown").lower()
            else:
                if p.get("mobile_duration_seconds") is not None:
                    event_mobile_duration_seconds = p.get("mobile_duration_seconds")
                last_connection_type = str(p.get("connection_type") or last_connection_type or "unknown").lower()
        else:
            if in_evt and start_ts is not None:
                end_ts = ts_utc
                if p.get("mobile_duration_seconds") is not None:
                    event_mobile_duration_seconds = p.get("mobile_duration_seconds")
                last_connection_type = str(p.get("connection_type") or last_connection_type or "unknown").lower()
                events.append(_format_event(start_ts, end_ts, start_trigger, event_mobile_duration_seconds, last_connection_type))
                in_evt = False
                start_ts = None
                start_trigger = ""
                event_mobile_duration_seconds = None

    if in_evt and start_ts is not None and raw_points:
        end_ts = raw_points[-1]["timestamp_utc"]
        events.append(_format_event(start_ts, end_ts, start_trigger, event_mobile_duration_seconds, last_connection_type))

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

    ui_title = "DSL Stability Monitor"
    lat_thr = PING_LATENCY_THRESHOLD_MS
    lat_thr_txt = "disabled" if lat_thr <= 0 else f"{int(lat_thr) if lat_thr.is_integer() else lat_thr}ms"
    ui_subtitle = (
        f"Last {RETENTION_DAYS} days · ping every {PING_INTERVAL_SECONDS}s · "
        f"bucket size {BUCKET_MINUTES} min · latency threshold {lat_thr_txt} · "
        f"failure threshold {FAILURE_THRESHOLD}"
    )

    return {
        "points": agg_points,
        "events": events,
        "last_updated_utc": last_updated_utc,
        "bucket_minutes": BUCKET_MINUTES,
        "dsl_event_active": dsl_event_active,
        "ui_title": ui_title,
        "ui_subtitle": ui_subtitle,
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

