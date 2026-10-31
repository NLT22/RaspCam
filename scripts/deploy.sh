#!/usr/bin/env bash
# Sync the code to the Pi, install it into a venv, and restart the service.
set -euo pipefail
PI="${PI:-nlt22@192.168.1.90}"
DEST="${DEST:-/home/nlt22/raspcam}"
cd "$(dirname "$0")/.."

rsync -az --delete \
  --exclude .git --exclude .venv --exclude config.yaml --exclude __pycache__ \
  --exclude .pytest_cache --exclude '*.egg-info' \
  ./ "$PI:$DEST/"

ssh "$PI" bash -s -- "$DEST" <<'REMOTE'
set -euo pipefail
cd "$1"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -e .
if [ ! -f config.yaml ]; then
  echo "config.yaml missing in $1: copy config.example.yaml and run .venv/bin/python -m raspcam.auth" >&2
  exit 1
fi
sudo install -m 644 deploy/raspcam.service /etc/systemd/system/raspcam.service
sudo systemctl daemon-reload
sudo systemctl enable --quiet raspcam
sudo systemctl restart raspcam
sleep 3
systemctl is-active raspcam
REMOTE
