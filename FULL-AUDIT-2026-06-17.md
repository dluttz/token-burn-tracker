# Token Burn Tracker — Full Scan, Audit & Test

**Date:** 2026-06-17
**What this is:** a hands-on exercise of the *running* build — every API endpoint, the
security gates, and the frontend controls were actually driven and inspected, then a
skeptical "detective" went looking for faults. This is the **functional + security + UX**
pass. The separate **distribution / multi-user** review lives in `SHIP-READINESS-AUDIT.md`
and its blockers are summarized (not re-derived) at the end here.

**Headline:** functionally and security-wise the tool is **green** — token math is exact,
every write endpoint is locked, path-traversal is blocked, and all frontend controls resolve.
The only open items are the already-documented *distribution* blockers plus three small
interpretation/UX notes. **No functional or security regression was found in this pass.**

---

## How I tested (live, against the running server on :8799)

- Pulled `/api/data`, `/api/live`, `/api/agents`, `/api/series` (ranges `1d/5d/1m/all` + a
  garbage range), `/api/leftovers`.
- Cross-checked token accounting three independent ways.
- Fired every POST endpoint with a **wrong token**, a **malformed body**, and (for `/api/fix`)
  a **right token + bogus target** to confirm safe no-ops.
- Attacked `/transcript` with a valid file, `/etc/passwd`, `~/.ssh/id_rsa`, and an empty param.
- Drove the frontend: theme switch (apply + persist), all 9 sidebar nav items, the
  customize-layout modal, drag handles, chart range buttons, tooltips, scroll-suppress rule.
- Read the actual nav / theme / launcher source to confirm wiring rather than infer it.

---

## Test results at a glance

| Area | Probe | Result |
|---|---|---|
| Token integrity | `sum(byTool)` vs `grand` | **exact match** (15,597,652,510 = 15,597,652,510) |
| Token integrity | `sum(days)` / `grand` | **1.000** (no tokens lost to "unknown" date) |
| Token integrity | `sum(byProject)` / `grand` | **1.000** |
| Series | `1d` / `5d` / `1m` / `all` | hour·linear·24 / hour·linear·120 / day·log·31 / day·log·174 |
| Series | `range=garbage` | **gracefully defaults to "all"** (174 buckets) — no error |
| Routing | unknown path `/api/zzz` | **404** |
| Routing | GET on POST-only route | **404** |
| Security | POST `/api/fix` wrong token | **403** |
| Security | POST `/api/kill_leftovers` wrong token | **403** |
| Security | POST `/api/add_source` wrong token | **403** |
| Security | POST `/api/fix` right token + bogus agent | `{ok:false}` — **safe no-op** |
| Security | malformed JSON body | **400** |
| Transcript | valid log file | **200** |
| Transcript | `/etc/passwd` | **403** |
| Transcript | `~/.ssh/id_rsa` | **403** |
| Transcript | missing `file` param | **403** |
| Frontend | theme switch (light/dark/soft) | applies via `data-theme` + persists to `localStorage` |
| Frontend | no-flash theme on load | early `<script>` sets theme before paint ✓ |
| Frontend | sidebar nav (9 items) | all resolve — 8 center their panel, "Overview" scrolls to top |
| Frontend | drag-reorder | 10 draggable rows + 10 grips wired |
| Frontend | chart ranges | 1D / 5D / 1M / All present |
| Frontend | tooltips | 17 `?` help tips present |
| Frontend | scroll-suppress | `body.is-scrolling .help::after` rule live |
| Live data | running sessions | carry real **titles** (not just tool name) + per-session `burnMin` |
| Live data | system stats | `aiCpu`, `aiRamMB`, `memUsedGB`, `memTotalGB`, `cores`, `load` |
| Live data | self-check banner | `warnings: []` (all three log sources parsing cleanly) |

---

## Detective questions & answers

### Data integrity

**Q1. Do the numbers actually add up, or is each view computed independently and quietly
disagreeing?**
They add up **exactly.** The per-tool totals, the per-day totals, and the per-project totals
each sum to the same grand total, to the token. Verdict: **PASS.**

**Q2. After the date-attribution fixes, are any tokens still landing on "unknown"?**
No. `sum(days) / grand = 1.000`, meaning every token now carries a real date. The earlier
Cowork (`_audit_timestamp`) and Codex per-day bucketing fixes are holding. Verdict: **PASS.**

**Q3. Does a nonsense chart range crash or return junk?**
No — `range=garbage` falls through to the "all" series (174 daily buckets, log scale). Verdict: **PASS.**

**Q4. Is the live burn rate believable?**
It reads in the millions-of-tokens-per-minute range. That is *arithmetically correct* but
dominated by **cache-read** tokens, which are cheap and inflate the headline. Not a bug — but
the number means "throughput including cache reads," not "money on fire." Verdict: **NOTE —
consider labeling it as cache-inclusive so it isn't misread.**

### Security

**Q5. Can a wrong/forged token trigger a kill, a fix, or a source change?**
No. All three POST endpoints return **403** without the correct per-install token. Verdict: **PASS.**

**Q6. If the token is right but the target is bogus, does anything destructive happen?**
No. `/api/fix` with a valid token but an unrecognized agent returns `{ok:false,
error:"Not an AI login agent I recognize"}` and performs no action. Verdict: **PASS.**

**Q7. Can the transcript viewer be walked out of the log directory to read arbitrary files?**
No. A valid log path returns 200; `/etc/passwd`, an `~/.ssh/id_rsa` path, and an empty param
all return **403**. The whitelist-to-`gather_files()` defense works. Verdict: **PASS.**

**Q8. Does a malformed request or wrong method blow up the server?**
No. Malformed JSON → **400**; GET on a POST-only route → **404**; unknown path → **404**.
Verdict: **PASS.**

