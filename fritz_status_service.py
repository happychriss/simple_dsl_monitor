#!/usr/bin/env python3

import os
import time
from datetime import datetime, timezone
from typing import Any, Literal, Optional, cast

from flask import Flask, jsonify

try:
    from fritzconnection import FritzConnection
except ImportError:
    FritzConnection = None  # type: ignore

ConnectionType = Literal["dsl", "mobile", "unknown"]

FRITZ_HOST = os.environ.get("FRITZ_HOST", "fritz.box")
FRITZ_USER = os.environ.get("FRITZ_USER", "")
FRITZ_PASSWORD = os.environ.get("FRITZ_PASSWORD", "")

app = Flask(__name__)

_fc_cache: Any | None = None


def _get_fritzconnection() -> Any:
    global _fc_cache
    if FritzConnection is None:
        raise RuntimeError("fritzconnection not installed")
    if _fc_cache is None:
        fritz_cls = cast(object, FritzConnection)
        _fc_cache = cast(Any, fritz_cls)(
            address=FRITZ_HOST,
            user=FRITZ_USER or None,
            password=FRITZ_PASSWORD or None,
        )
    return _fc_cache


def _invalidate_fritzconnection_cache() -> None:
    global _fc_cache
    _fc_cache = None


def _map_wan_access_type(raw: str) -> ConnectionType:
    raw_upper = raw.upper()
    if "DSL" in raw_upper:
        return "dsl"
    if any(x in raw_upper for x in ["UMTS", "LTE", "MOBILE"]):
        return "mobile"
    return "unknown"


def _iter_service_variants(service: str) -> list[str]:
    svc = service.strip()
    if not svc:
        return []
    if ":" in svc:
        base = svc.split(":", 1)[0]
        return [svc, base]
    return [f"{svc}:1", svc]


def _call_action_with_variants(fc: Any, service: str, action: str) -> Any:
    last_exc: Exception | None = None
    for svc in _iter_service_variants(service):
        try:
            return fc.call_action(svc, action)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("invalid service")


def _query_fritzbox_connection_type(fc: Any) -> tuple[ConnectionType, str]:
    resp = _call_action_with_variants(fc, "WANCommonInterfaceConfig", "GetCommonLinkProperties")
    raw = str(getattr(resp, "get", lambda *_: "")("NewWANAccessType", ""))
    return _map_wan_access_type(raw), raw


def _query_dsl_info(fc: Any) -> dict[str, Any]:
    """Fetch SNR margins, line attenuation, and sync rates via WANDSLInterfaceConfig:GetInfo.

    TR-064 returns noise margin and attenuation in units of 0.1 dB; we divide by 10.
    Sync rates are in kbps.
    """
    result: dict[str, Any] = {
        "snr_down_db": None,
        "snr_up_db": None,
        "ds_attenuation_db": None,
        "us_attenuation_db": None,
        "ds_curr_rate_kbps": None,
        "us_curr_rate_kbps": None,
        "ds_max_rate_kbps": None,
        "us_max_rate_kbps": None,
    }
    candidates: list[tuple[str, str]] = [
        ("WANDSLInterfaceConfig", "GetInfo"),
        ("WANDSLInterfaceConfig", "GetDSLInfo"),
    ]
    for service, action in candidates:
        try:
            resp = _call_action_with_variants(fc, service, action)
        except Exception:
            continue
        if not isinstance(resp, dict):
            continue

        # SNR margin (0.1 dB → dB)
        for key_down, key_up in [
            ("NewDownstreamNoiseMargin", "NewUpstreamNoiseMargin"),
            ("NewSNRMarginDown", "NewSNRMarginUp"),
        ]:
            if key_down in resp:
                try:
                    result["snr_down_db"] = int(resp[key_down]) / 10.0
                    snr_up_raw = resp.get(key_up)
                    result["snr_up_db"] = int(snr_up_raw) / 10.0 if snr_up_raw is not None else None
                except (TypeError, ValueError):
                    pass
                break

        # Line attenuation (0.1 dB → dB)
        if "NewDownstreamAttenuation" in resp:
            try:
                result["ds_attenuation_db"] = int(resp["NewDownstreamAttenuation"]) / 10.0
                us_att = resp.get("NewUpstreamAttenuation")
                result["us_attenuation_db"] = int(us_att) / 10.0 if us_att is not None else None
            except (TypeError, ValueError):
                pass

        # Sync rates (kbps)
        for tr064_key, out_key in [
            ("NewDownstreamCurrRate", "ds_curr_rate_kbps"),
            ("NewUpstreamCurrRate", "us_curr_rate_kbps"),
            ("NewDownstreamMaxRate", "ds_max_rate_kbps"),
            ("NewUpstreamMaxRate", "us_max_rate_kbps"),
        ]:
            if tr064_key in resp:
                try:
                    result[out_key] = int(resp[tr064_key])
                except (TypeError, ValueError):
                    pass

        if result["snr_down_db"] is not None or result["ds_attenuation_db"] is not None:
            return result

    return result


