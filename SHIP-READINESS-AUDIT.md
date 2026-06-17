# Token Burn Tracker — Ship-Readiness Audit

**Context:** evaluating whether the Token Burn widget/dashboard could be published as a
GitHub repo / website for other people to download and run on their own Macs.
**Date:** 2026-06-17.
**Status:** the tool is functionally sound and **fully local** (the earlier security hole —
a wildcard CORS header — is already fixed, and all requests are gated to a local Host). It is
**not yet distributable.** This document lists what a skeptical reviewer would ask and the
honest answers, so we can fix the blockers before publishing. (Do this AFTER the planned UI work.)

---

## TL;DR — must-fix before any public repo

1. **Add `.gitignore` + scrub the folder.** There is no `.gitignore`, and the folder contains
   `.cache.json` (which stores **real chat prompts** as session titles), `.fixtoken` (the secret
   action token), `custom_sources.json`, `*.log`, `*.out`, `probe-*.txt`, and `.DS_Store`.
   Pushing as-is publishes your own prompts and secret token. Ignore those + delete probe files.
2. **De-hardcode the install path.** Nine files hardcode `~/Desktop/AI-Tools-Setup/token-tracker`.
   Derive paths from the script location instead so it runs anywhere a user unzips it.
3. **Gatekeeper / quarantine.** Downloaded `.command` files get blocked ("can't verify developer").
   Notarize, or ship an install script + clear "right-click → Open" instructions.
