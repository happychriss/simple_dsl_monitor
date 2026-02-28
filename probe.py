#!/usr/bin/env python3
"""DSL Monitor – ping + HTTP probe + Fritz connection type.

DSL event definition:
- Starts if: (a) 3 consecutive ping failures, OR (b) HTTP probe times out,
  OR (c) ping latency exceeds DSL_MONITOR_PING_LATENCY_THRESHOLD_MS (default 100ms,
  indicating mobile-fallback routing).
- While active: poll Fritz connection_type at most once per minute.
- Ends if pings recover AND Fritz reports connection_type == 'dsl', OR after 45 minutes.

Probe writes a compact CSV that the web UI reads.
"""

import csv
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional, Tuple, cast

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PING_TARGET = os.environ.get("DSL_MONITOR_PING_TARGET", "8.8.8.8")

LOG_PATH = os.environ.get(
    "DSL_MONITOR_LOG",
    os.path.join(os.path.dirname(__file__), "dsl_log.csv"),
)

# Retention for the web UI display only.
RETENTION_DAYS = int(os.environ.get("DSL_MONITOR_RETENTION_DAYS", "7"))

# CSV pruning retention (0/empty = keep forever)
CSV_RETENTION_DAYS = int(os.environ.get("DSL_MONITOR_CSV_RETENTION_DAYS", "0"))

PING_INTERVAL_SECONDS = int(os.environ.get("DSL_MONITOR_PING_INTERVAL_SECONDS", "15"))
CONSECUTIVE_FAILURES_THRESHOLD = int(os.environ.get("DSL_MONITOR_FAILURE_THRESHOLD", "3"))
PING_TIMEOUT_SECONDS = float(os.environ.get("DSL_MONITOR_PING_TIMEOUT_SECONDS", "5"))

# Fritz connection-type helper (local status bridge)
CONN_STATUS_URL = os.environ.get("DSL_CONN_STATUS_URL", "http://127.0.0.1:9077/status")
ConnectionType = Literal["dsl", "mobile", "unknown"]
CONN_TYPE_POLL_INTERVAL_SECONDS = int(os.environ.get("DSL_MONITOR_CONN_TYPE_POLL_INTERVAL_SECONDS", "60"))

# Normal-mode Fritz polling interval (seconds). Even when no event is active we
# periodically check Fritz so we can detect a dsl→mobile switch proactively.
CONN_TYPE_NORMAL_POLL_INTERVAL_SECONDS = int(os.environ.get("DSL_MONITOR_CONN_TYPE_NORMAL_POLL_INTERVAL_SECONDS", "1200"))  # 20min

MOBILE_YELLOW_THRESHOLD_SECONDS = int(os.environ.get("DSL_MONITOR_MOBILE_YELLOW_THRESHOLD_SECONDS", "300"))

# Generic HTTP probe (configurable URL)
HTTP_PROBE_URL = os.environ.get("DSL_MONITOR_HTTP_PROBE_URL", "https://www.tagesschau.de/tagesthemen")
HTTP_PROBE_INTERVAL_SECONDS = int(os.environ.get("DSL_MONITOR_HTTP_PROBE_INTERVAL_SECONDS", "300"))
HTTP_PROBE_TIMEOUT_SECONDS = float(os.environ.get("DSL_MONITOR_HTTP_PROBE_TIMEOUT_SECONDS", "15"))

# Latency threshold (ms): a ping that arrives but with latency above this value
# is treated as a failure (e.g. mobile-fallback routing).  0 = disabled.
PING_LATENCY_THRESHOLD_MS = float(os.environ.get("DSL_MONITOR_PING_LATENCY_THRESHOLD_MS", "100"))

DslEventTrigger = Literal["", "ping_failures", "http_timeout", "high_latency", "fritz_mobile"]
DslEventEndReason = Literal["", "recovered_to_dsl", "max_duration"]

DSL_EVENT_MAX_SECONDS = int(os.environ.get("DSL_MONITOR_DSL_EVENT_MAX_SECONDS", "2700"))  # 45min

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_running = True
_last_signal: int | None = None


def _handle_signal(signum, frame):  # noqa: ANN001
    global _running, _last_signal
    _last_signal = int(signum)
    print(f"DSL Monitor probe received signal {signum} – stopping…", flush=True)
    _running = False


def _excepthook(exc_type, exc, tb):  # noqa: ANN001
    import traceback

    traceback.print_exception(exc_type, exc, tb)


sys.excepthook = _excepthook

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Generic HTTP probe state (thread-safe via lock)
# ---------------------------------------------------------------------------