def _query_dsl_statistics(fc: Any) -> dict[str, Optional[int]]:
    """Fetch cumulative DSL error counters via WANDSLInterfaceConfig:GetStatisticsTotal.

    All values are cumulative since last modem reboot/retrain.
    NewLinkRetrain is the key metric: it increments every time DSL resyncs.
    """
    result: dict[str, Optional[int]] = {}
    try:
        resp = _call_action_with_variants(fc, "WANDSLInterfaceConfig", "GetStatisticsTotal")
    except Exception:
        return result
    if not isinstance(resp, dict):
        return result
    for out_key, tr064_key in [
        ("link_retrains", "NewLinkRetrain"),
        ("crc_errors", "NewCRCErrors"),
        ("fec_errors", "NewFECErrors"),
        ("errored_secs", "NewErroredSecs"),
        ("severely_errored_secs", "NewSeverelyErroredSecs"),
        ("atuc_crc_errors", "NewATUCCRCErrors"),
        ("atuc_fec_errors", "NewATUCFECErrors"),
    ]:
        val = resp.get(tr064_key)
        if val is not None:
            try:
                result[out_key] = int(val)
            except (TypeError, ValueError):
                pass
    return result


def _query_ppp_uptime(fc: Any) -> dict[str, Any]:
    """Fetch PPP/IP connection uptime. NewUptime resets to 0 on each reconnect.

    Distinguishes PPP-level drops (ISP/routing) from DSL sync losses (physical).
    """
    candidates: list[tuple[str, str]] = [
        ("WANPPPConnection", "GetInfo"),
        ("WANIPConnection", "GetInfo"),
    ]
    for service, action in candidates:
        try:
            resp = _call_action_with_variants(fc, service, action)
        except Exception:
            continue
        if not isinstance(resp, dict):
            continue
        uptime = resp.get("NewUptime")
        status = resp.get("NewConnectionStatus")
        if uptime is not None or status is not None:
            result: dict[str, Any] = {}
            if uptime is not None:
                try:
                    result["ppp_uptime_seconds"] = int(uptime)
                except (TypeError, ValueError):
                    pass
            if status is not None:
                result["ppp_connection_status"] = str(status)
            last_err = resp.get("NewLastConnectionError")
            if last_err:
                result["ppp_last_error"] = str(last_err)
            return result
    return {}


def _query_dsl_sync_status(fc: Any) -> Optional[dict[str, Any]]:
    candidates: list[tuple[str, str]] = [
        ("WANCommonInterfaceConfig", "GetCommonLinkProperties"),
        ("WANDSLInterfaceConfig", "GetInfo"),
        ("WANDSLInterfaceConfig", "GetDSLInfo"),
    ]
    for service, action in candidates:
        try:
            resp = _call_action_with_variants(fc, service, action)
        except Exception:
            continue
        if not isinstance(resp, dict):
            continue
        payload: dict[str, Any] = {"service": service, "action": action, **resp}
        sync_up: Optional[bool] = None
        for key in ["NewPhysicalLinkStatus", "NewLinkStatus", "NewStatus"]:
            if key in payload:
                val = str(payload.get(key, "")).strip().upper()
                if val in {"UP", "1", "TRUE", "CONNECTED", "ONLINE"}:
                    sync_up = True
                elif val in {"DOWN", "0", "FALSE", "DISCONNECTED", "OFFLINE"}:
                    sync_up = False
        if sync_up is None:
            downstream = payload.get("NewDownstreamCurrRate") or payload.get("NewDownstreamMaxRate")
            upstream = payload.get("NewUpstreamCurrRate") or payload.get("NewUpstreamMaxRate")
            if downstream is not None or upstream is not None:
                try:
                    ds = 0 if downstream is None else int(downstream)
                    us = 0 if upstream is None else int(upstream)
                    sync_up = (ds > 0) or (us > 0)
                except Exception:
                    pass
        if sync_up is not None:
            return {"sync_up": bool(sync_up), "service": service, "action": action, "raw": payload}
    return None


# ---------------------------------------------------------------------------
# Device log
# ---------------------------------------------------------------------------

_LOG_DSL_KEYWORDS = (
    "dsl",
    "internet",
    "synchroni",
    "verbindung",
    "verbunden",
    "getrennt",
    "ppp",
    "ip-adresse",
    "ip address",
    "breitband",
    "broadband",
    "reconnect",
    "lineid",
)

