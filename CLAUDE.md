# Token Burn Tracker — dev conventions

## Ports — read this first
- **8799 is the real, always-on install** (launchd `com.dluttz.tokenburn`, runs from `~/.token-burn-tracker`). Never develop against it, never kill it casually — the desktop widget auto-revives it within ~30s anyway.
- **Dev/testing always happens on 8800**: run `./dev.sh` (or `TRACKER_PORT=8800 python3 tracker.py`) and open http://localhost:8800.
- Runtime files (.cache.json, .fixtoken, theme.json, .install_id) are created in the folder the server runs from; all are gitignored here.

## Release checklist (in order)
1. Edit `tracker.py` / `tracker.html` / `docs/*` in this repo.
2. Bump `CACHE_VERSION` in tracker.py **only** if the per-file cache entry shape changed.
3. Bump the `VERSION` file for **any** user-facing release — this is what triggers everyone's "Update now" button. No bump = nobody updates.
4. Sync `tracker.py`, `tracker.html`, `VERSION`, `widget/index.jsx` into `docs/Token Burn Tracker.app/Contents/Resources/`; bump both version keys in `Contents/Info.plist`; rebuild `docs/token-burn-tracker.zip` from the .app (exclude `__pycache__`).
5. Commit + push origin main. Pushing also redeploys the GitHub Pages landing site.
6. raw.githubusercontent.com can serve the old VERSION for ~5 min after a push — the git ref is authoritative.

## Assistant/sandbox quirks
- On the mounted folder, `rm`/`unlink` and in-place zip edits can fail with "Operation not permitted". Build in `/tmp`, then `cp -f` results back. Use a tar-pipe instead of `cp -R` to copy the .app (avoids permission carryover).
- Stale `.git/*.lock` warnings during sandbox commits are harmless; if git errors on locks, remove them from the host Mac.
- Sandbox has no GitHub credentials: commit sandbox-side, the user runs `git push origin main`.

## Cost engine (backend, awaiting UI)
- `GET /api/costs` = dollar view of the usage matrix (tokens × public API list prices from LiteLLM's sheet, cached 24h in `.prices.json`, bundled offline fallback; opt-out `TOKENBURN_PRICES=off`).
- Copy rule: it's an **"API-list-price equivalent"** — subscription plans don't bill per token. Never present it as an invoice or actual spend.
- Unsplit tokens (Codex/custom sources expose totals only) are priced at the input rate and flagged `approx`; unmatched models are listed, never guessed.

## Privacy copy rules (exact)
- OK: "Local-first — your prompts and chats never leave your Mac" + "anonymous, aggregate usage stats (opt-out: TOKENBURN_ANALYTICS=off)".
- NEVER claim "no telemetry" or "100% local".
- Analytics events must stay content-free: no prompts, chat titles, project names, or file paths.

## Architecture in one breath
`tracker.py` = entire backend (stdlib only: scanner, aggregation, HTTP+JSON API on 127.0.0.1, fixes engine, self-updater, analytics). `tracker.html` = entire frontend (served with `__FIX_TOKEN__` injected; state-changing POSTs require that token). `widget/index.jsx` = Übersicht widget (`__TRACKER_DIR__` filled at install). `docs/` = GitHub Pages site + installer + downloadable .app/zip.
