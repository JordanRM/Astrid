#!/usr/bin/env bash
# One-time setup on the VPS. Safe to re-run. Run from anywhere:  bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
OLD_ENV="${OLD_ENV:-/Companion/my-companion/.env}"

if pgrep -f "elizaos start|bun run start" >/dev/null 2>&1; then
  echo "The old elizaOS bot is still running. Stop it first (Ctrl+C in its PuTTY window), then run this again."
  echo "(Two programs can't read the same Telegram bot at once.)"
  exit 1
fi

echo "== Python environment"
if [ "${ASTRID_TEST:-0}" = "1" ]; then
  # tests only: reuse already-installed packages, no downloads
  python3 -m venv --system-site-packages .venv
else
  if ! python3 -m venv .venv 2>/dev/null; then
    echo "installing python3-venv..."
    apt-get update -qq && apt-get install -y -qq python3-venv python3-pip
    python3 -m venv .venv
  fi
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

echo "== Settings"
if [ -f .env ]; then
  echo ".env already exists, keeping it"
else
  python3 scripts/make_env.py "$OLD_ENV" .env
fi

echo "== Checking database, Telegram and the reply model"
if ! .venv/bin/python -m astrid check; then
  echo
  echo "A check failed (see above). Fix it, then run this script again."
  exit 1
fi

if [ "${SKIP_SERVICE:-0}" = "1" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Skipping the background service. Start it by hand with: .venv/bin/python -m astrid"
  exit 0
fi

echo "== Background service (starts on boot, restarts if it crashes)"
cat > /etc/systemd/system/astrid.service <<UNIT
[Unit]
Description=Astrid companion
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python -m astrid
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable astrid >/dev/null 2>&1
systemctl restart astrid
sleep 2
systemctl --no-pager --lines=5 status astrid || true
echo
echo "Done. Watch her logs with:  journalctl -u astrid -f"