@dataclass
class HttpProbeState:
    last_ok: Optional[bool] = None
    last_check_utc: Optional[datetime] = None
    last_error: Optional[str] = None
    last_timeout_utc: Optional[datetime] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, ok: bool, error: Optional[str]) -> None:
        with self._lock:
            self.last_ok = ok
            self.last_check_utc = datetime.now(timezone.utc)
            self.last_error = error
            if ok:
                return
            if (error or "").strip().lower() == "timeout":
                self.last_timeout_utc = self.last_check_utc

    def snapshot(self) -> tuple[Optional[bool], Optional[str], Optional[datetime]]:
        with self._lock:
            return self.last_ok, self.last_error, self.last_timeout_utc


_http_probe_state = HttpProbeState()


def _http_probe_worker() -> None:
    global _running
    next_run = time.monotonic()
    while _running:
        now_mono = time.monotonic()
        if now_mono >= next_run:
            next_run = now_mono + HTTP_PROBE_INTERVAL_SECONDS
            try:
                resp = requests.get(
                    HTTP_PROBE_URL,
                    timeout=HTTP_PROBE_TIMEOUT_SECONDS,
                    headers={"User-Agent": "dsl-monitor/1.0"},
                    allow_redirects=True,
                )
                if resp.status_code < 400:
                    _http_probe_state.update(True, None)
                else:
                    _http_probe_state.update(False, f"HTTP {resp.status_code}")
            except requests.Timeout:
                _http_probe_state.update(False, "timeout")
            except Exception as exc:  # noqa: BLE001
                _http_probe_state.update(False, str(exc))
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------


def ensure_log_header(path: str) -> None:
    """Ensure CSV exists with the expected header.

    No backwards compatibility: if an existing header differs, the file is re-initialized.
    """

    header = [
        "timestamp",
        "ping_target",
        "ping_ok",
        "latency_ms",
        "consecutive_failures",
        "dsl_event_active",
        "dsl_event_trigger",
        "dsl_event_duration_seconds",
        "dsl_event_end_reason",
        "connection_type",
        "mobile_duration_seconds",
        "http_probe_ok",
        "http_probe_error",
    ]

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(header)
        return

    try:
        with open(path, "r", newline="") as f:
            first = next(csv.reader(f), [])
        if [c.strip() for c in first] != header:
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(header)
    except Exception:
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(header)


def append_and_prune_log(path: str, row: list) -> None:
    ensure_log_header(path)
    with open(path, "a", newline="") as f:
        csv.writer(f).writerow(row)

    # Keep all measurements by default. Enable pruning explicitly via env var.
    if CSV_RETENTION_DAYS <= 0:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=CSV_RETENTION_DAYS)

    rows: list[list] = []
    with open(path, "r", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return
        rows.append(header)
        for r in reader:
            try:
                ts = datetime.fromisoformat(r[0])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if ts >= cutoff:
                rows.append(r)

    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


# ---------------------------------------------------------------------------
# Ping probe
# ---------------------------------------------------------------------------


def probe_ping(target: str, timeout: float = PING_TIMEOUT_SECONDS) -> Tuple[bool, float]:
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), target]
    else:
        cmd = ["ping", "-c", "1", "-W", str(int(timeout)), target]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 1,
            text=True,
        )

        if result.returncode == 0:
            output = result.stdout.lower()
            for line in output.split("\n"):
                if "time=" in line:
                    try:
                        time_str = line.split("time=")[1].split()[0]
                        return True, float(time_str.replace("ms", ""))
                    except Exception:
                        break
            return True, 0.0

        return False, 0.0
    except Exception:
        return False, 0.0


# ---------------------------------------------------------------------------
# Fritz status (connection type)
# ---------------------------------------------------------------------------


def get_fritz_status() -> dict:
    try:
        resp = requests.get(CONN_STATUS_URL, timeout=6.0)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


_conn_type_last_fetch_mono: float = 0.0
_conn_type_last_value: ConnectionType = "unknown"


def _reset_fritz_poll_for_event() -> None:
    """Reset the Fritz poll timer so the next call fetches immediately (60s rate)."""
    global _conn_type_last_fetch_mono
    _conn_type_last_fetch_mono = 0.0


