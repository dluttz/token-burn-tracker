#!/bin/bash
# Token Burn Tracker — DEV server.
# Always runs on port 8800 so it never collides with the real always-on
# install at localhost:8799 (launchd com.dluttz.tokenburn). See CLAUDE.md.
cd "$(dirname "$0")"
PORT="${TRACKER_PORT:-8800}"
echo "Dev server → http://localhost:$PORT   (real install stays on 8799)"
TRACKER_PORT="$PORT" exec python3 tracker.py