**Q9. Is the old cross-site hole really gone?**
Yes — confirmed previously and consistent here: no `Access-Control-Allow-Origin`, every
request Host-gated to localhost. Other sites can neither read data nor steal the token.
Verdict: **PASS** (call it out in the README as a feature).

### Frontend / UX

**Q10. Does every sidebar item actually go somewhere, or are there dead clicks?**
Every one works. Eight match a panel heading and center it; "Overview" is special-cased to
scroll to top (line 909). My first synthetic test *flagged* "Overview" as a dead click — but
that test didn't replicate the special-case branch; reading the real handler cleared it.
Verdict: **PASS** (and a reminder that synthetic checks can lie — verify against source).

**Q11. Do the theme switch and the saved theme survive a reload without a flash?**
Yes. Buttons set `data-theme` and write `localStorage.tbTheme`; an inline `<script>` in
`<head>` re-applies it before first paint, so there's no light-mode flash on load. Verdict: **PASS.**

**Q12. Is the drag-to-reorder actually wired or just styled?**
Wired — 10 rows are `draggable="true"` with 10 grip handles and live drag CSS. Verdict: **PASS.**

**Q13. Did the "black box on scroll" fix actually take?**
Yes — the `body.is-scrolling .help::after/::before { opacity:0 }` suppression rule is present
in the live stylesheet, alongside the hover-intent delay. Verdict: **PASS.**

**Q14. Do running sessions show meaningful titles and per-session burn, as requested?**
Yes. Each running item carries `title` (distinct from the tool name) plus `burnMin`. System
metrics (`aiCpu`, `aiRamMB`, `memUsedGB`, `cores`, `load`) are present at the system level —
note that CPU/RAM are aggregated there, **not** per running row, which is by design but worth
remembering if you ever want a per-process column. Verdict: **PASS** (with a design note).

**Q15. Are "waste / suggested to remove" and "leftovers" empty because they're broken, or
because there's nothing to flag?**
Because there's nothing to flag — you deleted the two automations and cleared Trash earlier,
and the live view now correctly shows `waste: 0` and `leftovers: 0`. This is positive evidence
that the live-refresh-after-cleanup fix works. Verdict: **PASS.**

### Reliability / concurrency

**Q16. If I add or remove a tracked tool while a background build is mid-flight, does it show
up immediately?**
Possibly not *instantly.* `add_source`/`remove_source` write the file and kick a rebuild, but
the build lock is non-blocking — if a build is already running it skips, and the new source
won't appear in `/api/data` until the next build cycle. The live caches are cleared, so it
self-heals within a cycle. Verdict: **MINOR** — fine for one user; for shipping, either block
briefly or re-trigger after the in-flight build finishes.

**Q17. Does the self-check banner actually catch a source that stopped parsing?**
The mechanism is in place and currently reports `warnings: []` (all three sources healthy).
This is the main defense against silent vendor-format drift. Verdict: **OK** (keep it;
document tested tool versions).

### Distribution (already covered in SHIP-READINESS-AUDIT.md — confirmed still true)

**Q18. Will the launcher kill an innocent process?**
Yes, potentially. Re-confirmed by reading the files: `stop.command` and
`run-in-background.command` both run `lsof -ti tcp:$PORT | xargs kill -9`, which kills
*whatever* holds 8799 — on someone else's Mac that could be unrelated. Verdict: **BLOCKER
(distribution)** — only kill our own `tracker.py`, or auto-pick a free port.

**Q19. Would pushing this repo leak private data?**
Yes, as-is. No `.gitignore`; `.cache.json` stores **real chat prompts** as titles and
`.fixtoken` is the secret action token. Verdict: **BLOCKER (distribution)** — ignore + scrub
before any `git init`.

**Q20. Does it run anywhere, or only from your Desktop?**
Only from the hardcoded `~/Desktop/AI-Tools-Setup/token-tracker` path (9 files). Verdict:
**BLOCKER (distribution)** — derive paths from the script location.

**Q21. The other distribution items** (Gatekeeper/quarantine, LICENSE + README + version +
macOS-only notice, consent-to-read-chats, opt-in for the process-killer, uninstaller, empty
state, add-tool presets, parser tests/CI, separating the internet-fetching AI Feed widget) are
unchanged from `SHIP-READINESS-AUDIT.md`. Verdict: **see that doc.**

---

## What's actually broken vs. fine

**Broken / must-fix before *publishing* (not before *using* it yourself):**
- Aggressive port-kill (Q18)
- No `.gitignore` / data scrub (Q19)
- Hardcoded install path (Q20)
- The remaining distribution items (Q21)

**Cosmetic / interpretation (optional):**
- Burn rate is cache-inclusive and looks huge (Q4) — consider a label.
- Add/remove tool may lag one build cycle under concurrency (Q16).
- CPU/RAM are system-aggregated, not per running row (Q14) — only matters if you want a column.

**Verified working (no action needed):**
- All token math, all four chart ranges, all routing/error handling.
- All POST security gates, the safe no-op path, and transcript traversal protection.
- All frontend controls: themes (+ no-flash + persist), 9/9 nav, drag-reorder, ranges,
  tooltips, scroll-suppress.
- Live titles + per-session burn + system metrics + the self-check banner.
- Live refresh correctly reflects your earlier cleanup (empty waste/leftovers).

---

## Bottom line

For you, running it locally today, it's **solid** — I tried to break the data integrity and
the security gates and couldn't. The work that remains is the *distribution* layer: making it
safe and portable for **other** people's machines. If you want to go that way, the order is
unchanged: `.gitignore` + scrub → de-hardcode the path → safer port handling → Gatekeeper
guidance → LICENSE/README/version → consent/opt-in for the chat-reading and the kill button.