def get_connection_type_if_outage(in_outage: bool) -> ConnectionType:
    """Fetch Fritz connection type with rate-limiting.

    Two modes:
    - Normal (in_outage=False): poll every CONN_TYPE_NORMAL_POLL_INTERVAL_SECONDS (20min).
      This lets us detect dsl→mobile switches proactively.
    - Event  (in_outage=True):  poll every CONN_TYPE_POLL_INTERVAL_SECONDS (60s).
    """
    global _conn_type_last_fetch_mono, _conn_type_last_value

    now_mono = time.monotonic()

    if in_outage:
        interval = float(CONN_TYPE_POLL_INTERVAL_SECONDS)
    else:
        interval = float(CONN_TYPE_NORMAL_POLL_INTERVAL_SECONDS)

    if (now_mono - _conn_type_last_fetch_mono) < interval:
        return cast(ConnectionType, _conn_type_last_value)

    _conn_type_last_fetch_mono = now_mono
    try:
        data = get_fritz_status()
        ct = str(data.get("connection_type", "unknown")).lower()
        if ct in {"dsl", "mobile", "unknown"}:
            _conn_type_last_value = cast(ConnectionType, ct)
        else:
            _conn_type_last_value = "unknown"
    except Exception:
        _conn_type_last_value = "unknown"

    return cast(ConnectionType, _conn_type_last_value)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


@dataclass
class OutageState:
    consecutive_failures: int = 0
    in_outage: bool = False
    outage_start_utc: Optional[datetime] = None
    mobile_start_utc: Optional[datetime] = None
    last_connection_type: ConnectionType = "unknown"

    dsl_event_active: bool = False
    dsl_event_start_utc: Optional[datetime] = None
    dsl_event_trigger: DslEventTrigger = ""
    dsl_event_end_reason: DslEventEndReason = ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dsl_event_duration_seconds(state: OutageState, now_utc: datetime) -> Optional[float]:
    if not state.dsl_event_active or state.dsl_event_start_utc is None:
        return None
    return max(0.0, (now_utc - state.dsl_event_start_utc).total_seconds())


def update_state(
    state: OutageState,
    *,
    ping_ok: bool,
    now_utc: datetime,
    http_timeout_trigger: bool = False,
    latency_ms: float = 0.0,
    fritz_mobile_trigger: bool = False,
) -> OutageState:

    # --- fritz_mobile trigger (periodic 20min check found "mobile") ---
    if fritz_mobile_trigger and not state.dsl_event_active:
        state.dsl_event_active = True
        state.dsl_event_start_utc = now_utc
        state.dsl_event_trigger = "fritz_mobile"
        state.dsl_event_end_reason = ""
        state.last_connection_type = "mobile"
        state.mobile_start_utc = now_utc
        _reset_fritz_poll_for_event()

    # --- http_timeout trigger ---
    if http_timeout_trigger and not state.dsl_event_active:
        state.dsl_event_active = True
        state.dsl_event_start_utc = now_utc
        state.dsl_event_trigger = "http_timeout"
        state.dsl_event_end_reason = ""
        _reset_fritz_poll_for_event()

    # --- High-latency detection ---
    high_latency = bool(
        ping_ok
        and PING_LATENCY_THRESHOLD_MS > 0
        and latency_ms > PING_LATENCY_THRESHOLD_MS
    )
    if high_latency:
        ping_ok = False

    if high_latency and not state.dsl_event_active:
        state.dsl_event_active = True
        state.dsl_event_start_utc = now_utc
        state.dsl_event_trigger = "high_latency"
        state.dsl_event_end_reason = ""
        state.in_outage = True
        state.outage_start_utc = now_utc
        _reset_fritz_poll_for_event()

    # --- ping_ok branch ---
    if ping_ok:
        state.consecutive_failures = 0
        if state.in_outage:
            state.in_outage = False
            state.outage_start_utc = None
            state.mobile_start_utc = None

        if state.dsl_event_active:
            dur = _dsl_event_duration_seconds(state, now_utc)
            if dur is not None and dur >= DSL_EVENT_MAX_SECONDS:
                state.dsl_event_active = False
                state.dsl_event_end_reason = "max_duration"
                state.last_connection_type = get_connection_type_if_outage(False)
                return state

            state.last_connection_type = get_connection_type_if_outage(True)
            if state.last_connection_type == "dsl":
                state.dsl_event_active = False
                state.dsl_event_end_reason = "recovered_to_dsl"
                state.last_connection_type = get_connection_type_if_outage(False)
                return state

        state.last_connection_type = get_connection_type_if_outage(False)
        return state

    # --- ping_fail branch ---
    state.consecutive_failures += 1

    started_outage = False
    if not state.in_outage and state.consecutive_failures >= CONSECUTIVE_FAILURES_THRESHOLD:
        started_outage = True
        state.in_outage = True
        state.outage_start_utc = now_utc

        if not state.dsl_event_active:
            state.dsl_event_active = True
            state.dsl_event_start_utc = now_utc
            state.dsl_event_trigger = "ping_failures"
            state.dsl_event_end_reason = ""
            _reset_fritz_poll_for_event()

        state.last_connection_type = get_connection_type_if_outage(True)
        if state.last_connection_type == "mobile":
            state.mobile_start_utc = now_utc

    if state.dsl_event_active and not started_outage:
        dur = _dsl_event_duration_seconds(state, now_utc)
        if dur is not None and dur >= DSL_EVENT_MAX_SECONDS:
            state.dsl_event_active = False
            state.dsl_event_end_reason = "max_duration"
            state.last_connection_type = get_connection_type_if_outage(False)
            return state

        state.last_connection_type = get_connection_type_if_outage(True)
        if state.last_connection_type == "mobile":
            if state.mobile_start_utc is None:
                state.mobile_start_utc = now_utc
        else:
            state.mobile_start_utc = None

        # Do NOT end the DSL event while pings are still failing.
        # Fritz may report "dsl" (DSL sync is up) even though internet
        # routing is broken.  The event should only end when pings recover
        # AND Fritz confirms "dsl" – that branch is handled above in the
        # `if ping_ok:` section.

    return state


