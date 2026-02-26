#!/usr/bin/env python3
"""Start DSL monitor components in one go.

Starts:
- Fritz status bridge (optional, local-only HTTP endpoint)
- Ping probe
- Web UI

All configuration is via environment variables. This script only orchestrates.

Notes:
- This is meant for local/dev usage and for a simple systemd service.
- Processes are terminated as a group on SIGTERM/SIGINT.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import List


def _load_env_from_project(here: str) -> None:
    """Load .env from the project folder if python-dotenv is available."""

    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(here, ".env"))
    except Exception:
        # python-dotenv is optional at runtime; env vars may be provided by systemd.
        pass


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _start(cmd: List[str], env: dict[str, str] | None = None, cwd: str | None = None) -> subprocess.Popen[str]:
    # Use inherited stdio by default (None), which works reliably across IDE/systemd.
    return subprocess.Popen(
        cmd,
        text=True,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))

    # Ensure .env is read from the repo folder (important when launched via systemd).
    _load_env_from_project(here)

    procs: list[subprocess.Popen[str]] = []

    # Optional Fritz status bridge
    if _env_flag("DSL_MONITOR_START_FRITZ_BRIDGE", "1"):
        print(
            "Starting fritz_status_service.py – endpoint:",
            os.environ.get("DSL_CONN_STATUS_URL", "http://127.0.0.1:9077/status"),
            flush=True,
        )
        procs.append(_start([sys.executable, os.path.join(here, "fritz_status_service.py")], cwd=here))

    # Probe (long running)
    print("Starting probe.py – log:", os.environ.get("DSL_MONITOR_LOG", "(default)"), flush=True)
    procs.append(_start([sys.executable, os.path.join(here, "probe.py")], cwd=here))

    # Web UI: support the .env style variables DSL_MONITOR_WEB_HOST/PORT by mapping them
    # to the variables web.py actually reads.
    web_env = dict(os.environ)
    if "DSL_MONITOR_WEB_PORT" in web_env and "PORT" not in web_env:
        web_env["PORT"] = str(web_env["DSL_MONITOR_WEB_PORT"])
    if "DSL_MONITOR_WEB_HOST" in web_env and "HOST" not in web_env:
        web_env["HOST"] = str(web_env["DSL_MONITOR_WEB_HOST"])

    print("Starting web.py – UI on:", f"http://{web_env.get('HOST','0.0.0.0')}:{web_env.get('PORT','8080')}", flush=True)
    procs.append(_start([sys.executable, os.path.join(here, "web.py")], env=web_env, cwd=here))

    stopping = False

    def _stop(_signum, _frame):  # noqa: ANN001
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print("run.py: stopping children…", flush=True)
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Wait for any child to exit; if one dies, stop all.
    try:
        while True:
            for p in procs:
                rc = p.poll()
                if rc is not None:
                    print(f"run.py: child exited pid={p.pid} rc={rc} cmd={getattr(p, 'args', '')}", flush=True)
                    _stop(signal.SIGTERM, None)
                    # give others a moment
                    time.sleep(2.0)
                    for p2 in procs:
                        if p2.poll() is None:
                            try:
                                p2.kill()
                            except Exception:
                                pass
                    return int(rc)
            time.sleep(0.5)
    finally:
        _stop(signal.SIGTERM, None)


if __name__ == "__main__":
    raise SystemExit(main())

