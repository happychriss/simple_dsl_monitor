#!/usr/bin/env python3
"""DSL Monitor  ping-based probe daemon.

New logic:
- Ping a target every 10 seconds.
- 3 consecutive failed pings constitute a DSL outage event.
- Once an outage starts, query FritzBox connection type (dsl/mobile/unknown)
  and keep tracking mobile duration.

Extended diagnostics (classification):
- Ping FritzBox locally
- Ping a public IP (default 1.1.1.1)
- Periodic DNS resolve check
- Optional FritzBox DSL sync status (via fritz_status_service)
- Rolling latency/loss metrics to classify issues into:
  * DSL_SYNC_DROP
  * WAN_REACHABILITY_LOSS_WITH_SYNC_OK
  * HIGH_LATENCY_UNDER_UPLOAD

The probe appends raw samples to a CSV file. The Web UI aggregates these
samples into 5-minute buckets.

Run as a long-lived systemd service (Type=simple).
"""

import csv
import json
import os
import platform
import signal
import socket
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, Literal, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PING_TARGET = os.environ.get("DSL_MONITOR_PING_TARGET", "8.8.8.8")

# Additional signal sources (keep existing target ping)
FRITZ_PING_TARGET = os.environ.get("DSL_MONITOR_FRITZ_PING_TARGET", "fritz.box")
PUBLIC_PING_TARGET = os.environ.get("DSL_MONITOR_PUBLIC_PING_TARGET", "1.1.1.1")
DNS_TEST_HOSTNAME = os.environ.get("DSL_MONITOR_DNS_TEST_HOSTNAME", "example.com")
DNS_CHECK_EVERY_N_TICKS = int(os.environ.get("DSL_MONITOR_DNS_CHECK_EVERY_N_TICKS", "6"))  # 6*10s=60s default

LOG_PATH = os.environ.get(
    "DSL_MONITOR_LOG",
    os.path.join(os.path.dirname(__file__), "dsl_log.csv"),
)
RETENTION_DAYS = int(os.environ.get("DSL_MONITOR_RETENTION_DAYS", "7"))

PING_INTERVAL_SECONDS = int(os.environ.get("DSL_MONITOR_PING_INTERVAL_SECONDS", "10"))
CONSECUTIVE_FAILURES_THRESHOLD = int(os.environ.get("DSL_MONITOR_FAILURE_THRESHOLD", "3"))
PING_TIMEOUT_SECONDS = float(os.environ.get("DSL_MONITOR_PING_TIMEOUT_SECONDS", "5"))

# Local FritzBox connection-type helper (TR-064 bridge)
CONN_STATUS_URL = os.environ.get("DSL_CONN_STATUS_URL", "http://127.0.0.1:9077/status")
ConnectionType = Literal["dsl", "mobile", "unknown"]

MOBILE_YELLOW_THRESHOLD_SECONDS = int(os.environ.get("DSL_MONITOR_MOBILE_YELLOW_THRESHOLD_SECONDS", "300"))

# Classification thresholds
HIGH_LATENCY_P95_MS = float(os.environ.get("DSL_MONITOR_HIGH_LATENCY_P95_MS", "150"))
HIGH_LATENCY_MIN_SAMPLES = int(os.environ.get("DSL_MONITOR_HIGH_LATENCY_MIN_SAMPLES", "12"))  # ~2min at 10s
LOSS_WINDOW_SAMPLES = int(os.environ.get("DSL_MONITOR_LOSS_WINDOW_SAMPLES", "30"))  # ~5min at 10s
LOSS_WAN_THRESHOLD = float(os.environ.get("DSL_MONITOR_LOSS_WAN_THRESHOLD", "0.6"))

Reason = Literal[
    "",
    "DSL_SYNC_DROP",
    "WAN_REACHABILITY_LOSS_WITH_SYNC_OK",
    "HIGH_LATENCY_UNDER_UPLOAD",
]

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_running = True


def _handle_signal(signum, frame):  # noqa: ANN001
    global _running
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------


