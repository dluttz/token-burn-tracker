#!/bin/bash
# Start the Token Burn Tracker in the BACKGROUND so you can close this window
# and tracking keeps running (until you log out / restart, or run stop.command).
BASE="$(cd "$(dirname "$0")" && pwd)"   # the folder this script lives in — works anywhere
PORT="${TRACKER_PORT:-8799}"
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin"
PY="$(command -v python3 || echo /usr/bin/python3)"

# stop any existing copy and free the port
pkill -f "tracker.py" 2>/dev/null
# free the port only if OUR tracker is holding it (never kill a stranger's service on this port)
for _pid in $(/usr/sbin/lsof -ti tcp:"$PORT" 2>/dev/null); do
  /bin/ps -p "$_pid" -o command= 2>/dev/null | grep -q "tracker.py" && kill "$_pid" 2>/dev/null
done
sleep 1

# launch detached: nohup + & + disown means closing this window won't stop it
nohup "$PY" "$BASE/tracker.py" >"$BASE/tracker.out" 2>&1 &
disown

echo "Token Burn Tracker is now running in the BACKGROUND."
echo "    →  http://localhost:$PORT"
echo ""
echo "You can CLOSE THIS WINDOW and tracking will keep running."
echo "To stop it later, double-click  stop.command  in this folder."
( for i in $(seq 1 40); do
    curl -s "http://localhost:$PORT/" >/dev/null 2>&1 && { open "http://localhost:$PORT/"; break; }
    sleep 0.5
  done ) &
sleep 2
echo ""
echo "Done — this window can be closed now."