4. **LICENSE + README + "macOS-only" notice + version.** None exist today. (Use "works with
   Claude Code / Codex / Cowork" language — don't imply vendor endorsement.)
5. **Safer port handling.** Launchers run `lsof -ti tcp:8799 | xargs kill -9`, which kills ANY
   process on 8799 — on a stranger's machine that could be an unrelated service. Only kill our own
   `tracker.py`, or auto-pick a free port.

---

## Severity tiers

- **Ship-blockers:** #2 install path, #3 Gatekeeper, #6 macOS-only undocumented, #13 trust/signing,
  #16 repo data leak, #22 license/README/version.
- **High:** #1 Python runtime, #10 shipping a process-killer, #15 chat-reading consent.
- **Medium:** #4 port kill, #5 widget positions, #8 empty state, #9 version drift, #20 uninstall,
  #21 add-tool is technical, #23 tests/CI, #24 AI Feed external calls.
- **Low:** #17 first-scan cost, #18 synced dirs, #19 silent failures.
- **Already fine to ship:** #11 CORS fixed, #12 no outbound calls, #14 transcript/add-tool safe,
  #7 graceful when tools absent.

---

## Full Q&A

### Install & portability

**1 — Runtime not guaranteed (HIGH).** Pure-stdlib Python 3 (no pip deps — good), but it shells to
`python3`, which a fresh macOS doesn't ship (only a "install Command Line Tools" stub). Fix:
document/installer-check for Python 3.

**2 — Hardcoded install path (SHIP-BLOCKER).** `start.command`, `run-in-background.command`,
`apply-layout.command`, `install-widget.command`, `widget/index.jsx`, etc. hardcode
`~/Desktop/AI-Tools-Setup/token-tracker`. Clone it elsewhere → launchers and widget break.
Fix: derive from `$(dirname "$0")` / `$(pwd)`; never assume Desktop.

**3 — Gatekeeper blocks downloaded `.command` (SHIP-BLOCKER for ease-of-use).** Quarantine flag →
"macOS cannot verify the developer." Fix: notarized app, or `xattr -dr com.apple.quarantine` +
right-click→Open instructions, ideally an install script.

**4 — Port-claim too aggressive (MEDIUM).** Verified: both launchers `lsof -ti tcp:8799 | xargs kill -9`
= kills whatever holds 8799. Fix: only kill our `tracker.py`, or pick a free port and pass it to the
widget/dashboard.

**5 — Widgets assume your screen (MEDIUM).** Übersicht widgets hardcode pixel positions and the
"clock → AI Feed → Token Burn" column for your display; other resolutions may overlap/clip. Fix:
configurable/auto placement.

### OS & dependency assumptions

**6 — macOS-only, undocumented (SHIP-BLOCKER if unstated).** Depends on `ps`, `lsof`, `vm_stat`,
`sysctl`, `launchctl`, `plutil`, `~/.Trash`, `/usr/sbin/...`. No Linux/Windows. Fine as a macOS tool —
must say so. Fix: state it; degrade gracefully elsewhere.

**7 — Mostly graceful (OK).** Dashboard needs only Python 3; desktop widgets need Übersicht; logs read
only if the tools exist. Fix: README should separate "dashboard" vs "widgets."

**8 — Empty state is thin (LOW/MED).** A user with none of the three tools sees zeros / "no data" — looks
broken. Fix: friendly onboarding ("no logs found yet — add your tool").

**9 — Version drift = silent zeros (MEDIUM).** Everything rides on reverse-engineered formats
(Claude/Cowork JSONL keys, Cowork `_audit_timestamp`, Codex `rollout` events, `codex-dev.db` schema).
A renamed field → that tool reads zero. Mitigated by the self-check banner already added. Fix: keep
banner; document tested tool versions.

### Security & trust

**10 — Shipping a one-click process-killer / login-agent-remover (HIGH, trust).** Actions are guarded
(local-only, per-install token, reversible-to-Trash, whitelisted, "recently-active" protection) — solid
for a single owner, but riskier for strangers (a heuristic bug could kill something wanted). Fix: opt-in
+ consent, an undo log, default to "show command, don't auto-run" unless enabled.

**11 — Other sites reading data / triggering actions (FIXED — OK).** CORS `*` removed; no ACAO header;
every request gated to a local Host. Other websites can't read data or steal the token. Note it in README.

**12 — Phone home? (NO — OK, selling point).** Verified `tracker.py` makes no outbound calls (only
`urllib.parse` for query strings). Nothing leaves the machine.

**13 — Download trust (SHIP-BLOCKER for trust).** Unsigned, un-notarized, no checksums/releases. Users
run an unsigned local server that can kill processes — on faith. Fix: signed releases, checksums, clear
"what it does / can't do" security section, readable open source as the trust anchor.

**14 — Transcript/add-tool abuse (OK).** Transcript viewer only serves files in the known log set (no
traversal — `/etc/passwd` → 403) and HTML-escapes content (no XSS). "Add a tool" parses JSON, no `eval`,
bounded reads.

### Privacy & data

**15 — Reads private chats; needs consent (MEDIUM, privacy).** Reads and can display full chat
transcripts + project paths (all local). A downloaded tool should say up-front what it reads and that it
stays local. Fix: first-run consent/explainer.

**16 — Repo leaks private data as-is (SHIP-BLOCKER).** No `.gitignore`. Folder has `.cache.json` (real
prompts as titles — confirmed example: "You are labeling a tutoring video…"), `.fixtoken` (secret),
`custom_sources.json`, `tracker.out`, `*.log`, `probe-*.txt`, `.DS_Store`. Pushing publishes prompts +
token. Fix (must-do): `.gitignore` those + `__pycache__`; remove probe files.

### Reliability & resource use

**17 — First scan heavy (LOW/MED).** First build parses every log file, held in memory; CPU/time spike on
big histories. Steady-state cached + 8s poll with `ps`/`lsof`. Fix: progress shown; consider trimming old
data / lazy load.

**18 — Synced/locked dirs (LOW).** In iCloud/Dropbox, `.cache.json`/`.fixtoken` writes + build lock can
fight sync. Fix: recommend installing outside synced folders; tolerate write failures (already best-effort).

**19 — Failure modes mostly handled, but silent (LOW).** Missing logs/malformed lines/port busy/permission
errors degrade; weak spot = silent widget relaunch failure if `python3` not found. Fix: visible health line.

**20 — Persistence & uninstall informal (MEDIUM).** Survives terminal-close (nohup); the Übersicht widget
relaunches the server; no login item by default (fine); but no clean uninstaller. Fix: `uninstall.command`
(stop server, remove widgets, delete cache/token).

### Onboarding, legal, maintenance

**21 — "Add a tool" too technical (MEDIUM).** Asks for a log glob + JSON token-field names — fine for devs,
opaque for others. Fix: presets for common tools so users pick from a list.

**22 — No license/README/version/updates (SHIP-BLOCKER).** None exist. Need LICENSE (trademark-safe
"works with" language), README, version, and an update path (today = re-download + re-edit paths). Fix: add all.

**23 — No tests/CI (MEDIUM, maintainability).** Nothing guards the fragile parsers against regressions /
vendor changes. Fix: unit tests for parsers (codex + custom already hand-tested) + CI lint/compile.

**24 — Bundled "AI Feed" widget reaches the internet (note).** Unlike the Token Burn server, the AI Feed
widget fetches GitHub/Hacker News — outbound traffic + offline/rate-limit behavior to disclose. Fix: ship
it as a separate, optional widget so "Token Burn = 100% local" stays true.

---

## Recommended order for the "make it shippable" pass (later)

1. `.gitignore` + scrub (stop leaking prompts/token).
2. De-hardcode install path (runs anywhere).
3. Gatekeeper guidance / install script (or notarize).
4. LICENSE + README + macOS-only notice + version.
5. Safer port handling (don't kill strangers' services).
6. Trust items: consent/explainer for reading chats; opt-in for process-killing actions.
7. Polish: uninstaller, empty state, add-tool presets, parser tests/CI, separate the AI Feed widget.

> Note: the separate *functional + security* audit done earlier (CORS, build lock, persistent token,
> Cowork/Codex date attribution, kill-safety, memory streaming, gather-files caching) is **already
> applied** in the code. This document is specifically the **distribution / multi-user** layer.
