#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
mkdir -p logs
exec "$ROOT/venv/bin/python" "$ROOT/run_scheduled.py" >> "$ROOT/logs/cron.log" 2>&1
