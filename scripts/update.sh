#!/usr/bin/env bash
# Pull the latest code and restart. Run on the VPS:  bash scripts/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m astrid migrate
systemctl restart astrid
echo "updated and restarted"
