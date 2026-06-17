# Token Burn Tracker

A **100% local** dashboard that shows how many tokens your AI coding tools are
burning — live, on your own Mac. No account, no cloud, nothing leaves your machine.

Works with **Claude Code**, **Cowork**, and **Codex** out of the box, and you can
add other tools yourself.

- **macOS only** · **Python 3** (no pip packages) · **MIT licensed** · v1.0.0

---

## What it shows

- **Running now** — every active chat/session by its title, with live CPU, memory,
  and token burn rate (like Activity Monitor, but for your AI tools).
- **Token usage** broken down by tool, project, session, and day.
- **Over time** — a stock-style chart with 1D / 5D / 1M / All ranges.
- **Sub-agents** — what each agent run is actually doing inside (tools touched,
  patches, reasoning steps, sub-agents it spawned).
- **Calendar** heatmap of daily usage.
- **Efficiency** suggestions and a **Cleanup** panel that finds idle leftover
  processes you can safely quit.
- **Themes** (Light / Dark / Soft) and a customizable, drag-to-reorder layout.

---

## Requirements

- **macOS.** The tool reads macOS process and system info (`ps`, `lsof`, `vm_stat`,
  `launchctl`, …), so it is macOS-only and won't run on Linux or Windows.
- **Python 3.** Pre-installed on most Macs; if not, run `xcode-select --install`
  (free, from Apple). No other dependencies — it's pure Python standard library.
- **Übersicht** (optional) — only if you want the desktop widget.

---

## Install & run

1. **Download** this folder and unzip it **anywhere** (Desktop, Downloads, etc.).
   It figures out its own location, so it doesn't matter where you put it.

2. **First run — clear the macOS block.** Because the files came from the internet,
   macOS may say *"cannot verify the developer."* Either:
   - **Right-click `install.command` → Open → Open**, or
   - run `install.command`, which clears the quarantine flag for the whole folder,
     checks for Python 3, and starts the dashboard for you.

3. **Everyday use:**
   - `start.command` — runs the dashboard in a visible window (close it to quit).
   - `run-in-background.command` — keeps running after you close the window.
   - `stop.command` — stops it.

4. Open **http://localhost:8799** (the launchers open it for you).

> Change the port with the `TRACKER_PORT` environment variable (default `8799`).

---

## What it reads — and your privacy

It reads the local log files your AI tools already write:

- Claude Code — `~/.claude/projects`
- Cowork — `~/Library/Application Support/Claude/local-agent-mode-sessions`
- Codex — `~/.codex/sessions` (and its local SQLite for scheduled automations)

The dashboard can display your **chat titles and full transcripts** so you can see
where tokens went. **All of this is rendered locally in your browser.**

- **The server makes no outbound network calls.** The only network traffic is your
  browser talking to `localhost`. Nothing is uploaded anywhere.
- A small cache (`.cache.json`) and a per-install action token (`.fixtoken`) live in
  this folder. They are **git-ignored** so you never publish your own prompts or token.

---

## Actions (Cleanup / one-click fixes) — what's safe

The "Quit idle leftovers" button and one-click fixes are deliberately conservative:

- **Local only** — every action endpoint is gated to `localhost` and requires the
  per-install token, so no website can trigger them.
- **Re-verified** — the exact process is re-checked right before it's signaled.
- **Active sessions are protected** — anything that wrote to disk recently is treated
  as busy and left alone.
- **Reversible where possible** — removed login items are moved to the Trash, not
  hard-deleted.

It will never touch your AI logs.

---

## Add another tool

If you use an AI tool that isn't Claude Code / Cowork / Codex, open
**Tools → Add a tool** in the dashboard and point it at:

- a **log file glob** (e.g. `~/.mytool/**/*.jsonl`), and
- the **field names** that hold token counts.

It then tracks that tool alongside the built-ins. (Custom sources are saved locally
in `custom_sources.json`, which is git-ignored.)

---

## Desktop widget (optional)

`install-widget.command` installs an Übersicht "Token Burn" card that shows today's
burn at a glance and keeps the server alive. It bakes the correct folder path in at
install time, so it works no matter where you unzipped the app.

---

## Uninstall

- `uninstall.command` — stops the server, removes the Übersicht widget, and moves this
  folder's cache + token to the Trash (reversible, and it asks first). It does **not**
  delete your AI logs.
- To remove the app entirely, drag this folder to the Trash afterward.

---

## Troubleshooting

- **"macOS cannot verify the developer."** Right-click the `.command` → **Open**, or
  run `install.command` once to clear it for the whole folder.
- **"python3: command not found."** Run `xcode-select --install`, then try again.
- **Port already in use.** Set a different port: `TRACKER_PORT=8800 ./start.command`.
  (The launchers only ever stop *this* tool's own server — they won't kill an
  unrelated service that happens to use the port.)
- **A tool shows zero.** If a vendor changes their log format, the dashboard shows a
  self-check warning and that tool may read zero until the parser is updated.

---

## How it works

A small pure-stdlib Python HTTP server (`tracker.py`) parses each tool's log format,
caches results by file modification time, and serves a single-page dashboard
(`tracker.html`). No frameworks, no build step, no dependencies.

Tested against the Claude Code / Cowork / Codex log formats as of **June 2026**.

---

## License & attribution

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by Anthropic or OpenAI. "Claude", "Claude Code",
"Cowork", and "Codex" are the property of their respective owners; this project simply
reads the logs those tools write on your own machine.