_log_cache: list[dict[str, str]] = []
_log_cache_ts: float = 0.0
_LOG_CACHE_TTL = 300.0  # 5 minutes


def _parse_device_log(raw: str) -> list[dict[str, str]]:
    """Parse GetDeviceLog text blob into structured entries.

    Filters to DSL/internet-relevant lines only.
    Input format per line: "DD.MM.YY HH:MM:SS <message>"
    """
    entries: list[dict[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        date_str, time_str, msg = parts
        if not any(kw in msg.lower() for kw in _LOG_DSL_KEYWORDS):
            continue
        try:
            d_parts = date_str.split(".")
            if len(d_parts) == 3:
                day, month, yr = d_parts
                full_year = 2000 + int(yr) if int(yr) < 100 else int(yr)
                ts_iso = f"{full_year:04d}-{int(month):02d}-{int(day):02d}T{time_str}"
            else:
                ts_iso = f"{date_str} {time_str}"
        except Exception:
            ts_iso = f"{date_str} {time_str}"
        entries.append({"ts": ts_iso, "msg": msg})
    return entries


@app.route("/log")
def fritz_log():
    """Return recent DSL/internet events from the FritzBox event log (cached 5 min)."""
    global _log_cache, _log_cache_ts
    now_mono = time.monotonic()
    if _log_cache and (now_mono - _log_cache_ts) < _LOG_CACHE_TTL:
        return jsonify({"entries": _log_cache, "cached": True})
    try:
        fc = _get_fritzconnection()
        resp = _call_action_with_variants(fc, "DeviceInfo", "GetDeviceLog")
        raw = resp.get("NewDeviceLog", "") if isinstance(resp, dict) else ""
        entries = _parse_device_log(str(raw))
        _log_cache = entries
        _log_cache_ts = now_mono
        return jsonify({"entries": entries, "cached": False})
    except Exception as exc:  # noqa: BLE001
        _invalidate_fritzconnection_cache()
        return jsonify({"entries": [], "error": str(exc)})


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------

@app.route("/status")
def status():
    now = datetime.now(timezone.utc).isoformat()
    conn_type: ConnectionType = "unknown"
    raw_access: Optional[str] = None
    dsl_sync_up: Optional[bool] = None
    dsl_sync_source: Optional[str] = None
    error: Optional[str] = None

    dsl_info: dict[str, Any] = {}
    dsl_stats: dict[str, Any] = {}
    ppp: dict[str, Any] = {}

    try:
        fc = _get_fritzconnection()
        conn_type, raw_access = _query_fritzbox_connection_type(fc)
        dsl = _query_dsl_sync_status(fc)
        if dsl is not None:
            dsl_sync_up = cast(Optional[bool], dsl.get("sync_up"))
            dsl_sync_source = f"{dsl.get('service')}.{dsl.get('action')}"
        dsl_info = _query_dsl_info(fc)
        dsl_stats = _query_dsl_statistics(fc)
        ppp = _query_ppp_uptime(fc)
    except Exception as exc:  # noqa: BLE001
        _invalidate_fritzconnection_cache()
        error = str(exc)

    payload: dict[str, Any] = {
        "connection_type": conn_type,
        "last_change_utc": now,
        "raw": raw_access,
        "dsl_sync_up": dsl_sync_up,
        "dsl_sync_source": dsl_sync_source,
        # DSL info (GetInfo)
        "snr_down_db": dsl_info.get("snr_down_db"),
        "snr_up_db": dsl_info.get("snr_up_db"),
        "ds_attenuation_db": dsl_info.get("ds_attenuation_db"),
        "us_attenuation_db": dsl_info.get("us_attenuation_db"),
        "ds_curr_rate_kbps": dsl_info.get("ds_curr_rate_kbps"),
        "us_curr_rate_kbps": dsl_info.get("us_curr_rate_kbps"),
        # DSL statistics (GetStatisticsTotal) — cumulative since last reboot
        "link_retrains": dsl_stats.get("link_retrains"),
        "crc_errors": dsl_stats.get("crc_errors"),
        "fec_errors": dsl_stats.get("fec_errors"),
        "errored_secs": dsl_stats.get("errored_secs"),
        "severely_errored_secs": dsl_stats.get("severely_errored_secs"),
        # PPP connection
        "ppp_uptime_seconds": ppp.get("ppp_uptime_seconds"),
        "ppp_connection_status": ppp.get("ppp_connection_status"),
    }
    if error:
        payload["error"] = error
    return jsonify(payload)


def main() -> int:
    port = int(os.environ.get("FRITZ_STATUS_PORT", "9077"))
    app.run(host="127.0.0.1", port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
