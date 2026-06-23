#!/bin/bash
# Restart the tracker (latest code), wait for the first scan, install the Token Burn
# Übersicht widget, and refresh Übersicht.
BASE="$(cd "$(dirname "$0")" && pwd)"   # the folder this script lives in — works anywhere
LOG="$BASE/widget-install.log"
exec > >(tee "$LOG") 2>&1
echo "===== TOKEN-BURN WIDGET INSTALL $(date) ====="
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin"
PY="$(command -v python3 || echo /usr/bin/python3)"

echo "Restarting tracker server…"
pkill -f "tracker.py" 2>/dev/null; sleep 1
( cd "$BASE" && nohup "$PY" tracker.py >/dev/null 2>&1 & )

echo "Waiting for the first scan to finish…"
for i in $(seq 1 90); do
  s="$(curl -s --max-time 2 http://localhost:8799/api/summary 2>/dev/null)"
  if echo "$s" | grep -q '"loading": false'; then echo "scan done after ${i}s"; break; fi
  sleep 1
done

# Ensure Übersicht is installed (the desktop widget runs inside it).
if [ ! -d "/Applications/Übersicht.app" ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "Übersicht not found — installing it with Homebrew…"
    brew install --cask ubersicht 2>&1 | tail -3
  fi
  # Re-check: Homebrew may be absent, or the brew install may have failed.
  if [ ! -d "/Applications/Übersicht.app" ]; then
    echo ""
    echo "──────────────────────────────────────────────────────────────"
    echo "  Übersicht isn't installed, and Homebrew isn't available to"
    echo "  install it automatically."
    echo ""
    echo "  The desktop widget needs the free Übersicht app:"
    echo "    1. Download it (free):  https://tracesof.net/uebersicht/"
    echo "    2. Open the .dmg and drag Übersicht into Applications"
    echo "    3. Launch Übersicht once (a menu-bar icon appears)"
    echo "    4. Re-run this installer (install-widget.command)"
    echo ""
    echo "  Note: the full dashboard already works without the widget —"
    echo "  just open  http://localhost:8799  in your browser."
    echo "──────────────────────────────────────────────────────────────"
    echo "You can close this window."
    exit 1
  fi
fi

WID="$HOME/Library/Application Support/Übersicht/widgets/token-burn.widget"
mkdir -p "$WID"
cp -f "$BASE/widget/index.jsx" "$WID/index.jsx"
sed -i '' "s|__TRACKER_DIR__|$BASE|g" "$WID/index.jsx"   # bake the real install path into the widget
echo "widget -> $WID/index.jsx ($(stat -f%z "$WID/index.jsx" 2>/dev/null) bytes)"

echo "Refreshing Übersicht…"
killall Übersicht 2>/dev/null; sleep 1; open "/Applications/Übersicht.app" 2>/dev/null; sleep 3
pgrep -f "bersicht" >/dev/null 2>&1 && echo "UBERSICHT_RUNNING=yes" || echo "UBERSICHT_RUNNING=no"

echo "WIDGET_INSTALL_DONE"
echo ">>> Look at the TOP-RIGHT of your desktop for the 'Token Burn' card. Click it to open the full dashboard. <<<"
echo "You can close this window."
