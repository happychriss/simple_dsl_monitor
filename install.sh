#!/usr/bin/env bash
set -euo pipefail

# E2E installer for dsl_monitor
# - creates/updates venv
# - installs Python deps
# - installs/updates systemd unit
# - reloads systemd and starts service
#
# Assumptions:
# - repo already checked out (git pull done)
# - you want a user-local install (no docker)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="dsl-monitor.service"

echo "==> dsl_monitor install"
echo "Repo: $HERE"

if [[ ! -f "$HERE/run.py" ]]; then
  echo "ERROR: run.py not found in $HERE" >&2
  exit 1
fi

# --- venv ---
if [[ ! -d "$HERE/.venv" ]]; then
  echo "==> Creating venv (.venv)"
  python3 -m venv "$HERE/.venv"
fi

# shellcheck disable=SC1091
source "$HERE/.venv/bin/activate"

echo "==> Upgrading pip"
python -m pip install --upgrade pip >/dev/null

echo "==> Installing requirements"
python -m pip install -r "$HERE/requirements.txt"

# --- .env sanity ---
if [[ ! -f "$HERE/.env" ]]; then
  cat >&2 <<'EOF'
ERROR: .env not found.
Create / adjust it (do NOT commit it). Example keys:
  FRITZ_HOST=fritz.box
  FRITZ_USER=...
  FRITZ_PASSWORD=...
  FRITZ_STATUS_PORT=9077
  DSL_CONN_STATUS_URL=http://127.0.0.1:9077/status
  DSL_MONITOR_WEB_HOST=127.0.0.1
  DSL_MONITOR_WEB_PORT=9076
  DSL_MONITOR_START_FRITZ_BRIDGE=1
EOF
  exit 1
fi

# --- systemd install ---
SYSTEMD_DIR="/etc/systemd/system"
UNIT_SRC="$HERE/$UNIT_NAME"
UNIT_DST="$SYSTEMD_DIR/$UNIT_NAME"

if [[ ! -f "$UNIT_SRC" ]]; then
  echo "ERROR: $UNIT_SRC not found" >&2
  exit 1
fi

echo "==> Installing systemd unit to $UNIT_DST"
# We rewrite WorkingDirectory/ExecStart to match current checkout path.
# This avoids hardcoding in the repo for other machines.
TMP_UNIT="$(mktemp)"
trap 'rm -f "$TMP_UNIT"' EXIT

python - <<PY >"$TMP_UNIT"
import pathlib
import re

here = pathlib.Path(r"$HERE")
text = pathlib.Path(r"$UNIT_SRC").read_text(encoding="utf-8")

# Replace WorkingDirectory and ExecStart lines with this checkout.
text = re.sub(r"^WorkingDirectory=.*$", f"WorkingDirectory={here}", text, flags=re.M)
text = re.sub(
    r"^ExecStart=.*$",
    f"ExecStart={here}/.venv/bin/python3 {here}/run.py",
    text,
    flags=re.M,
)

print(text, end="")
PY

sudo install -m 0644 "$TMP_UNIT" "$UNIT_DST"

echo "==> Reloading systemd"
sudo systemctl daemon-reload

echo "==> Enabling + restarting $UNIT_NAME"
sudo systemctl enable "$UNIT_NAME" >/dev/null
sudo systemctl restart "$UNIT_NAME"

echo
sudo systemctl --no-pager --full status "$UNIT_NAME" || true

echo
cat <<EOF
Done.
UI should be on: http://$(grep -E '^DSL_MONITOR_WEB_HOST=' "$HERE/.env" | cut -d= -f2 | tr -d '"'):$((grep -E '^DSL_MONITOR_WEB_PORT=' "$HERE/.env" | cut -d= -f2 | tr -d '"') 2>/dev/null || echo 9076)

Useful commands:
  sudo journalctl -u $UNIT_NAME -f
  curl -sS http://127.0.0.1:9077/status
  curl -sS http://127.0.0.1:9076/api/fritz_status
EOF