def ensure_log_header(path: str) -> None:
    """Ensure CSV exists with the expected header.

    The Web UI is tolerant (missing columns -> defaults), but we write a stable
    schema going forward.
    """

    header = [
        "timestamp",
        "target",
        "ping_ok",
        "latency_ms",
        "method",
        "consecutive_failures",
        "in_outage",
        "outage_duration_seconds",
        "connection_type",
        "mobile_duration_seconds",
        # --- new diagnostics ---
        "fritz_ping_ok",
        "fritz_latency_ms",
        "public_ping_ok",
        "public_latency_ms",
        "dns_ok",
        "dns_latency_ms",
        "dsl_sync_up",
        "reason",
        "reason_details_json",
    ]

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(header)


def append_and_prune_log(path: str, row: list) -> None:
    ensure_log_header(path)
    with open(path, "a", newline="") as f:
        csv.writer(f).writerow(row)

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

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
                # Keep unparseable rows
                rows.append(r)
                continue
            if ts >= cutoff:
                rows.append(r)

    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


# ---------------------------------------------------------------------------
# Ping probe
# ---------------------------------------------------------------------------


def probe_ping(target: str, timeout: float = PING_TIMEOUT_SECONDS) -> Tuple[bool, float]:
    """Ping the target host once. Returns (ok, latency_ms)."""

    system = platform.system().lower()

    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), target]
    else:
        # Linux: -W is timeout in seconds for a reply
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


def probe_dns(hostname: str = DNS_TEST_HOSTNAME, timeout_s: float = 3.0) -> tuple[bool, float, Optional[str]]:
    """Resolve a hostname and measure rough latency.

    Returns: (ok, latency_ms, error_str)
    """

    start = time.monotonic()
    try:
        # Best effort per-call timeout (doesn't affect all resolvers, but helps).
        old_to = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout_s)
        try:
            socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
        finally:
            socket.setdefaulttimeout(old_to)
        return True, (time.monotonic() - start) * 1000.0, None
    except Exception as exc:  # noqa: BLE001
        return False, (time.monotonic() - start) * 1000.0, str(exc)


# ---------------------------------------------------------------------------
# Fritz / connection-type + DSL sync helper
# ---------------------------------------------------------------------------


def get_fritz_status() -> dict:
    """Fetch Fritz status from the local status service.

    Expected fields (best effort):
    - connection_type: dsl/mobile/unknown
    - dsl_sync_up: bool|None
    - error: optional
    """

    try:
        resp = requests.get(CONN_STATUS_URL, timeout=6.0)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def get_connection_type() -> ConnectionType:
    try:
        data = get_fritz_status()
        ct = str(data.get("connection_type", "unknown")).lower()
        if ct in {"dsl", "mobile", "unknown"}:
            return ct  # type: ignore[return-value]
    except Exception:
        pass
    return "unknown"


def get_dsl_sync_up() -> Optional[bool]:
    data = get_fritz_status()
    val = data.get("dsl_sync_up")
    if val is True or val is False:
        return bool(val)
    return None


# ---------------------------------------------------------------------------
# State machine (existing outage logic stays as-is)
# ---------------------------------------------------------------------------


@dataclass
class OutageState:
    consecutive_failures: int = 0
    in_outage: bool = False
    outage_start_utc: Optional[datetime] = None
    mobile_start_utc: Optional[datetime] = None
    last_connection_type: ConnectionType = "unknown"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def update_state(state: OutageState, ping_ok: bool, now_utc: datetime) -> OutageState:
    """Update OutageState based on a single ping result."""

    if ping_ok:
        state.consecutive_failures = 0
        if state.in_outage:
            # outage ends on first successful ping
            state.in_outage = False
            state.outage_start_utc = None
            state.mobile_start_utc = None
        return state

    # ping failed
    state.consecutive_failures += 1

    if not state.in_outage and state.consecutive_failures >= CONSECUTIVE_FAILURES_THRESHOLD:
        # outage starts now
        state.in_outage = True
        state.outage_start_utc = now_utc
        # Start mobile tracking only if Fritz reports mobile at outage start
        state.last_connection_type = get_connection_type()
        if state.last_connection_type == "mobile":
            state.mobile_start_utc = now_utc

    if state.in_outage:
        # Keep checking Fritz status while outage is ongoing
        state.last_connection_type = get_connection_type()
        if state.last_connection_type == "mobile":
            if state.mobile_start_utc is None:
                state.mobile_start_utc = now_utc
        else:
            state.mobile_start_utc = None

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
# Reason classification (pure-ish, driven by rolling metrics)
# ---------------------------------------------------------------------------


