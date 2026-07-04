#!/bin/bash
# Token Burn Tracker — uninstaller.
# Stops the server, removes the Übersicht widget, and moves THIS tool's local data
# (cache + token + custom sources) to the Trash. It does NOT touch your AI logs,
# and everything it removes is reversible from the Trash.
BASE="$(cd "$(dirname "$0")" && pwd)"   # the folder this script lives in — works anywhere
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin"
PORT="${TRACKER_PORT:-8799}"

echo "This will:"
echo "  - stop the Token Burn server"
echo "  - remove the Übersicht 'Token Burn' widget (if installed)"
echo "  - move this folder's cache + token to the Trash (reversible)"
echo
echo "It will NOT delete your AI logs or this app's source files."
echo
printf "Continue? [y/N] "
read -r ans
case "$ans" in
  y|Y|yes|YES) ;;
  *) echo "Cancelled — nothing was changed."; exit 0;;
esac

# Stop only OUR server (never a stranger's service on this port)
pkill -f "tracker.py" 2>/dev/null
for _pid in $(/usr/sbin/lsof -ti tcp:"$PORT" 2>/dev/null); do
  /bin/ps -p "$_pid" -o command= 2>/dev/null | grep -q "tracker.py" && kill "$_pid" 2>/dev/null
done
echo "- Server stopped."

# Remove the login item (LaunchAgent) so it no longer auto-starts on login
PLIST="$HOME/Library/LaunchAgents/com.dluttz.tokenburn.plist"
if [ -f "$PLIST" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST" && echo "- Login item removed (won't auto-start anymore)."
fi

# Remove the Übersicht widget (move to Trash)
WID="$HOME/Library/Application Support/Übersicht/widgets/token-burn.widget"
if [ -d "$WID" ]; then
  mv "$WID" "$HOME/.Trash/token-burn.widget-$(date +%s)" 2>/dev/null && echo "- Widget moved to Trash."
fi

# Move runtime data to Trash (reversible). Keeps source files intact.
for f in .cache.json .fixtoken custom_sources.json tracker.out; do
  if [ -e "$BASE/$f" ]; then
    mv "$BASE/$f" "$HOME/.Trash/${f#.}-$(date +%s)" 2>/dev/null && echo "- $f -> Trash"
  fi
done

echo
echo "Done. The dashboard is stopped and cleaned up."
echo "To use it again, double-click start.command."
echo "To delete it entirely, drag this whole folder to the Trash."
echo "You can close this window."
