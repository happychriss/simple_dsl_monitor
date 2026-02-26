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


def _map_wan_access_type(raw: str) -> ConnectionType:
    raw_upper = raw.upper()
    if "DSL" in raw_upper:
        return "dsl"
    if any(x in raw_upper for x in ["UMTS", "LTE", "MOBILE"]):
        return "mobile"
    return "unknown"


def _query_fritzbox_connection_type() -> tuple[ConnectionType, str]:
    if FritzConnection is None:
        raise RuntimeError("fritzconnection not installed")

    fritz_cls = cast(object, FritzConnection)
    fc = cast(Any, fritz_cls)(address=FRITZ_HOST, user=FRITZ_USER or None, password=FRITZ_PASSWORD or None)
    resp = fc.call_action("WANCommonInterfaceConfig", "GetCommonLinkProperties")
    raw = str(resp.get("NewWANAccessType", ""))
    mapped = _map_wan_access_type(raw)
    return mapped, raw


def _query_dsl_sync_status() -> Optional[dict[str, Any]]:
    """Try to query DSL sync status via TR-064.

    Fritz!Box firmwares differ a lot. We try a small set of actions and return
    structured info if any works.

    Returns None if not available.
    """

    if FritzConnection is None:
        return None

    fritz_cls = cast(object, FritzConnection)
    fc = cast(Any, fritz_cls)(address=FRITZ_HOST, user=FRITZ_USER or None, password=FRITZ_PASSWORD or None)

    # Most common service/action names seen in the wild.
    candidates: list[tuple[str, str]] = [
        ("WANCommonInterfaceConfig", "GetCommonLinkProperties"),
        ("WANDSLInterfaceConfig", "GetInfo"),
        ("WANDSLInterfaceConfig", "GetDSLInfo"),
    ]

    for service, action in candidates:
        try:
            resp = fc.call_action(service, action)
        except Exception:
            continue

        # Heuristic mapping to a stable payload.
        payload: dict[str, Any] = {"service": service, "action": action}

        if isinstance(resp, dict):
            payload.update(resp)

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
        downstream = payload.get("NewDownstreamCurrRate") or payload.get("NewDownstreamMaxRate")
        upstream = payload.get("NewUpstreamCurrRate") or payload.get("NewUpstreamMaxRate")

        if sync_up is None and (downstream is not None or upstream is not None):
            try:
                sync_up = (int(downstream or 0) > 0) or (int(upstream or 0) > 0)
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
        conn_type, raw_access = _query_fritzbox_connection_type()
        dsl = _query_dsl_sync_status()
        if dsl is not None:
            dsl_sync_up = cast(Optional[bool], dsl.get("sync_up"))
            dsl_sync_source = f"{dsl.get('service')}.{dsl.get('action')}"
    except Exception as exc:  # noqa: BLE001
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