def _p95(values: list[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    # nearest-rank method
    k = max(0, min(len(s) - 1, int((0.95 * (len(s) - 1)))))
    return float(s[k])


@dataclass
class RollingMetrics:
    public_ok_window: Deque[bool]
    public_latency_window: Deque[float]
    target_latency_window: Deque[float]

    def __init__(self) -> None:
        self.public_ok_window = deque(maxlen=LOSS_WINDOW_SAMPLES)
        self.public_latency_window = deque(maxlen=LOSS_WINDOW_SAMPLES)
        self.target_latency_window = deque(maxlen=max(LOSS_WINDOW_SAMPLES, HIGH_LATENCY_MIN_SAMPLES))


def classify_reason(
    *,
    in_outage: bool,
    dsl_sync_up: Optional[bool],
    fritz_ping_ok: Optional[bool],
    public_ping_ok: Optional[bool],
    dns_ok: Optional[bool],
    metrics: RollingMetrics,
) -> tuple[Reason, Dict[str, object]]:
    """Classify connection problems.

    Contract:
    - Keep existing outage detection (based on PING_TARGET) untouched.
    - Provide a best-effort reason when behaviour matches.
    - Return details dict for logging.
    """

    details: Dict[str, object] = {
        "dsl_sync_up": dsl_sync_up,
        "signals": {
            "fritz_ping_ok": fritz_ping_ok,
            "public_ping_ok": public_ping_ok,
            "dns_ok": dns_ok,
        },
    }

    # 1) Real DSL resync/drop if Fritz says sync is down.
    if in_outage and dsl_sync_up is False:
        return "DSL_SYNC_DROP", details

    # 2) WAN/PPPoE/route issue: DSL sync OK, local router reachable, but public reachability is broken.
    if in_outage and dsl_sync_up is True:
        if fritz_ping_ok is True and ((public_ping_ok is False) or (dns_ok is False)):
            return "WAN_REACHABILITY_LOSS_WITH_SYNC_OK", details

    # 3) High latency under upload / bufferbloat: reachability OK but latency p95 is high.
    target_p95 = _p95(list(metrics.target_latency_window))
    public_p95 = _p95(list(metrics.public_latency_window))
    details["latency"] = {
        "target_p95_ms": target_p95,
        "public_p95_ms": public_p95,
        "target_samples": len(metrics.target_latency_window),
        "public_samples": len(metrics.public_latency_window),
    }

    if (
        target_p95 is not None
        and len(metrics.target_latency_window) >= HIGH_LATENCY_MIN_SAMPLES
        and target_p95 >= HIGH_LATENCY_P95_MS
    ):
        return "HIGH_LATENCY_UNDER_UPLOAD", details

    # If not in outage, treat public latency as a bufferbloat indicator too.
    if (
        not in_outage
        and public_p95 is not None
        and len(metrics.public_latency_window) >= HIGH_LATENCY_MIN_SAMPLES
        and public_p95 >= HIGH_LATENCY_P95_MS
    ):
        return "HIGH_LATENCY_UNDER_UPLOAD", details

    return "", details


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> int:
    print(
        f"DSL Monitor probe starting. target={PING_TARGET} interval={PING_INTERVAL_SECONDS}s "
        f"threshold={CONSECUTIVE_FAILURES_THRESHOLD} timeout={PING_TIMEOUT_SECONDS}s",
        flush=True,
    )

    state = OutageState()
    metrics = RollingMetrics()
    tick = 0

    while _running:
        tick_start = time.monotonic()
        now_utc = _utc_now()

        # Existing primary check (keep behaviour unchanged)
        ping_ok, latency_ms = probe_ping(PING_TARGET, timeout=PING_TIMEOUT_SECONDS)
        state = update_state(state, ping_ok=ping_ok, now_utc=now_utc)
        outage_dur, mobile_dur = compute_durations(state, now_utc=now_utc)

        conn_type: ConnectionType = state.last_connection_type if state.in_outage else "unknown"

        # New signals
        fritz_ok, fritz_lat = probe_ping(FRITZ_PING_TARGET, timeout=min(2.0, PING_TIMEOUT_SECONDS))
        public_ok, public_lat = probe_ping(PUBLIC_PING_TARGET, timeout=PING_TIMEOUT_SECONDS)

        dsl_sync_up = get_dsl_sync_up()  # optional (None if not available)

        dns_ok: Optional[bool] = None
        dns_lat_ms: Optional[float] = None
        dns_err: Optional[str] = None
        if DNS_CHECK_EVERY_N_TICKS > 0 and (tick % DNS_CHECK_EVERY_N_TICKS == 0):
            dns_ok_b, dns_lat_b, dns_err_b = probe_dns(DNS_TEST_HOSTNAME)
            dns_ok = dns_ok_b
            dns_lat_ms = dns_lat_b
            dns_err = dns_err_b

        # Update rolling metrics
        if ping_ok:
            metrics.target_latency_window.append(float(latency_ms))
        if public_ok:
            metrics.public_latency_window.append(float(public_lat))
        metrics.public_ok_window.append(bool(public_ok))

        # Loss ratio over window for public
        if len(metrics.public_ok_window) > 0:
            public_loss_ratio = 1.0 - (sum(1 for x in metrics.public_ok_window if x) / len(metrics.public_ok_window))
        else:
            public_loss_ratio = 0.0

        reason, reason_details = classify_reason(
            in_outage=state.in_outage,
            dsl_sync_up=dsl_sync_up,
            fritz_ping_ok=fritz_ok,
            public_ping_ok=public_ok,
            dns_ok=dns_ok,
            metrics=metrics,
        )

        # Add some extra small-details for post-mortem.
        reason_details["targets"] = {
            "primary": PING_TARGET,
            "fritz": FRITZ_PING_TARGET,
            "public": PUBLIC_PING_TARGET,
            "dns": DNS_TEST_HOSTNAME,
        }
        reason_details["public_loss_ratio_window"] = round(public_loss_ratio, 3)
        if dns_err:
            reason_details["dns_error"] = dns_err

        # If DSL sync is OK but public loss is massive and we are in outage, force WAN reason.
        if state.in_outage and dsl_sync_up is True and public_loss_ratio >= LOSS_WAN_THRESHOLD and fritz_ok:
            reason = "WAN_REACHABILITY_LOSS_WITH_SYNC_OK"

        row = [
            now_utc.isoformat(),
            PING_TARGET,
            "1" if ping_ok else "0",
            f"{latency_ms:.3f}" if ping_ok else "",
            "ping",
            str(state.consecutive_failures),
            "1" if state.in_outage else "0",
            "" if outage_dur is None else f"{outage_dur:.1f}",
            conn_type,
            "" if mobile_dur is None else f"{mobile_dur:.1f}",
            # --- new diagnostics ---
            "1" if fritz_ok else "0",
            f"{fritz_lat:.3f}" if fritz_ok else "",
            "1" if public_ok else "0",
            f"{public_lat:.3f}" if public_ok else "",
            "" if dns_ok is None else ("1" if dns_ok else "0"),
            "" if dns_lat_ms is None else f"{dns_lat_ms:.3f}",
            "" if dsl_sync_up is None else ("1" if dsl_sync_up else "0"),
            reason,
            json.dumps(reason_details, sort_keys=True, ensure_ascii=False),
        ]

        append_and_prune_log(LOG_PATH, row)

        tick += 1

        # Sleep to maintain roughly fixed interval.
        elapsed = time.monotonic() - tick_start
        sleep_s = max(0.0, float(PING_INTERVAL_SECONDS) - elapsed)
        # Use small sleeps so SIGTERM is responsive.
        end = time.monotonic() + sleep_s
        while _running and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))

    print("DSL Monitor probe stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
