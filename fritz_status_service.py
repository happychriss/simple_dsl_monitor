#!/usr/bin/env python3

import os
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

# Global cache for the FritzConnection instance (per process).
_fc_cache: Any | None = None


def _get_fritzconnection() -> Any:
    """Return a cached FritzConnection instance.

    FritzConnection creation can be relatively slow (TR-064 service discovery, etc.).
    Caching makes /status faster and also reduces load on the FritzBox.

    We keep this intentionally simple: single instance per process.
    """

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
    """Return likely TR-064 service name variants.

    Some boxes require an explicit version suffix like ':1'. Others accept the
    plain service name.
    """

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
    mapped = _map_wan_access_type(raw)
    return mapped, raw


def _query_dsl_sync_status(fc: Any) -> Optional[dict[str, Any]]:
    """Try to query DSL sync status via TR-064.

    Fritz!Box firmwares differ a lot. We try a small set of actions and return
    structured info if any works.

    Returns None if not available.
    """

    # Most common service/action names seen in the wild.
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

        # Heuristic mapping to a stable payload.
        payload: dict[str, Any] = {"service": service, "action": action, **resp}

        # Try to infer "sync up" from common fields.
        sync_up: Optional[bool] = None
        for key in [
            "NewPhysicalLinkStatus",  # often "Up"/"Down"
            "NewLinkStatus",  # sometimes present
            "NewStatus",  # sometimes present
        ]:
            if key in payload:
                val = str(payload.get(key, "")).strip().upper()
                if val in {"UP", "1", "TRUE", "CONNECTED", "ONLINE"}:
                    sync_up = True
                elif val in {"DOWN", "0", "FALSE", "DISCONNECTED", "OFFLINE"}:
                    sync_up = False

        # Some firmwares expose rates.
        if sync_up is None:
            downstream = payload.get("NewDownstreamCurrRate") or payload.get("NewDownstreamMaxRate")
            upstream = payload.get("NewUpstreamCurrRate") or payload.get("NewUpstreamMaxRate")

            if (downstream is not None) or (upstream is not None):
                try:
                    ds = 0 if downstream is None else int(downstream)
                    us = 0 if upstream is None else int(upstream)
                    sync_up = (ds > 0) or (us > 0)
                except Exception:
                    pass

        if sync_up is not None:
            return {
                "sync_up": bool(sync_up),
                "service": service,
                "action": action,
                "raw": payload,
            }

    return None


@app.route("/status")
def status():
    """Return current FritzBox connection info.

    This endpoint queries the FritzBox on-demand (no background polling).

    Response is intentionally stable:
    - connection_type: dsl/mobile/unknown
    - dsl_sync_up: bool|None
    - raw: optional raw WAN access type string
    """

    now = datetime.now(timezone.utc).isoformat()

    conn_type: ConnectionType = "unknown"
    raw_access: Optional[str] = None
    dsl_sync_up: Optional[bool] = None
    dsl_sync_source: Optional[str] = None
    error: Optional[str] = None

    try:
        fc = _get_fritzconnection()
        conn_type, raw_access = _query_fritzbox_connection_type(fc)
        dsl = _query_dsl_sync_status(fc)
        if dsl is not None:
            dsl_sync_up = cast(Optional[bool], dsl.get("sync_up"))
            dsl_sync_source = f"{dsl.get('service')}.{dsl.get('action')}"
    except Exception as exc:  # noqa: BLE001
        # Connection caching can get stale if the box drops TR-064 sessions.
        # Invalidate once so the next request can re-create a working instance.
        _invalidate_fritzconnection_cache()
        error = str(exc)

    payload: dict[str, Any] = {
        "connection_type": conn_type,
        "last_change_utc": now,
        "raw": raw_access,
        "dsl_sync_up": dsl_sync_up,
        "dsl_sync_source": dsl_sync_source,
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
