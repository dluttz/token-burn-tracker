#!/bin/bash
# Token Burn Tracker — reads your Claude Code / Cowork / Codex logs and opens a live
# dashboard in your browser. Close this window to quit.
BASE="$(cd "$(dirname "$0")" && pwd)"   # the folder this script lives in — works anywhere
PORT="${TRACKER_PORT:-8799}"
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin"
PY="$(command -v python3 || echo /usr/bin/python3)"

# Stop any tracker already running — match both absolute and relative invocations,
# and free the port directly in case it was started some other way.
pkill -f "tracker.py" 2>/dev/null
# free the port only if OUR tracker is holding it (never kill a stranger's service on this port)
for _pid in $(/usr/sbin/lsof -ti tcp:"$PORT" 2>/dev/null); do
  /bin/ps -p "$_pid" -o command= 2>/dev/null | grep -q "tracker.py" && kill "$_pid" 2>/dev/null
done
sleep 1
echo "Token Burn Tracker  ->  http://localhost:$PORT     (close this window to quit)"
echo "First scan reads your logs and can take a little while; the page shows progress."
echo ""
( for i in $(seq 1 40); do
    curl -s "http://localhost:$PORT/" >/dev/null 2>&1 && { open "http://localhost:$PORT/"; break; }
    sleep 0.5
  done ) &
exec "$PY" "$BASE/tracker.py"
