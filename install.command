#!/bin/bash
# Token Burn Tracker — first-run setup for macOS. Safe to re-run.
# Clears the download "quarantine" flag so the .command files aren't blocked,
# makes the launchers runnable, checks for Python 3, then starts the dashboard.
BASE="$(cd "$(dirname "$0")" && pwd)"   # the folder this script lives in — works anywhere
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin"

echo "Token Burn Tracker — setup"
echo "Folder: $BASE"
echo

# 1) Remove the macOS quarantine flag for everything in this folder
echo "- Clearing the macOS quarantine flag..."
xattr -dr com.apple.quarantine "$BASE" 2>/dev/null || true

# 2) Make the launcher scripts executable
chmod +x "$BASE"/*.command 2>/dev/null || true

# 3) Check for Python 3
if ! command -v python3 >/dev/null 2>&1; then
  echo
  echo "!! Python 3 was not found."
  echo "   Install Apple's Command Line Tools (free), then re-run this installer:"
  echo
  echo "        xcode-select --install"
  echo
  echo "   When it finishes, double-click install.command again."
  echo
  echo "You can close this window."
  exit 1
fi
echo "- Python 3 found: $(python3 --version 2>&1)"
echo
echo "- Starting the dashboard (this window stays open while it runs)..."
echo

# 4) Hand off to the normal launcher
exec "$BASE/start.command"