def compute_durations(state: OutageState, now_utc: datetime) -> tuple[Optional[float], Optional[float]]:
    outage_dur = None
    if state.in_outage and state.outage_start_utc is not None:
        outage_dur = max(0.0, (now_utc - state.outage_start_utc).total_seconds())

    mobile_dur = None
    if state.mobile_start_utc is not None:
        mobile_dur = max(0.0, (now_utc - state.mobile_start_utc).total_seconds())

    return outage_dur, mobile_dur


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> int:
    print(
        f"DSL Monitor probe starting. target={PING_TARGET} interval={PING_INTERVAL_SECONDS}s "
        f"threshold={CONSECUTIVE_FAILURES_THRESHOLD} timeout={PING_TIMEOUT_SECONDS}s "
        f"latency_threshold={PING_LATENCY_THRESHOLD_MS}ms "
        f"fritz_normal={CONN_TYPE_NORMAL_POLL_INTERVAL_SECONDS}s fritz_event={CONN_TYPE_POLL_INTERVAL_SECONDS}s",
        flush=True,
    )

    http_probe_thread = threading.Thread(target=_http_probe_worker, name="http-probe", daemon=True)
    http_probe_thread.start()
    print(
        f"HTTP probe started: {HTTP_PROBE_URL} every {HTTP_PROBE_INTERVAL_SECONDS}s "
        f"timeout={HTTP_PROBE_TIMEOUT_SECONDS}s",
        flush=True,
    )

    state = OutageState()

    while _running:
        tick_start = time.monotonic()
        now_utc = _utc_now()

        ping_ok, latency_ms = probe_ping(PING_TARGET, timeout=PING_TIMEOUT_SECONDS)

        http_ok, http_err, http_last_timeout_utc = _http_probe_state.snapshot()
        http_timeout_trigger = bool(
            http_last_timeout_utc and (now_utc - http_last_timeout_utc).total_seconds() <= 60.0
        )

        # Periodic Fritz check (rate-limited inside get_connection_type_if_outage).
        # In normal mode this polls every 20min; during an event every 60s.
        # If the normal-mode check discovers "mobile", that's a trigger.
        fritz_mobile_trigger = False
        if not state.dsl_event_active:
            ct_check = get_connection_type_if_outage(False)
            if ct_check == "mobile":
                fritz_mobile_trigger = True

        state = update_state(
            state,
            ping_ok=ping_ok,
            now_utc=now_utc,
            http_timeout_trigger=http_timeout_trigger,
            latency_ms=latency_ms,
            fritz_mobile_trigger=fritz_mobile_trigger,
        )
        _outage_dur, mobile_dur = compute_durations(state, now_utc)

        # Always log connection_type (not just during events) so the status-box
        # line can show dsl/mobile/unknown at all times.
        conn_type: ConnectionType = state.last_connection_type
        dsl_event_dur = _dsl_event_duration_seconds(state, now_utc)

        row = [
            now_utc.isoformat(),
            PING_TARGET,
            "1" if ping_ok else "0",
            f"{latency_ms:.3f}" if ping_ok else "",
            str(state.consecutive_failures),
            "1" if state.dsl_event_active else "0",
            state.dsl_event_trigger,
            "" if dsl_event_dur is None else f"{dsl_event_dur:.1f}",
            state.dsl_event_end_reason,
            conn_type,
            "" if mobile_dur is None else f"{mobile_dur:.1f}",
            "" if http_ok is None else ("1" if http_ok else "0"),
            http_err or "",
        ]

        append_and_prune_log(LOG_PATH, row)

        elapsed = time.monotonic() - tick_start
        sleep_s = max(0.0, float(PING_INTERVAL_SECONDS) - elapsed)
        end = time.monotonic() + sleep_s
        while _running and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))

    print("DSL Monitor probe stopped.", "signal=" + str(_last_signal) if _last_signal is not None else "", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
