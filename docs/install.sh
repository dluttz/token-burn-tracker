#!/bin/bash
# Token Burn Tracker — one-command installer (100% local; no app to sign, no Gatekeeper prompt).
#
#   curl -fsSL https://dluttz.github.io/token-burn-tracker/install.sh | bash
#
# It downloads the latest files into ~/.token-burn-tracker, starts the local
# dashboard server, opens it in your browser, and (if you have Übersicht) adds
# the desktop widget. Nothing leaves your Mac.
set -e
RAW="https://raw.githubusercontent.com/dluttz/token-burn-tracker/main"
DIR="$HOME/.token-burn-tracker"
PORT=8799
say(){ printf "\033[1;35m%s\033[0m\n" "$1"; }

say "Token Burn Tracker — installing locally (nothing leaves your Mac)…"

# --- Python 3 is required (most Macs already have it) ---
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "Python 3 isn't installed yet. Opening Apple's free Command Line Tools installer…"
  xcode-select --install 2>/dev/null || true
  echo "When that finishes, run this command again."
  exit 1
fi

# --- download the latest files ---
mkdir -p "$DIR/widget"
echo "Downloading the latest files…"
curl -fsSL "$RAW/tracker.py"       -o "$DIR/tracker.py"
curl -fsSL "$RAW/tracker.html"     -o "$DIR/tracker.html"
curl -fsSL "$RAW/widget/index.jsx" -o "$DIR/widget/index.jsx"

# --- (re)start the local server ---
pkill -f "$DIR/tracker.py" 2>/dev/null || true
( cd "$DIR" && TOKENBURN_DATA_DIR="$DIR" nohup "$PY" tracker.py >"$DIR/server.log" 2>&1 & )

# --- wait until it answers, then open the dashboard ---
for i in $(seq 1 60); do
  curl -s --max-time 2 "http://localhost:$PORT/api/summary" >/dev/null 2>&1 && break
  sleep 1
done
open "http://localhost:$PORT/" 2>/dev/null || true

# --- optional desktop widget (needs the free Übersicht app) ---
if [ -d "/Applications/Übersicht.app" ]; then
  W="$HOME/Library/Application Support/Übersicht/widgets/token-burn.widget"
  mkdir -p "$W"
  cp -f "$DIR/widget/index.jsx" "$W/index.jsx"
  sed -i '' "s|__TRACKER_DIR__|$DIR|g" "$W/index.jsx" 2>/dev/null || true
  open "/Applications/Übersicht.app" 2>/dev/null || true
  say "Desktop widget added."
else
  echo "Optional desktop widget: install Übersicht (free) from https://tracesof.net/uebersicht,"
  echo "then run this command again to add the widget to your desktop."
fi

say "Done — your dashboard is open at http://localhost:$PORT"
cat <<EOF

  Keep this handy:
    Restart:   ( cd ~/.token-burn-tracker && python3 tracker.py & ) ; open http://localhost:$PORT
    Stop:      pkill -f tracker.py
    Uninstall: pkill -f tracker.py ; rm -rf ~/.token-burn-tracker
                 (and delete the 'token-burn' widget from Übersicht's widgets folder)

EOF
