#!/bin/bash
# Stop the Token Burn Tracker (whether started in foreground or background).
PORT="${TRACKER_PORT:-8799}"
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin"
pkill -f "tracker.py" 2>/dev/null
# free the port only if OUR tracker is holding it (never kill a stranger's service on this port)
for _pid in $(/usr/sbin/lsof -ti tcp:"$PORT" 2>/dev/null); do
  /bin/ps -p "$_pid" -o command= 2>/dev/null | grep -q "tracker.py" && kill "$_pid" 2>/dev/null
done
echo "Token Burn Tracker stopped."
echo "Your desktop widgets will show no data until you start it again"
echo "(start.command for a visible window, or run-in-background.command)."
echo "You can close this window."
