# Token Burn Tracker

A **100% local** dashboard that shows how many tokens your AI coding tools are
burning — live, on your own Mac. No account and no cloud — your prompts and token data stay on your Mac.

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

**One command (recommended).** Paste this into Terminal and press Return:

```bash
curl -fsSL https://dluttz.github.io/token-burn-tracker/install.sh | bash
```

It downloads the latest files into `~/.token-burn-tracker`, starts the local
dashboard, opens **http://localhost:8799**, and — if you have Übersicht — adds the
desktop widget. Because it runs from Terminal, there's **no macOS "unverified
developer" prompt**, and your prompts and logs stay on your Mac.

**Everyday use afterwards:**

- **Restart:** `( cd ~/.token-burn-tracker && python3 tracker.py & ) ; open http://localhost:8799`
- **Stop:** `pkill -f tracker.py`
- **Update:** run the install command again (it always pulls the latest).

> Change the port with the `TRACKER_PORT` environment variable (default `8799`).

**Prefer a clickable app?** Download the [app build](https://dluttz.github.io/token-burn-tracker/token-burn-tracker.zip);
on first open use **System Settings → Privacy & Security → Open Anyway** (it's
unsigned, so macOS asks once).

---

## What it reads — and your privacy

It reads the local log files your AI tools already write:

- Claude Code — `~/.claude/projects`
- Cowork — `~/Library/Application Support/Claude/local-agent-mode-sessions`
- Codex — `~/.codex/sessions` (and its local SQLite for scheduled automations)

The dashboard can display your **chat titles and full transcripts** so you can see
where tokens went. **All of this is rendered locally in your browser.**

- **Your prompts, logs, and token counts never leave your Mac** — they're read and
  rendered locally, never uploaded. The app does send **anonymous, aggregate usage
  stats** (a random install ID, app + macOS version, and which integrations you use —
  never any content) to help improve it. Turn it off with `TOKENBURN_ANALYTICS=off`.
- A small cache (`.cache.json`) and a per-install action token (`.fixtoken`) live in
  `~/.token-burn-tracker` (or next to `tracker.py` for the app build) — never published.

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

If you have the free [Übersicht](https://tracesof.net/uebersicht) app, the install
command adds a "Token Burn" desktop card automatically. Don't have Übersicht yet?
Install it, then run the install command again to add the widget.

---

## Uninstall

```bash
pkill -f tracker.py
rm -rf ~/.token-burn-tracker
```

Then delete the `token-burn` widget from Übersicht's widgets folder if you added it.
Your AI logs are never touched.

---

## Troubleshooting

- **"macOS cannot verify the developer."** Only happens with the downloadable app —
  use **System Settings → Privacy & Security → Open Anyway**. The one-command install
  avoids this entirely.
- **"python3: command not found."** Run `xcode-select --install`, then try again.
- **Port already in use.** Set a different port: `TRACKER_PORT=8800 python3 tracker.py`.
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
