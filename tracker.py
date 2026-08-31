#!/usr/bin/env python3
"""
Token Burn Tracker — reads your local logs and serves a live dashboard + widget feed.

Sources:
  Claude Code : ~/.claude/projects/**/*.jsonl
  Cowork      : ~/Library/Application Support/Claude/local-agent-mode-sessions/**/*.jsonl
  Codex       : ~/.codex/sessions/**/rollout-*.jsonl   (+ ~/.codex/session_index.jsonl for titles)

Adds a "why" view: tokens grouped by SESSION, titled by the first user prompt
(Claude Code / Cowork) or the Codex thread name. stdlib only; binds to 127.0.0.1.
"""
import http.server, socketserver, json, os, re, glob, threading, datetime, traceback, subprocess, time, sqlite3, secrets, signal, platform, urllib.request, urllib.parse, tempfile
from collections import defaultdict, deque

SESS_RE = re.compile(r'local_[0-9a-fA-F-]{6,}')
def session_key(tool, sid, path):
    if sid and sid != "?":
        return tool + ":" + sid
    m = SESS_RE.search(path)
    if m:
        return tool + ":" + m.group(0)
    return tool + ":" + os.path.dirname(path)

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
# Mutable files (cache, token, custom sources, theme) live in a writable data dir so the
# app bundle's Resources can stay read-only. Falls back to HERE for plain folder installs.
DATA_DIR = os.environ.get("TOKENBURN_DATA_DIR") or HERE
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = HERE

# ---------- anonymous, opt-out usage analytics (never sends content) ----------
# Sends anonymous, aggregate usage: app + macOS version, which tools are used, token TOTALS
# by tool/model, the input/cache/output efficiency split, and activity counts — so usage can be
# understood (and, for orgs, reported in aggregate). Never sends prompts, chat titles, project
# names, or file paths. Keyed by a random install id, not identity. Turn it off: TOKENBURN_ANALYTICS=off
PH_KEY = "phc_sfV8RXR5sqqRboLPP8Px75FDBPzoGmgHZqrKrT8nEfZv"
PH_HOST = os.environ.get("TOKENBURN_PH_HOST", "https://us.i.posthog.com")
ANALYTICS_ON = os.environ.get("TOKENBURN_ANALYTICS", "on").lower() not in ("off", "0", "false", "no")
def _install_id():
    p = os.path.join(DATA_DIR, ".install_id")
    try:
        if os.path.exists(p): return open(p).read().strip()
        iid = secrets.token_hex(16); open(p, "w").write(iid); return iid
    except Exception:
        return "unknown"
def _is_internal():
    """True on the developer's own machines/runs so their usage can be excluded from real-user metrics.
    Mark a machine with:  touch ~/.tokenburn_internal   (or set env TOKENBURN_INTERNAL=1)."""
    if os.environ.get("TOKENBURN_INTERNAL", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    for p in (os.path.expanduser("~/.tokenburn_internal"), os.path.join(DATA_DIR, ".internal")):
        try:
            if os.path.exists(p):
                return True
        except Exception:
            pass
    return False
def analytics_event(event, props=None):
    if not (ANALYTICS_ON and PH_KEY): return
    def _send():
        try:
            p = dict(props or {}); p.setdefault("internal", _is_internal())   # tag dev/self runs so they can be filtered out
            body = json.dumps({"api_key": PH_KEY, "event": event,
                               "distinct_id": _install_id(), "properties": p}).encode()
            req = urllib.request.Request(PH_HOST.rstrip("/") + "/capture/", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=4).read()
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()
def analytics_launch():
    try:
        ver = "?"
        try: ver = open(os.path.join(HERE, "VERSION")).read().strip()
        except Exception: pass
        analytics_event("app_launched", {
            "app_version": ver, "macos": (platform.mac_ver()[0] or "?"),
            "python": platform.python_version(), "$os": "Mac OS X",
            "uses_claude_code": os.path.isdir(HOME + "/.claude/projects"),
            "uses_cowork": os.path.isdir(HOME + "/Library/Application Support/Claude/local-agent-mode-sessions"),
            "uses_codex": os.path.isdir(HOME + "/.codex/sessions"),
            "widget_installed": os.path.isdir(HOME + "/Library/Application Support/Übersicht/widgets/token-burn.widget"),
        })
    except Exception:
        pass

_USAGE_SENT = False
def analytics_usage(d):
    """Anonymous, content-free usage snapshot: token totals, tool/model mix, efficiency split, and
    activity counts. NEVER includes chat titles, prompts, project names, or file paths. Once per launch."""
    global _USAGE_SENT
    if _USAGE_SENT or not ANALYTICS_ON or not isinstance(d, dict):
        return
    _USAGE_SENT = True
    try:
        tb = d.get("tokenBreakdown") or {}
        tot = sum(int(v or 0) for v in tb.values()) or 1
        cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        active = sum(1 for x in (d.get("days") or []) if x.get("date", "") >= cutoff and (x.get("total") or 0) > 0)
        sessions = d.get("bySession") or []
        heavy = sum(1 for s in sessions if len(s) > 3 and (s[3] or 0) >= 1000000)   # count only, no titles
        byTool = d.get("byTool") or {}
        models = [m[0] for m in (d.get("byModel") or [])[:5] if isinstance(m, (list, tuple)) and m]   # model names only
        analytics_event("app_usage", {
            "app_version": local_version(), "macos": (platform.mac_ver()[0] or "?"), "$os": "Mac OS X",
            "grand_total": int(d.get("grand") or 0), "today_total": int(d.get("today") or 0), "week_total": int(d.get("week") or 0),
            "tok_input": int(tb.get("input") or 0), "tok_cache_read": int(tb.get("cache_read") or 0),
            "tok_cache_write": int(tb.get("cache_write") or 0), "tok_output": int(tb.get("output") or 0),
            "cache_read_pct": round(100.0 * int(tb.get("cache_read") or 0) / tot, 1),
            "tokens_claude_code": int(byTool.get("Claude Code") or 0),
            "tokens_cowork": int(byTool.get("Cowork") or 0),
            "tokens_codex": int(byTool.get("Codex") or 0),
            "active_days_30": active, "session_count": len(sessions), "heavy_chat_count": heavy,
            "top_models": models,
        })
    except Exception:
        pass

# ---------- in-app "update available" check ----------
# Fail-silent, network-lazy (only fetched on /api/data, cached ~1h via _cached()) version check
# against the public VERSION file on GitHub. Never blocks startup, never raises.
UPDATE_VERSION_URL = "https://raw.githubusercontent.com/dluttz/token-burn-tracker/main/VERSION"
UPDATE_INSTALL_CMD = "curl -fsSL https://dluttz.github.io/token-burn-tracker/install.sh | bash"
def local_version():
    try:
        return open(os.path.join(HERE, "VERSION")).read().strip() or "?"
    except Exception:
        return "?"
def _ver_tuple(v):
    """'1.2.3' -> (1,2,3); unknown/'?'/blank -> (0,) so any real remote version outranks it."""
    if not v or v == "?":
        return (0,)
    parts = []
    for p in str(v).strip().split("."):
        try:
            parts.append(int(p))
        except Exception:
            parts.append(0)
    return tuple(parts) or (0,)
def _version_newer(a, b):
    """True if version string a > version string b, comparing numerically component-by-component."""
    ta, tb = _ver_tuple(a), _ver_tuple(b)
    n = max(len(ta), len(tb))
    ta = ta + (0,) * (n - len(ta)); tb = tb + (0,) * (n - len(tb))
    return ta > tb
def _fetch_latest_version_uncached():
    try:
        req = urllib.request.Request(UPDATE_VERSION_URL, headers={"User-Agent": "token-burn-tracker"})
        with urllib.request.urlopen(req, timeout=3) as r:
            v = r.read().decode("utf-8", "ignore").strip()
        return v or None
    except Exception:
        return None
def check_update():
    """Lazy, cached (~1h), fail-silent update check. Safe to call from a request handler."""
    cur = local_version()
    try:
        latest = _cached("update_latest_version", 3600, _fetch_latest_version_uncached)
    except Exception:
        latest = None
    outdated = bool(latest) and _version_newer(latest, cur)
    return {"current": cur, "latest": latest, "outdated": outdated, "cmd": UPDATE_INSTALL_CMD}
def force_check_update():
    """Same shape as check_update(), but bypasses the ~1h cache — used by the manual
    Rescan button so a user who just updated (or wants a fresh check) doesn't wait an hour.
    Fail-silent: network errors just mean 'no update known', never a 500."""
    cur = local_version()
    try:
        latest = _fetch_latest_version_uncached()
    except Exception:
        latest = None
    # keep the shared cache in sync so a subsequent /api/data (within the hour) reflects this fresh check too
    try:
        _LIVE_CACHE["update_latest_version"] = (time.time(), latest)
    except Exception:
        pass
    outdated = bool(latest) and _version_newer(latest, cur)
    return {"current": cur, "latest": latest, "outdated": outdated, "cmd": UPDATE_INSTALL_CMD}

# ---------- one-click self-update (download newest files, verify they compile, restart) ----------
UPDATE_RAW = "https://raw.githubusercontent.com/dluttz/token-burn-tracker/main"
def apply_update():
    """Download the newest app files into HERE and swap them in. tracker.py is only replaced after
    it passes a py_compile check, so a bad release can never brick the install. Returns (ok, message)."""
    import py_compile
    if not os.access(HERE, os.W_OK):
        return False, "This copy is in a read-only location; re-run the installer to update."
    staged = {}
    for rel in ("tracker.py", "tracker.html", "VERSION", "widget/index.jsx"):
        try:
            req = urllib.request.Request(UPDATE_RAW + "/" + rel, headers={"User-Agent": "token-burn-tracker"})
            with urllib.request.urlopen(req, timeout=20) as r:
                staged[rel] = r.read()
        except Exception as e:
            if rel == "widget/index.jsx":
                continue   # widget file is optional
            return False, "Couldn't download %s (%s)" % (rel, str(e)[:120])
    if not staged.get("tracker.py", b"").strip():
        return False, "Update download was empty; nothing changed."
    newpy = os.path.join(HERE, "tracker.py.new")
    try:
        with open(newpy, "wb") as f:
            f.write(staged["tracker.py"])
        py_compile.compile(newpy, doraise=True)   # verify BEFORE replacing anything
    except Exception as e:
        try: os.remove(newpy)
        except Exception: pass
        return False, "New version failed a safety check and was not applied (%s)." % (str(e)[:120])
    try:
        for rel, data in staged.items():
            dest = os.path.join(HERE, *rel.split("/"))
            dd = os.path.dirname(dest)
            if dd and not os.path.isdir(dd):
                os.makedirs(dd, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
        try: os.remove(newpy)
        except Exception: pass
    except Exception as e:
        return False, "Couldn't write the update (%s)." % (str(e)[:120])
    return True, local_version()

def restart_self():
    """Relaunch so freshly-downloaded code takes effect. A detached helper waits for this process
    to exit (freeing the port), then starts a fresh tracker.py. Never touches the user's terminal."""
    ppid = os.getpid()
    here_q = HERE.replace('"', '\\"'); data_q = DATA_DIR.replace('"', '\\"')
    script = ('sleep 1.2; kill %d 2>/dev/null; sleep 0.8; '
              'cd "%s" && TOKENBURN_DATA_DIR="%s" nohup python3 tracker.py > server.log 2>&1 &') % (ppid, here_q, data_q)
    try:
        subprocess.Popen(["/bin/bash", "-lc", script], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def analytics_error(where, err):
    """Anonymous error report (never any chat content) so issues can be seen and fixed."""
    try:
        analytics_event("app_error", {"where": str(where)[:60],
                        "error": (type(err).__name__ + ": " + str(err))[:200],
                        "app_version": local_version(), "macos": (platform.mac_ver()[0] or "?"), "$os": "Mac OS X"})
    except Exception:
        pass

CACHE_FILE = os.path.join(DATA_DIR, ".cache.json")
CACHE_VERSION = 14   # bumped: per-file cache entries now carry {corr, tools} per session
PORT = int(os.environ.get("TRACKER_PORT", "8799"))
# Secret embedded in the served page; required on POST /api/fix|kill so only the page
# we served (same origin) can trigger an action. Persisted so an already-open tab keeps
# working across server restarts (fixes the "invalid token after restart" issue).
TOKEN_FILE = os.path.join(DATA_DIR, ".fixtoken")
def _init_token():
    try:
        t = open(TOKEN_FILE).read().strip()
        if t:
            return t
    except Exception:
        pass
    t = secrets.token_hex(16)
    try:
        open(TOKEN_FILE, "w").write(t); os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass
    return t
FIX_TOKEN = _init_token()

STATE = {"data": None, "loading": True, "error": None, "files": 0, "parsed": 0}
BUILD_LOCK = threading.Lock()
SERIES_CACHE = {}  # range -> (computed_at, data)
_LIVE_CACHE = {}   # key -> (computed_at, value), keeps the live poll cheap
def _cached(key, ttl, fn):
    now = time.time(); c = _LIVE_CACHE.get(key)
    if c and now - c[0] < ttl:
        return c[1]
    v = fn(); _LIVE_CACHE[key] = (now, v); return v

# A correction is a message that pushes back on what just came out. Anthropic publishes the only
# vendor-backed threshold in this area, in the Claude Code best practices: "If you've corrected
# Claude more than twice on the same issue in one session, the context is cluttered with failed
# approaches. Run /clear and start fresh." Three is therefore the line the board draws.
#
# This is deliberately a keyword matcher over the OPENING of a user message, not a classifier.
# Published work on detecting frustration in deployed assistants (COLING 2025 Industry Track,
# arXiv:2411.17437) found keyword matching scores near-perfect precision and about 1% recall,
# because "poor conversation handling does not always manifest as overtly negative language".
# So this undercounts by design. A chat it flags is almost certainly in trouble; a chat it does
# not flag is not therefore fine. The Cached and Share columns say what a chat costs; this one
# only ever says "this one went round in circles", and only when it is obvious.
_CORRECTION_PAT = re.compile(
    r"^\s*(?:no+[,.!\s]|nope\b|wrong\b|that'?s (?:not|wrong|incorrect)|not what i|"
    r"i (?:said|asked|told you|already said|meant)|you (?:didn'?t|did not|still|keep|forgot|missed|ignored)|"
    r"still (?:not|doesn'?t|isn'?t|broken|failing|wrong)|again[,.!\s]|"
    r"that (?:didn'?t|does not|doesn'?t) work|it'?s still|undo\b|revert\b|go back\b|"
    r"stop\b|why did you|read (?:the|what i))", re.I)

def is_correction(text):
    """True when a user message opens by pushing back. Checks the opening only: a correction
    leads with the objection, while a long message that happens to contain the word "no" is
    usually a fresh instruction."""
    if not text:
        return False
    t = " ".join(str(text).split())
    if not t or t.startswith("<") or t.startswith("/"):   # tool tags and slash commands are not corrections
        return False
    return bool(_CORRECTION_PAT.search(t[:120]))

def user_text(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        for it in c:
            if isinstance(it, dict) and it.get("type") == "text" and it.get("text"):
                return it["text"]
    return None

def _ts_date(v):
    """Any timestamp (ISO string or epoch s/ms) -> local 'YYYY-MM-DD', or None."""
    t = parse_ts(v)
    if t is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(t).date().isoformat()
    except Exception:
        return None

# ---------- parsers -> (entries[[date,tool,model,project,tokens,sessionKey,filePath]], titles{sk:title}) ----------
def _clean_title(t):
    """Sanitize first-prompt fallback titles: the desktop app can append tag blocks like
    <system-reminder>… to the user's message, which must never show up as a chat title.
    Drops those blocks (including ones cut off by the 160-char cap) and tidies whitespace."""
    t = re.sub(r"(?is)<(system[-_ ]?reminder|uploaded_files|command-message)\b.*?(</\1>|$)", " ", t)
    # partial known-noise tag cut off by the length cap (e.g. "…<syst"); bare '<' in prose is left alone
    t = re.sub(r"(?is)\s*</?(syst|upload|command|antml)[^>]*$", "", t)
    return " ".join(t.split())[:160].strip()

def _empty_breakdown():
    return {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}

def claude_entries(path, tool, file_date):
    entries, titles, corr = [], {}, defaultdict(int)
    tools = defaultdict(lambda: defaultdict(int))   # session -> pretty tool name -> calls
    tb = _empty_breakdown()   # input/cache/output split, summed across every usage record in this file
    last_ts = None
    file_sid = None
    default_cwd = "Cowork sessions" if tool == "Cowork" else "?"
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                is_usage = '"usage"' in line
                is_user = ('"role": "user"' in line) or ('"role":"user"' in line)
                is_summary = '"summary"' in line
                if not is_usage and not is_user and not is_summary:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if file_sid is None and d.get("sessionId"):
                    file_sid = d.get("sessionId")
                if is_summary and not is_usage and not is_user:
                    # Claude Code writes a session-summary line; use it as the title (better than first message).
                    s = d.get("summary")
                    if isinstance(s, str) and s.strip():
                        titles[session_key(tool, d.get("sessionId") or file_sid, path)] = " ".join(s.split())[:160]
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                sk = session_key(tool, d.get("sessionId"), path)
                if msg.get("role") == "user":
                    t = user_text(msg)
                    if t:
                        ct = _clean_title(t)
                        if ct:   # don't let a tag-only message (e.g. a scheduled run) claim the title slot
                            titles.setdefault(sk, ct)
                        if is_correction(t):
                            corr[sk] += 1
                    continue
                # Tool calls ride along on assistant messages, which already carry usage, so
                # counting them here costs one pass over content that is in hand and no extra
                # file reads. This is what fills the tool strip on the leak board.
                if isinstance(msg.get("content"), list):
                    for _it in msg["content"]:
                        if isinstance(_it, dict) and _it.get("type") == "tool_use":
                            tools[sk][_pretty_tool(_it.get("name"))] += 1
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                tk = ((u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
                      + (u.get("cache_creation_input_tokens") or 0)
                      + (u.get("cache_read_input_tokens") or 0))
                if tk <= 0:
                    continue
                tb["input"] += u.get("input_tokens") or 0
                tb["cache_write"] += u.get("cache_creation_input_tokens") or 0
                tb["cache_read"] += u.get("cache_read_input_tokens") or 0
                tb["output"] += u.get("output_tokens") or 0
                # Claude Code uses "timestamp"; Cowork uses "_audit_timestamp".
                ts = d.get("timestamp") or d.get("_audit_timestamp") or msg.get("timestamp")
                if ts:
                    last_ts = ts
                date = _ts_date(ts) or _ts_date(last_ts) or file_date
                slug = d.get("slug")
                if slug:
                    titles.setdefault(sk, str(slug).replace("-", " "))
                entries.append([date, tool, msg.get("model") or "?", d.get("cwd") or default_cwd, tk, sk, path,
                                [u.get("input_tokens") or 0, u.get("cache_creation_input_tokens") or 0,
                                 u.get("cache_read_input_tokens") or 0, u.get("output_tokens") or 0]])
    except Exception:
        pass
    return entries, titles, tb, {"corr": dict(corr),
                                "tools": {k: dict(v) for k, v in tools.items()}}

def codex_entries(path, file_date, index_map):
    """Codex rollout logs carry CUMULATIVE counters per token_count event:
    total_tokens, input_tokens, cached_input_tokens, output_tokens. The deltas
    between consecutive events are that turn's real usage, so newer Codex
    sessions get a measured split (fresh input / cache read / output) and
    join the leak board. Older logs that only carry total_tokens still fall
    back to unsplit per-day totals, reported as unmeasured, never guessed."""
    cwd = model = first_ts = sid = None
    prev = None                       # (total, input, cached, output) cumulative — non-total fields may be None
    turns = []                        # (date, delta_total, split-or-None) per token_count event
    unsplit_by_date = defaultdict(int)
    try:
        with open(path, errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 0 and ('token_count' not in line and 'session_meta' not in line
                              and '"model"' not in line and '"cwd"' not in line):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if first_ts is None:
                    first_ts = d.get("timestamp")
                p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
                if not sid and p.get("id"):
                    sid = p.get("id")
                if not cwd and p.get("cwd"):
                    cwd = p.get("cwd")
                if not model:
                    model = p.get("model") or d.get("model")
                if p.get("type") == "token_count" and isinstance(p.get("info"), dict):
                    u = p["info"].get("total_token_usage") or {}
                    tt = u.get("total_tokens")
                    if not isinstance(tt, (int, float)):
                        continue
                    ti, tc, to = u.get("input_tokens"), u.get("cached_input_tokens"), u.get("output_tokens")
                    have = all(isinstance(x, (int, float)) for x in (ti, tc, to))
                    ev_date = _ts_date(d.get("timestamp")) or _ts_date(first_ts) or file_date
                    if prev is None:
                        if have:                       # session's first reading = turn 1
                            fresh = max(0, int(ti) - int(tc))
                            turns.append((ev_date, int(tt), (fresh, 0, int(tc), int(to))))
                        else:
                            unsplit_by_date[ev_date] += int(tt)
                        prev = (tt, ti if have else None, tc if have else None, to if have else None)
                    elif tt > prev[0]:
                        dt_tok = int(tt - prev[0])
                        if have and prev[1] is not None:
                            di, dc, do = int(ti - prev[1]), int(tc - prev[2]), int(to - prev[3])
                            if di >= 0 and dc >= 0 and do >= 0:
                                turns.append((ev_date, dt_tok, (max(0, di - dc), 0, dc, do)))
                            else:                      # a counter went backwards: do not guess
                                unsplit_by_date[ev_date] += dt_tok
                        else:
                            unsplit_by_date[ev_date] += dt_tok
                        prev = (tt, ti if have else prev[1], tc if have else prev[2], to if have else prev[3])
                    elif tt < prev[0]:
                        # the counter RESET (compaction / a new sub-session). Re-baseline and
                        # count the fresh segment's opening reading like a first event — the old
                        # code kept waiting for the counter to re-pass its old maximum and
                        # silently dropped everything in between (9.2M real tokens in one
                        # session on this machine).
                        if have:
                            fresh = max(0, int(ti) - int(tc))
                            turns.append((ev_date, int(tt), (fresh, 0, int(tc), int(to))))
                        else:
                            unsplit_by_date[ev_date] += int(tt)
                        prev = (tt, ti if have else None, tc if have else None, to if have else None)
    except Exception:
        pass
    total = sum(t[1] for t in turns) + sum(unsplit_by_date.values())
    if total > 0:
        sk = "Codex:" + (sid or os.path.basename(path))
        ents = [[dt, "Codex", model or "codex", cwd or "?", tok, sk, path, list(split)]
                for dt, tok, split in turns if tok > 0]
        ents += [[dt, "Codex", model or "codex", cwd or "?", int(tok), sk, path]
                 for dt, tok in unsplit_by_date.items() if tok > 0]
        tb = _empty_breakdown()      # the splits feed the file-level breakdown too, so
        for _dt, _tok, sp in turns:  # Today's cache-read figure agrees with the Models cut
            tb["input"] += sp[0]; tb["cache_write"] += sp[1]
            tb["cache_read"] += sp[2]; tb["output"] += sp[3]
        return ents, {sk: index_map.get(sid or "", "Codex session")}, tb, {"corr": {}, "tools": {}}
    return [], {}, _empty_breakdown(), {"corr": {}, "tools": {}}

def _sqlite_title_map(db):
    """Best-effort id -> chat title from an unknown SQLite schema (newer Codex keeps titles in logs_*.sqlite)."""
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=1.0)
    except Exception:
        return out
    try:
        cur = con.cursor()
        tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'").fetchall()]
        RANK = ["thread_name", "title", "summary", "name", "label"]   # prefer clearly-a-title columns
        for t in tables:
            try:
                cols = [c[1] for c in cur.execute('PRAGMA table_info("%s")' % t).fetchall()]
            except Exception:
                continue
            id_cols = [c for c in cols if re.search(r'(^|_)(id|uuid)$', c, re.I)
                       or re.search(r'(session|conversation|thread|rollout)', c, re.I)]
            title_cols = sorted([c for c in cols if c.lower() in RANK], key=lambda c: RANK.index(c.lower()))
            if not id_cols or not title_cols:
                continue
            sel = ",".join('"%s"' % c for c in (id_cols + title_cols))
            try:
                rows = cur.execute('select %s from "%s"' % (sel, t)).fetchall()
            except Exception:
                continue
            ni = len(id_cols)
            for row in rows:
                ids = [str(x) for x in row[:ni] if x not in (None, "")]
                titles = [str(x).strip() for x in row[ni:] if isinstance(x, str) and x.strip()]
                if ids and titles:
                    for i in ids:
                        out.setdefault(i, titles[0][:200])
    except Exception:
        pass
    finally:
        try: con.close()
        except Exception: pass
    return out

def load_codex_index():
    m = {}
    try:
        with open(HOME + "/.codex/session_index.jsonl", errors="ignore") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("id") and d.get("thread_name"):
                    m[d["id"]] = d["thread_name"]
    except Exception:
        pass
    try:   # newer Codex stores chat titles in a SQLite db instead of session_index.jsonl
        for db in sorted(glob.glob(HOME + "/.codex/*.sqlite")):
            for k, v in _sqlite_title_map(db).items():
                m.setdefault(k, v)
    except Exception:
        pass
    return m

# ---------- custom (user-added) sources ----------
CUSTOM_FILE = os.path.join(DATA_DIR, "custom_sources.json")
BASE_TOOL_COLORS = {"Claude Code": "#d4663a", "Cowork": "#6e56cf", "Codex": "#10a37f", "Ollama": "#0ea5e9"}
_PALETTE = ["#e08a2b", "#db2777", "#65a30d", "#7c3aed", "#0891b2", "#b45309", "#be123c"]

def load_custom_sources():
    try:
        d = json.load(open(CUSTOM_FILE))
        return d if isinstance(d, list) else []
    except Exception:
        return []

def save_custom_sources(lst):
    try:
        json.dump(lst, open(CUSTOM_FILE, "w"), indent=2); return True
    except Exception:
        return False

def tool_colors():
    colors = dict(BASE_TOOL_COLORS)
    i = 0
    for s in load_custom_sources():
        nm = s.get("name")
        if not nm:
            continue
        colors[nm] = s.get("color") or _PALETTE[i % len(_PALETTE)]; i += 1
    return colors

def _find_token_sum(obj, keys, depth=0):
    """Recursively sum numeric values stored under any of `keys` anywhere in the object."""
    total = 0
    if depth > 6 or not isinstance(obj, (dict, list)):
        return 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (int, float)) and not isinstance(v, bool):
                total += v
            else:
                total += _find_token_sum(v, keys, depth + 1)
    else:
        for v in obj:
            total += _find_token_sum(v, keys, depth + 1)
    return total

def _find_first(obj, keys, depth=0):
    if depth > 6 or not isinstance(obj, (dict, list)):
        return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and not isinstance(obj[k], (dict, list)):
                return obj[k]
        for v in obj.values():
            r = _find_first(v, keys, depth + 1)
            if r is not None:
                return r
    else:
        for v in obj:
            r = _find_first(v, keys, depth + 1)
            if r is not None:
                return r
    return None

def custom_entries(path, src, file_date):
    name = src.get("name") or "Custom"
    tkeys = set(src.get("tokenKeys") or [])
    tskeys = src.get("tsKeys") or ["timestamp", "_audit_timestamp", "created_at", "time", "ts"]
    if not tkeys:
        return [], {}, _empty_breakdown(), {"corr": {}, "tools": {}}
    sk = name + ":" + os.path.basename(path)
    proj = shorten(os.path.dirname(path)) if "/" in path else name
    ttlkeys = set(src.get("titleKeys") or ["title", "name", "thread_name", "summary", "subject"])
    ctitle = None
    by_date = defaultdict(int)
    try:
        with open(path, errors="ignore") as f:
            for line in deque(f, maxlen=20000):
                line = line.strip()
                if not line:
                    continue
                has_tok = any(k in line for k in tkeys)
                has_ttl = ctitle is None and any(k in line for k in ttlkeys)
                if not has_tok and not has_ttl:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if has_ttl:
                    tv = _find_first(d, ttlkeys)
                    if tv is not None and str(tv).strip():
                        ctitle = " ".join(str(tv).split())[:160]
                if has_tok:
                    tok = _find_token_sum(d, tkeys)
                    if tok <= 0:
                        continue
                    dt = _ts_date(_find_first(d, tskeys)) or file_date
                    by_date[dt] += tok
    except Exception:
        pass
    ents = [[dt, name, src.get("model") or "?", proj, int(tok), sk, path] for dt, tok in by_date.items() if tok > 0]
    # Custom sources only declare which field(s) hold a token count, not which kind
    # (input/cache/output), so they don't contribute to the token breakdown split.
    return ents, ({sk: (ctitle or os.path.basename(path))} if ents else {}), _empty_breakdown(), {"corr": {}, "tools": {}}

# ---------- build ----------
def _gather_files_uncached():
    files = [("claude", p) for p in glob.glob(HOME + "/.claude/projects/**/*.jsonl", recursive=True)]
    cw = HOME + "/Library/Application Support/Claude/local-agent-mode-sessions"
    files += [("cowork", p) for p in glob.glob(cw + "/**/*.jsonl", recursive=True)]
    files += [("codex", p) for p in glob.glob(HOME + "/.codex/sessions/**/rollout-*.jsonl", recursive=True)]
    return files

def gather_files():
    # globbing thousands of files repeatedly is wasteful; reuse for a few seconds
    return _cached("gather_files", 15, _gather_files_uncached)

def build():
    if not BUILD_LOCK.acquire(blocking=False):
        return  # another build is already running; skip (it will refresh STATE + clear loading)
    try:
        cache = {}
        if os.path.exists(CACHE_FILE):
            try:
                cache = json.load(open(CACHE_FILE))
                if cache.get("_v") != CACHE_VERSION:
                    cache = {}
            except Exception:
                cache = {}
        index_map = load_codex_index()
        files = gather_files()
        STATE["files"] = len(files)
        STATE["parsed"] = 0
        newcache = {"_v": CACHE_VERSION}
        entries, titles, parsed = [], {}, 0
        corrections = defaultdict(int)   # session key -> times the user pushed back
        sess_tools = defaultdict(lambda: defaultdict(int))   # session key -> tool -> calls
        tokenBreakdown = _empty_breakdown()   # input/cache/output totals across every parsed record
        kind_files = defaultdict(int); kind_tokens = defaultdict(int)
        for kind, path in files:
            kind_files[kind] += 1
            try:
                mt = os.path.getmtime(path)
            except OSError:
                continue
            c = cache.get(path)
            if c and c.get("mtime") == mt and "tb" in c:
                ents, tts, tb = c["entries"], c.get("titles", {}), c.get("tb", _empty_breakdown())
                cr = c.get("corr", {})
            else:
                fdate = datetime.date.fromtimestamp(mt).isoformat()
                # One unreadable or unexpected file must never empty the whole dashboard. A
                # three-value return slipped through here once and the scan finished reporting
                # zero tokens, which looks exactly like "you have never used any of these tools".
                # Skipping the file and carrying on is always the better failure.
                try:
                    if kind == "claude":
                        ents, tts, tb, cr = claude_entries(path, "Claude Code", fdate)
                    elif kind == "cowork":
                        ents, tts, tb, cr = claude_entries(path, "Cowork", fdate)
                    else:
                        ents, tts, tb, cr = codex_entries(path, fdate, index_map)
                except Exception:
                    ents, tts, tb, cr = [], {}, _empty_breakdown(), {"corr": {}, "tools": {}}
                    STATE["skippedFiles"] = STATE.get("skippedFiles", 0) + 1
                parsed += 1
                STATE["parsed"] = parsed
            newcache[path] = {"mtime": mt, "entries": ents, "titles": tts, "tb": tb, "corr": cr}
            for _sk, _n in ((cr or {}).get("corr") or {}).items():
                corrections[_sk] += _n
            for _sk, _tm in ((cr or {}).get("tools") or {}).items():
                for _tn, _tc in _tm.items():
                    sess_tools[_sk][_tn] += _tc
            entries.extend(ents)
            kind_tokens[kind] += sum(e[4] for e in ents)
            for k, v in tts.items():
                titles.setdefault(k, v)
            for k in tokenBreakdown:
                tokenBreakdown[k] += tb.get(k, 0)
        # user-added custom token sources (manual "add a tool to track")
        custom = load_custom_sources()
        custom_health = []
        for src in custom:
            nm = src.get("name"); g = src.get("glob")
            if not nm or not g or not src.get("tokenKeys"):
                continue
            nfiles = ntok = 0
            for path in glob.glob(os.path.expanduser(g), recursive=True):
                nfiles += 1
                try:
                    mt = os.path.getmtime(path)
                except OSError:
                    continue
                ck = "custom::" + nm + "::" + path
                c = cache.get(ck)
                if c and c.get("mtime") == mt and "tb" in c:
                    ents, tts, tb = c["entries"], c.get("titles", {}), c.get("tb", _empty_breakdown())
                else:
                    fdate = datetime.date.fromtimestamp(mt).isoformat()
                    try:
                        ents, tts, tb, _cr = custom_entries(path, src, fdate)
                    except Exception:
                        ents, tts, tb = [], {}, _empty_breakdown()
                        STATE["skippedFiles"] = STATE.get("skippedFiles", 0) + 1
                    parsed += 1; STATE["parsed"] = parsed
                newcache[ck] = {"mtime": mt, "entries": ents, "titles": tts, "tb": tb}
                entries.extend(ents); ntok += sum(e[4] for e in ents)
                for k, v in tts.items():
                    titles.setdefault(k, v)
                for k in tokenBreakdown:
                    tokenBreakdown[k] += tb.get(k, 0)
            custom_health.append((nm, nfiles, ntok))
        try:
            json.dump(newcache, open(CACHE_FILE, "w"))
        except Exception:
            pass
        d = aggregate(entries, titles, dict(corrections),
                      {k: dict(v) for k, v in sess_tools.items()})
        d["tokenBreakdown"] = tokenBreakdown
        # self-check: a source with log files but zero parsed tokens likely means its format changed
        warn = []
        for k, nm in (("claude", "Claude Code"), ("cowork", "Cowork"), ("codex", "Codex")):
            if kind_files.get(k, 0) > 0 and kind_tokens.get(k, 0) == 0:
                warn.append(f"Found {kind_files[k]} {nm} log file(s) but couldn't read any tokens — {nm}'s log format may have changed.")
        for nm, nf, nt in custom_health:
            if nf == 0:
                warn.append(f"Custom tool “{nm}”: no files matched its log pattern yet.")
            elif nt == 0:
                warn.append(f"Custom tool “{nm}”: found {nf} file(s) but no tokens — check the token field name(s).")
        d["warnings"] = warn
        d["toolColors"] = tool_colors()
        d["customSources"] = [{"name": s.get("name"), "glob": s.get("glob"),
                               "tokenKeys": s.get("tokenKeys"), "process": s.get("process")} for s in custom]
        try:
            d["insights"] = build_insights(d)
        except Exception:
            d["insights"] = {"suggestions": [], "waste": []}
        STATE["data"] = d
        STATE["loading"] = False
        analytics_usage(d)   # anonymous, content-free usage snapshot, once per launch
    except Exception as e:
        STATE["error"] = str(e) + "\n" + traceback.format_exc()
        STATE["loading"] = False
        analytics_error("build", e)   # anonymous: error type + version only, never chat content
    finally:
        try: BUILD_LOCK.release()
        except Exception: pass

def shorten(p):
    if not p or p == "?":
        return "(unknown)"
    if "/" not in p:
        return p
    parts = [x for x in p.rstrip("/").split("/") if x]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else p)

# ---- real chat titles + start times, read from each session's own metadata JSON ----
# The desktop app stores a per-chat metadata file (local_<uuid>.json) with the exact title it shows in
# its sidebar, plus createdAt/lastActivityAt. Reading it lets flagged chats show their real name + true
# start time — matched to a session by the local_<uuid> in its transcript path.
_AGENT_META = {"idx": {}, "files": {}, "ts": 0.0}
def load_agent_meta():
    """Refresh the title index. The glob still walks the tree, but a per-file
    mtime cache means unchanged metadata files are not re-read — in steady
    state that is ~100 stat calls instead of ~100 JSON parses every refresh.
    A rename rewrites its local_<id>.json, so the mtime bump catches it."""
    idx = {}
    files = _AGENT_META["files"]
    seen = set()
    base = HOME + "/Library/Application Support/Claude/local-agent-mode-sessions"
    for mp in glob.glob(base + "/**/local_*.json", recursive=True):
        # Key by the filename stem. A hex-only pattern here missed named sessions
        # like local_ditto_<uuid>.json, so their real sidebar titles never resolved.
        name = os.path.basename(mp)[:-5]
        seen.add(name)
        try:
            mt = os.stat(mp).st_mtime
        except OSError:
            continue
        cached = files.get(name)
        if cached and cached[0] == mt:
            idx[name] = cached[1]
            continue
        try:
            d = json.load(open(mp, errors="ignore"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        entry = {"title": (d.get("title") or "").strip(),
                 "created": d.get("createdAt"), "last": d.get("lastActivityAt")}
        files[name] = (mt, entry)
        idx[name] = entry
    for k in list(files):        # forget sessions whose metadata file is gone
        if k not in seen:
            files.pop(k, None)
    return idx
def agent_meta():
    now = time.time()
    if now - _AGENT_META["ts"] > 8 or not _AGENT_META["idx"]:
        try:
            _AGENT_META["idx"] = load_agent_meta() or _AGENT_META["idx"]
        except Exception:
            pass
        _AGENT_META["ts"] = now
    return _AGENT_META["idx"]
def agent_meta_for(path):
    # A chat's transcript lives inside a folder named exactly like its metadata
    # JSON's stem (…/local_<id>/…/audit.jsonl beside local_<id>.json), so match
    # whole path segments, innermost first. The old hex-only regex extracted the
    # bogus key "local_d" from local_ditto_<uuid> paths and the lookup missed.
    idx = agent_meta()
    for seg in reversed((path or "").split("/")):
        if seg.startswith("local_") and seg in idx:
            return idx[seg]
    return None

def _fresh_session_titles(d):
    """bySession and leaks are baked at scan time, but the app writes/renames chat titles
    asynchronously — so a chat can get its real sidebar title AFTER we scanned it. Re-resolve from
    the live metadata index at serve time (the same source the Agents view uses, which is why that
    view was always right). Cheap: agent_meta() is cached ~8s and this touches a few dozen rows."""
    try:
        for row in (d or {}).get("bySession") or []:
            if len(row) >= 5 and row[4]:
                m = agent_meta_for(row[4])
                if m and m.get("title"):
                    row[0] = m["title"]
    except Exception:
        pass
    try:   # same treatment for Token Leaks, or its titles go stale while bySession's stay fresh
        for _w in ((d or {}).get("leaks") or {}).values():
            for c in (_w or {}).get("chats") or []:
                m = agent_meta_for(c.get("file"))
                if m and m.get("title"):
                    c["title"] = m["title"]; c["titleSource"] = "app"
    except Exception:
        pass
    return d

# ---------- Token Leaks (v2.4.0) ----------
# "Re-sent text" is cache_read: the conversation so far, sent again on every single turn. For most
# people it dwarfs everything they actually typed, and unlike a prompt it is invisible. Everything
# below is derived from entries already parsed for the dashboard — no extra scanning, no new files.
# Only sources that report a token split (Claude Code / Cowork) can be measured. Codex and custom
# sources expose totals only, so their tokens are reported as unmeasured rather than guessed at.

def _pctile(vals, p):
    """p in 0..1 over an already-sorted list."""
    if not vals:
        return 0
    return vals[min(len(vals) - 1, int(round((len(vals) - 1) * p)))]

SPLIT_TARGET_TURNS = 50   # the chunk size the saving estimate assumes. A declared constant, not
                          # a statistic derived from your own data: a number you can argue with
                          # beats a median you cannot see.

def _split_savings(resent, turns, target_turns=SPLIT_TARGET_TURNS):
    """Re-sent text grows with the SQUARE of turn count — turn N re-sends N turns of history — so
    splitting a chat into k parts cuts the total to roughly 1/k. k is how many chunks of
    target_turns this chat would become. Capped at 90% so the figure can never overpromise.

    This used to divide by the median chat's turn count, which made the same chat show different
    savings in different windows and hid the assumption inside a statistic. A fixed, stated
    chunk size is both stable and arguable."""
    if resent <= 0 or turns <= 0 or target_turns <= 0:
        return 0
    k = turns / float(target_turns)
    if k <= 1:
        return 0
    return int(min(resent * 0.90, resent - (resent / k)))

def _cache_hit(read, write, fresh):
    """Cache hit ratio: of everything the model had to read on the way in, what share came from
    cache rather than being paid for at the full input rate.

        hit = cache_read / (cache_read + cache_write + fresh_input)

    This is the metric Anthropic, OpenAI, Langfuse, Braintrust and Arize all expose. Cache reads
    bill at roughly a tenth of fresh input, so a chat re-sending an enormous but well-cached
    history is cheap, while a short chat that keeps missing cache is expensive. High is good here,
    unlike every other number on the page. Returns None when there is nothing to divide, so the UI
    can say "not measured" rather than print a confident 0%."""
    denom = (read or 0) + (write or 0) + (fresh or 0)
    if denom <= 0:
        return None
    return round(100.0 * (read or 0) / denom, 1)

def build_leaks(sess_resent, sess_turns, sess_startup, sess_proj, sess_date, resolve,
                unmeasured=0, top=8, since=None, window="all",
                top_share_n=5, sess_fresh=None, sess_cachew=None, corrections=None,
                sess_tools=None):
    """One window of the leak board. `since` is an ISO date; a chat belongs to the window if it was
    last ACTIVE in it. Windowing by activity (not by slicing turns) is what lets the board show
    improvement: split a monster chat today and it ages out of the 7- and 30-day views, while the
    shorter chats that replaced it show up small. Without that, a past monster would sit at the top
    forever and nothing you did would ever look better."""
    # A chat needs a back-and-forth to leak: a single-turn chat has no history to re-send.
    keys = [sk for sk in sess_resent
            if sess_turns.get(sk, 0) >= 2 and (not since or (sess_date.get(sk) or "") >= since)]
    empty = {"window": window, "since": since, "chats": [],
             "startup": [], "measured": 0, "totalResent": 0, "topShare": 0, "topN": 0,
             "cacheHit": None, "freshInput": 0, "cacheWrite": 0,
             "splitTarget": SPLIT_TARGET_TURNS,
             "gauge": {"p75": 0, "p90": 0}, "unmeasuredTokens": unmeasured}
    if not keys:
        return empty
    resents = sorted(sess_resent[sk] for sk in keys)
    # There is deliberately no "typical chat" here any more. Dividing a chat's re-sent tokens by
    # the median chat's produced a badge that mostly measured LENGTH: re-sent text grows with
    # roughly the square of a conversation's length, so the longest chat wins the badge even when
    # each of its turns is carrying less history than a short one. On this machine the chat with
    # the most turns of any carried 30% LESS history per turn than the median and still ranked
    # fifth worst. The board now reports share of the window, cost efficiency (cache hit) and
    # reply bloat, all of which mean the same thing regardless of how long a chat ran.
    chats = []
    today = datetime.date.today().isoformat()
    for sk in sorted(keys, key=lambda k: -sess_resent[k])[:top]:
        r, n = sess_resent[sk], sess_turns[sk]
        title, proj, tool, fpath, date, when, src = resolve(sk)
        # titleSource tells the UI whether this is the app's own sidebar name or our sanitized
        # first-message fallback. Only Cowork chats carry app titles; Claude Code and Codex have
        # no equivalent, so those rows should be labelled by tool + project + date, never by
        # dumping raw prompt text that the user cannot match to anything.
        chats.append({"title": title, "titleSource": src, "project": proj, "tool": tool,
                      "file": fpath, "date": date, "when": when, "lastActive": date,
                      "active": bool(date) and date >= today,
                      "resent": r, "turns": n,
                      "perTurn": int(r / max(1, n - 1)),
                      "cacheHit": _cache_hit(r, (sess_cachew or {}).get(sk, 0),
                                             (sess_fresh or {}).get(sk, 0)),
                      "corrections": int((corrections or {}).get(sk, 0)),
                      "tools": sorted(((sess_tools or {}).get(sk) or {}).items(),
                                      key=lambda kv: -kv[1])[:14],
                      "saves": _split_savings(r, n)})
    # Entry fee: what a chat costs before the first word — the tool's own instructions, CLAUDE.md,
    # and one description per connected tool. Median (not mean) so a single big opening read can't
    # skew it, and only for projects with enough chats to be fair.
    byproj = defaultdict(list)
    for sk in keys:
        s0 = sess_startup.get(sk)
        if s0:
            byproj[sess_proj.get(sk, "(unknown)")].append(s0)
    startup = []
    for pj, vals in byproj.items():
        if len(vals) < 3:
            continue
        vals.sort()
        med = _pctile(vals, 0.5)
        startup.append({"project": pj, "median": med, "chats": len(vals), "paid": med * len(vals)})
    startup.sort(key=lambda x: -x["paid"])
    total = sum(resents)
    # topShare is the share held by the heaviest `top_share_n`, which is deliberately smaller than
    # the `top` rows we list: the point of the sentence is that a handful of chats dominate, and
    # quoting it over every listed row would say "8 of our 11 chats are 99.8%", which is nothing.
    # topN ships with it so the copy can never again name a different count than the maths used.
    # topShare only says something when the window holds meaningfully more chats than it names.
    # With 6 measured chats "the heaviest 5 are 99.9%" is arithmetic, not insight, so it is
    # suppressed rather than printed as though it were a finding.
    n_share = max(1, min(int(top_share_n), len(chats)))
    if len(keys) < n_share * 2:
        n_share = 0
    _w_read  = sum(sess_resent[sk] for sk in keys)
    _w_write = sum((sess_cachew or {}).get(sk, 0) for sk in keys)
    _w_fresh = sum((sess_fresh  or {}).get(sk, 0) for sk in keys)
    for c in chats:
        c["share"] = round(100.0 * c["resent"] / total, 1) if total else None
    return {"window": window, "since": since, "splitTarget": SPLIT_TARGET_TURNS,
            "cacheHit": _cache_hit(_w_read, _w_write, _w_fresh),
            "freshInput": _w_fresh, "cacheWrite": _w_write,
            "chats": chats,
            "startup": startup[:8], "measured": len(keys), "totalResent": total,
            "topN": n_share,
            "topShare": (round(100.0 * sum(c["resent"] for c in chats[:n_share]) / total, 1)
                         if (total and n_share) else None),
            "gauge": {"p75": _pctile(resents, 0.75), "p90": _pctile(resents, 0.90)},
            "unmeasuredTokens": unmeasured}

def aggregate(entries, titles, corrections=None, sess_tools=None):
    day = defaultdict(lambda: defaultdict(int))
    day_total = defaultdict(int)
    day_proj = defaultdict(lambda: defaultdict(int))
    proj = defaultdict(int)
    proj_tool = defaultdict(lambda: defaultdict(int))
    proj_path = {}   # shortened name -> representative real absolute cwd
    model = defaultdict(int)
    tool = defaultdict(int)
    sess = defaultdict(int)
    sess_meta = {}
    sess_turns = defaultdict(int)    # assistant turns that reported a token split
    sess_resent = defaultdict(int)   # cache_read per session = the conversation re-sent
    sess_fresh  = defaultdict(int)   # fresh input tokens: text the cache did NOT cover
    sess_cachew = defaultdict(int)   # cache writes: paying to put something INTO the cache
    sess_startup = {}                # that session's FIRST turn: the entry fee before you type
    sess_proj = {}
    unmeasured = 0                   # tokens from sources with no split (Codex / custom)
    sess_file = {}   # session key -> first source .jsonl path seen for it (for transcript links)
    sess_paths = defaultdict(set)   # session key -> ALL transcript files, so the real title resolves even if the first file misses
    sess_date = {}   # session key -> latest activity date (to find the chat in the app's date-grouped list)
    grand = 0
    usage_model = defaultdict(lambda: [0, 0, 0, 0, 0])                      # input, cache_write, cache_read, output, unsplit
    usage_day_model = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, 0]))
    usage_tool_model = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, 0]))
    for e in entries:
        date, tl, md, cwd, tk, sk, fpath = e[:7]
        split = e[7] if len(e) > 7 and isinstance(e[7], (list, tuple)) and len(e[7]) == 4 else None
        if not date or len(date) < 10:
            date = "unknown"
        day[date][tl] += tk
        day_total[date] += tk
        sp = shorten(cwd)
        proj[sp] += tk
        proj_tool[sp][tl] += tk
        if sp not in proj_path and isinstance(cwd, str) and cwd.startswith("/"):
            proj_path[sp] = cwd
        day_proj[date][sp] += tk
        model[md] += tk
        tool[tl] += tk
        sess[sk] += tk
        if sk not in sess_meta:
            sess_meta[sk] = (tl, sp)
        if fpath:
            sess_file.setdefault(sk, fpath)
            sess_paths[sk].add(fpath)
        if date != "unknown" and date > sess_date.get(sk, ""):
            sess_date[sk] = date
        um, udm, utm = usage_model[md], usage_day_model[date][md], usage_tool_model[tl][md]
        if split:
            for i in range(4):
                um[i] += split[i]; udm[i] += split[i]; utm[i] += split[i]
            sess_turns[sk] += 1
            sess_resent[sk] += split[2]
            sess_fresh[sk]  += split[0]
            sess_cachew[sk] += split[1]
            if sk not in sess_startup:          # entries arrive in file order, so this is turn 1
                sess_startup[sk] = split[1] or split[0]
            sess_proj.setdefault(sk, sp)
        else:   # tools that only expose totals (Codex, custom sources)
            um[4] += tk; udm[4] += tk; utm[4] += tk
            unmeasured += tk
        grand += tk
    today = datetime.date.today().isoformat()
    weekago = (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
    week = sum(v for d, v in day_total.items() if d != "unknown" and d >= weekago)
    days = []
    for d in sorted(k for k in day_total if k != "unknown"):
        tp = sorted(day_proj[d].items(), key=lambda x: -x[1])[:4]
        days.append({"date": d, "total": day_total[d], "byTool": dict(day[d]),
                     "topProjects": [[p, v] for p, v in tp]})
    byProject = [[p, v, dict(proj_tool[p])] for p, v in sorted(proj.items(), key=lambda x: -x[1])[:25]]
    byModel = sorted(model.items(), key=lambda x: -x[1])[:12]
    def _resolve(sk):
        """Real chat title + start time for one session key. Shared by bySession and Token Leaks."""
        tl, sp = sess_meta.get(sk, ("?", "?"))
        fp = sess_file.get(sk, "")
        meta = agent_meta_for(fp)   # real sidebar title + start time from the app's own per-chat metadata (Cowork)
        if not (meta and meta.get("title")):   # a chat can span several transcript files — scan them all for the real title
            for cand in sess_paths.get(sk, ()):
                m = agent_meta_for(cand)
                if m and m.get("title"):
                    meta = m
                    break
        src = "app" if (meta and meta.get("title")) else "firstMessage"
        title = (meta and meta.get("title")) or titles.get(sk) or sp or "(session)"
        when = ""
        if meta and meta.get("created"):   # authoritative chat-start time (epoch ms) — stable, never bumps on reopen
            try:
                when = datetime.datetime.fromtimestamp(meta["created"] / 1000).isoformat(timespec="minutes")
            except Exception:
                when = ""
        if not when and fp:                # fallback for tools without metadata (Claude Code / Codex)
            try:
                st = os.stat(fp)
                bt = getattr(st, "st_birthtime", None)
                ts0 = min(bt, st.st_mtime) if bt else st.st_mtime
                when = datetime.datetime.fromtimestamp(ts0).isoformat(timespec="minutes")
            except Exception:
                when = ""
        return title, sp, tl, fp, sess_date.get(sk, ""), when, src

    bySession = []
    for sk, v in sorted(sess.items(), key=lambda x: -x[1])[:30]:
        title, sp, tl, fp, dt, when, _src = _resolve(sk)
        bySession.append([title, sp, tl, v, fp, dt, when])
    try:
        _t = datetime.date.today()
        leaks = {}
        # Build all-time FIRST and reuse its median as the 1x unit for every window, so a chat
        # keeps its badge and its estimated saving when you switch windows.
        for _w, _days in (("all", None), ("7", 7), ("30", 30)):
            _since = (_t - datetime.timedelta(days=_days - 1)).isoformat() if _days else None
            leaks[_w] = build_leaks(sess_resent, sess_turns, sess_startup, sess_proj, sess_date,
                                    _resolve, unmeasured, since=_since, window=_w,
                                    sess_fresh=sess_fresh, sess_cachew=sess_cachew,
                                    corrections=corrections, sess_tools=sess_tools)
    except Exception:
        leaks = None   # a leaks failure must never take the dashboard down
    return {"generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "grand": grand, "today": day_total.get(today, 0), "week": week,
            "byTool": dict(tool), "days": days, "byProject": byProject,
            "byModel": byModel, "bySession": bySession, "projectPaths": proj_path,
            "leaks": leaks,
            "usageMatrix": {"byModel": {k: list(v) for k, v in usage_model.items()},
                            "byDayModel": {d_: {m: list(v) for m, v in mm.items()} for d_, mm in usage_day_model.items()},
                            "byToolModel": {t_: {m: list(v) for m, v in mm.items()} for t_, mm in usage_tool_model.items()}}}

def summary():
    d = _fresh_session_titles(STATE["data"] or {})
    days = d.get("days", [])
    todaystr = datetime.date.today().isoformat()
    today_tool = next((x["byTool"] for x in days if x["date"] == todaystr), {})
    return {"loading": STATE["loading"], "grand": d.get("grand", 0), "today": d.get("today", 0),
            "week": d.get("week", 0), "byTool": d.get("byTool", {}),
            "todayByTool": today_tool, "topWhy": d.get("bySession", [])[:5],
            "accent": load_theme()["accent"], "primary": load_theme()["primary"]}

# ---------- live: what's running now ----------
def quick_title(path, kind, idx):
    if kind == "codex":
        try:
            with open(path, errors="ignore") as f:
                d = json.loads(f.readline())
            sid = (d.get("payload") or {}).get("id")
            return idx.get(sid or "", "Codex session")
        except Exception:
            return "Codex session"
    try:
        meta = agent_meta_for(path)
        if meta and meta.get("title"):
            return meta["title"]
    except Exception:
        pass
    try:
        with open(path, errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 400:
                    break
                if '"role": "user"' in line or '"role":"user"' in line:
                    try: d = json.loads(line)
                    except Exception: continue
                    m = d.get("message")
                    t = user_text(m) if isinstance(m, dict) else None
                    if t:
                        return " ".join(t.split())[:80]
                if '"slug"' in line:
                    try: d = json.loads(line)
                    except Exception: continue
                    if d.get("slug"):
                        return str(d["slug"]).replace("-", " ")
    except Exception:
        pass
    return "session"

def etime_to_secs(e):
    # ps etime format: [[DD-]HH:]MM:SS
    try:
        days = 0
        if "-" in e:
            dd, e = e.split("-", 1); days = int(dd)
        parts = [int(x) for x in e.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return days*86400 + parts[-3]*3600 + parts[-2]*60 + parts[-1]
    except Exception:
        return 0

def fmt_dur(secs):
    secs = int(secs or 0)
    if secs < 60: return f"{secs}s"
    m = secs // 60
    if m < 60: return f"{m}m"
    h = m // 60
    if h < 24: return f"{h}h {m%60}m"
    return f"{h//24}d {h%24}h"

def proc_open_files(pids):
    info = {}
    if not pids:
        return info
    try:
        lsof_bin = "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else "lsof"
        raw = subprocess.run([lsof_bin, "-p", ",".join(pids), "-Fpfn"],
                             capture_output=True, text=True, timeout=10).stdout
        cur = None; curfd = None
        for line in raw.splitlines():
            if not line:
                continue
            tag, val = line[0], line[1:]
            if tag == "p":
                cur = val; info[cur] = {"cwd": None, "jsonl": []}
            elif tag == "f":
                curfd = val
            elif tag == "n" and cur is not None:
                if curfd == "cwd" and not info[cur]["cwd"]:
                    info[cur]["cwd"] = val
                if val.endswith(".jsonl") and any(k in val for k in ("/sessions/", "/projects/", "local-agent")):
                    info[cur]["jsonl"].append(val)
    except Exception:
        pass
    return info

def kind_of_path(p):
    if "/.codex/" in p or "rollout-" in p: return "codex"
    if "local-agent" in p: return "cowork"
    return "claude"

def active_sessions():
    cutoff = time.time() - 600
    recent = []
    for kind, p in gather_files():
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt >= cutoff:
            recent.append((mt, kind, p))
    recent.sort(reverse=True)
    idx = load_codex_index() if any(k == "codex" for _, k, _ in recent) else {}
    out, seen = [], set()
    label = {"claude": "Claude Code", "cowork": "Cowork", "codex": "Codex"}
    for mt, kind, p in recent:
        tool = label[kind]
        title = quick_title(p, kind, idx)
        key = (tool, title)
        if key in seen:
            continue
        seen.add(key)
        out.append({"tool": tool, "title": title, "ago": int(time.time() - mt), "path": p})
        if len(out) >= 8:
            break
    return out

def fmt_mb(mb):
    mb = mb or 0
    if mb >= 1024:
        return f"{mb/1024:.1f} GB"
    return f"{mb:.0f} MB"

def parse_ts(v):
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return v/1000 if v > 1e12 else v
        s = str(v)
        if s.isdigit():
            n = int(s); return n/1000 if n > 1e12 else n
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def recent_token_burn(window=600, want_by_file=False):
    """Tokens logged in the last `window` seconds — a live burn rate. Optionally per file."""
    now = time.time(); cutoff = now - window
    total = 0; by_file = {}
    for kind, p in gather_files():
        try:
            if os.path.getmtime(p) < cutoff:
                continue
        except OSError:
            continue
        try:
            with open(p, errors="ignore") as f:
                lines = deque(f, maxlen=500)   # last 500 lines, O(1) memory
        except Exception:
            continue
        ftot = 0
        if kind == "codex":
            prev = None
            for line in lines:
                if "total_token_usage" not in line:
                    continue
                try: d = json.loads(line)
                except Exception: continue
                info = ((d.get("payload") or {}).get("info")) or {}
                tot = (info.get("total_token_usage") or {}).get("total_tokens")
                if tot is None:
                    continue
                ts = parse_ts(d.get("timestamp"))
                if prev is not None and tot >= prev and ts and ts >= cutoff:
                    ftot += tot - prev
                prev = tot
        else:
            for line in lines:
                if '"usage"' not in line:
                    continue
                try: d = json.loads(line)
                except Exception: continue
                # Claude Code uses "timestamp"; Cowork uses "_audit_timestamp".
                ts = parse_ts(d.get("timestamp") or d.get("_audit_timestamp"))
                if not ts or ts < cutoff:
                    continue
                u = ((d.get("message") or {}) or {}).get("usage") or {}
                ftot += (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                         + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0))
        if ftot:
            total += ftot
            by_file[p] = int(ftot)
    return (int(total), by_file) if want_by_file else int(total)

def system_stats(ai_cpu, ai_rss_kb):
    out = {"aiCpu": round(ai_cpu, 1), "aiRamMB": round(ai_rss_kb/1024, 1)}
    sysctl_bin = "/usr/sbin/sysctl" if os.path.exists("/usr/sbin/sysctl") else "sysctl"
    def sysctl(k):
        return subprocess.run([sysctl_bin, "-n", k], capture_output=True, text=True, timeout=4).stdout.strip()
    try:
        out["memTotalGB"] = round(int(sysctl("hw.memsize"))/1e9, 1)
    except Exception:
        pass
    try:
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=4).stdout
        psz = 4096
        m = re.search(r"page size of (\d+)", vm)
        if m: psz = int(m.group(1))
        def pages(k):
            mm = re.search(k + r":\s+(\d+)\.", vm); return int(mm.group(1)) if mm else 0
        used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * psz
        out["memUsedGB"] = round(used/1e9, 1)
    except Exception:
        pass
    try:
        out["cores"] = int(sysctl("hw.ncpu"))
        mm = re.search(r"([\d.]+)", sysctl("vm.loadavg"))
        if mm: out["load"] = float(mm.group(1))
    except Exception:
        pass
    return out

def live_data():
    procs = []
    CUSTOM_PROC = [(s.get("name"), (s.get("process") or "").lower())
                   for s in load_custom_sources() if s.get("process")]
    try:
        raw = subprocess.run(["ps", "-Axo", "pid=,%cpu=,rss=,etime=,command="],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:
        raw = ""
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        parts = s.split(None, 4)
        if len(parts) < 5:
            continue
        pid, cpu, rss, etime, cmd = parts
        low = cmd.lower()
        if "token-tracker" in low or "ps -axo" in low or "/grep" in low:
            continue
        base = os.path.basename(cmd.split()[0]) if cmd.split() else ""
        tool = None
        if "codex" in low and "codex installer" not in low:
            tool = "Codex"
        elif "ollama" in low:
            tool = "Ollama"
        elif "/.claude/" in low or base == "claude":
            tool = "Claude Code"
        elif base == "aider" or "/aider" in low:
            tool = "Aider"
        elif base == "gemini" or "gemini-cli" in low:
            tool = "Gemini CLI"
        elif "llama-server" in low or "llama.cpp" in low or base == "llama":
            tool = "llama.cpp"
        else:
            # user-added custom process patterns (manual "track its process")
            for s in CUSTOM_PROC:
                if s[1] and s[1] in low:
                    tool = s[0]; break
            if not tool:
                continue
        try: c = float(cpu)
        except Exception: c = 0.0
        try: rkb = int(rss)
        except Exception: rkb = 0
        procs.append({"pid": pid, "cpu": c, "rss": rkb, "etime": etime,
                      "secs": etime_to_secs(etime), "tool": tool})
    pids = [p["pid"] for p in procs][:14]
    of = proc_open_files(pids)
    idx = load_codex_index() if any(p["tool"] == "Codex" for p in procs) else {}
    ai_cpu = sum(p["cpu"] for p in procs)
    ai_rss = sum(p["rss"] for p in procs)
    burn_total, burn_by_file = _cached("burn", 8, lambda: recent_token_burn(600, True))

    sessions = active_sessions()
    sess_paths = set(s.get("path") for s in sessions)
    running = []
    for s in sessions:
        bf = burn_by_file.get(s["path"], 0)
        running.append({"tool": s["tool"], "title": s["title"],
                        "meta": fmt_dur(s["ago"]) + " ago", "kind": "session",
                        "burnMin": round(bf/10) if bf else 0})

    idle = {}
    for p in sorted(procs, key=lambda x: -x["cpu"]):
        files = of.get(p["pid"]) or {}
        js = [j for j in files.get("jsonl", []) if j not in sess_paths]
        if p["cpu"] >= 5 or js:
            title = quick_title(js[0], kind_of_path(js[0]), idx) if js else None
            if not title or title in ("session", "Codex session"):
                cwd = files.get("cwd")
                title = ("working in " + shorten(cwd)) if cwd else (p["tool"] + " process")
            bf = burn_by_file.get(js[0], 0) if js else 0
            running.append({"tool": p["tool"], "title": title,
                            "meta": f"{p['cpu']:.0f}% CPU · {fmt_mb(p['rss']/1024)} · up " + fmt_dur(p["secs"]),
                            "kind": "process", "burnMin": round(bf/10) if bf else 0})
        else:
            g = idle.setdefault(p["tool"], {"count": 0, "maxsecs": 0, "rss": 0})
            g["count"] += 1
            g["maxsecs"] = max(g["maxsecs"], p["secs"])
            g["rss"] += p["rss"]
    idle_summary = [{"tool": t, "count": v["count"], "uptime": fmt_dur(v["maxsecs"]),
                     "ram": fmt_mb(v["rss"]/1024)}
                    for t, v in sorted(idle.items(), key=lambda kv: -kv[1]["count"])]
    # Recompute insights live (fresh automations + login agents) so removals show within seconds,
    # reusing the cached historical aggregates for the chat/folder suggestions.
    insights = {"suggestions": [], "waste": []}
    try:
        if STATE.get("data"):
            insights = _cached("insights", 20, lambda: build_insights(STATE["data"]))
    except Exception:
        pass
    lo = _cached("leftovers", 12, find_leftovers)
    return {"at": datetime.datetime.now().strftime("%-I:%M:%S %p"),
            "running": running, "idle": idle_summary,
            "burn": {"tokens": burn_total, "perMin": round(burn_total/10), "windowMin": 10},
            "system": system_stats(ai_cpu, ai_rss),
            "insights": insights,
            "leftovers": {"count": len(lo), "freedMB": sum(t.get("rssMB", 0) for t in lo)}}

# ---------- insights: efficiency + waste ----------
def list_ai_launch_agents():
    out = []
    d = os.path.expanduser("~/Library/LaunchAgents")
    # AI-specific names only — NOT generic words like "agent"/"gateway", which match
    # unrelated things (e.g. com.google.keystone.agent is Chrome's updater, not AI).
    AI_KEYS = ("hermes", "codex", "openclaw", "claw", "anthropic", "claude", "ollama")
    SKIP_VENDORS = ("com.apple", "com.google", "com.microsoft", "com.adobe", "com.docker")
    try:
        for f in sorted(os.listdir(d)):
            if not f.endswith(".plist"):
                continue
            low = f.lower()
            if any(low.startswith(v) for v in SKIP_VENDORS):
                continue
            if any(k in low for k in AI_KEYS):
                out.append(f[:-6])
    except Exception:
        pass
    return out

def proc_running(substr):
    if not substr:
        return False
    try:
        raw = subprocess.run(["ps", "-Axo", "command="], capture_output=True, text=True, timeout=6).stdout
        return any(substr in line for line in raw.splitlines())
    except Exception:
        return False

def agent_details(label):
    """Read a LaunchAgent plist to explain what it launches and whether it's running."""
    plist = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
    info = {"program": None, "runAtLogin": False, "keepAlive": False, "interval": None, "running": False}
    try:
        j = subprocess.run(["plutil", "-convert", "json", "-o", "-", plist],
                           capture_output=True, text=True, timeout=5).stdout
        d = json.loads(j)
        prog = d.get("Program")
        args = d.get("ProgramArguments")
        if prog:
            info["program"] = prog
        elif isinstance(args, list) and args:
            info["program"] = " ".join(str(a) for a in args[:4])
        info["runAtLogin"] = bool(d.get("RunAtLoad"))
        info["keepAlive"] = bool(d.get("KeepAlive"))
        if d.get("StartInterval"):
            info["interval"] = d["StartInterval"]
        probe = None
        if prog:
            probe = os.path.basename(prog)
        elif isinstance(args, list) and args:
            probe = os.path.basename(str(args[0]))
        info["running"] = proc_running(probe) if probe else False
    except Exception:
        pass
    return info

def human_rrule(rrule):
    if not rrule:
        return "on a schedule"
    s = rrule.upper()
    fm = re.search(r"FREQ[:=]([A-Z]+)", s)
    im = re.search(r"INTERVAL[:=](\d+)", s)
    n = int(im.group(1)) if im else 1
    base = {"MINUTELY": "minute", "HOURLY": "hour", "DAILY": "day",
            "WEEKLY": "week", "MONTHLY": "month"}.get(fm.group(1) if fm else "", "cycle")
    return f"every {base}" if n == 1 else f"every {n} {base}s"

def human_since(val):
    if val in (None, ""):
        return None
    try:
        if isinstance(val, (int, float)):
            ts = val/1000 if val > 1e12 else val
        elif isinstance(val, str) and val.isdigit():
            v = int(val); ts = v/1000 if v > 1e12 else v
        elif isinstance(val, str):
            ts = datetime.datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
        else:
            return None
        return fmt_dur(max(0, time.time() - ts)) + " ago"
    except Exception:
        return None

def codex_automations():
    out = []
    db = os.path.expanduser("~/.codex/sqlite/codex-dev.db")
    if not os.path.exists(db):
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for row in con.execute("SELECT id, name, status, rrule, last_run_at, model, prompt FROM automations"):
            aid, name, status, rrule, last_run, model, prompt = row
            runs = unread = None
            try:
                runs = con.execute("SELECT COUNT(*) FROM automation_runs WHERE automation_id=?", (aid,)).fetchone()[0]
                unread = con.execute("SELECT COUNT(*) FROM automation_runs WHERE automation_id=? AND read_at IS NULL", (aid,)).fetchone()[0]
            except Exception:
                pass
            out.append({"name": name, "status": status, "rrule": rrule, "last_run_at": last_run,
                        "model": model, "runs": runs, "unread": unread,
                        "prompt": (prompt or "").strip()})
        con.close()
    except Exception:
        pass
    return out

def _folder_delete_help(disp, path, exists, hidden):
    lines = []
    if hidden:
        lines.append("This is a hidden folder — its name starts with a dot — which is why a normal Finder search didn't turn it up.")
    lines.append("Full path:  " + path)
    if not exists:
        lines.append("Heads-up: this folder isn't on your Mac right now — you may have already deleted it (for example when we removed openclaw earlier). The tokens shown are historical, so there's nothing left to remove.")
        return "\n".join(lines)
    lines.append("Find it:  open Finder → Go menu → “Go to Folder…” (press Shift+Cmd+G) → paste the path above → press Enter.")
    lines.append("Delete it:  drag the folder to the Trash, or select it and press Cmd+Delete. It stays in the Trash until you empty it, so it's reversible.")
    lines.append("Only delete it if you no longer need that project's files — this removes the folder's contents, not just its token history.")
    return "\n".join(lines)

def build_insights(data):
    byTool = data.get("byTool", {})
    grand = data.get("grand", 0) or 1
    sessions = data.get("bySession", [])
    projects = data.get("byProject", [])
    autos = codex_automations()
    active_autos = [a for a in autos if (a.get("status") or "").upper() == "ACTIVE"]

    # ----- efficiency = how-you-WORK changes (habits) ; removable background jobs live ONLY in
    # the "suggested to remove" section below, so the two sections never duplicate. -----
    sug = []  # each: {tag, text, score}
    # 1) biggest single chat (named by its title)
    if sessions:
        t = sessions[0]
        title, proj, tool, tok = t[0], t[1], t[2], t[3]
        if tok / grand > 0.15:
            sug.append({"tag": "Heavy chat", "score": tok,
                        "text": (f"Your biggest single chat — “{(title or 'untitled')[:70]}” ({tool}, {fmt_tok(tok)}, "
                                 f"{tok/grand*100:.0f}% of all your tokens) — keeps re-reading its own context every turn. "
                                 f"Wrap it up and start a fresh thread (or /compact) rather than continuing it.")})
    # 3) heaviest real project (named by its folder). Prefer the heaviest one whose
    #    folder still EXISTS, so the row carries a working move-to-Trash button —
    #    a suggestion that says "you can delete it" about a folder that is already
    #    gone is a to-do with nothing to do.
    if projects:
        ppaths = data.get("projectPaths", {})
        def _locate(disp):
            realpath = ppaths.get(disp)              # actual absolute cwd recorded in the logs
            cands = ([realpath] if realpath else []) + [os.path.expanduser("~/" + disp), "/" + disp, disp]
            real = next((c for c in cands if c and os.path.isdir(c)), None)
            return real, (real or realpath or os.path.expanduser("~/" + disp))
        pick = None                                  # (project_row, real, path); heaviest existing wins
        for x in projects:
            if x[0] in ("Cowork sessions", "(unknown)") or x[1] / grand <= 0.05:
                continue
            real, path = _locate(x[0])
            if pick is None:
                pick = (x, real, path)
            if real:
                pick = (x, real, path)
                break
        if pick:
            p, real, path = pick
            disp = p[0]
            hidden = any(seg.startswith(".") for seg in path.split("/") if seg)
            if real:
                text = (f"The folder “{disp}” has burned {fmt_tok(p[1])}. If work there is exploratory, scope each "
                        f"session to one file/task — that's where tighter prompts save the most. If you no longer need it, you can delete it.")
            else:
                text = (f"The folder “{disp}” burned {fmt_tok(p[1])} before it was removed from this Mac. "
                        f"Its history stays counted; there is nothing left to delete.")
            sug.append({"tag": "Heavy folder", "score": p[1] * 0.6, "text": text,
                        "folder": real,   # verified absolute path, or None — the trash_folder whitelist keys off this
                        "revealLabel": "Where was this folder?" if not real else "How do I find or delete this folder?",
                        "reveal": _folder_delete_help(disp, path, real is not None, hidden)})
    sug.sort(key=lambda s: -s["score"])
    suggestions = [{"tag": s["tag"], "text": s["text"], "folder": s.get("folder"),
                    "reveal": s.get("reveal"), "revealLabel": s.get("revealLabel")} for s in sug]

    # ----- waste / suggested-to-remove — with what-it-does / have-you-used-it / what-the-fix-does -----
    waste = []
    for a in sorted(active_autos, key=lambda a: -(a.get("runs") or 0)):
        runs = a.get("runs"); unread = a.get("unread")
        metric = [human_rrule(a.get("rrule"))]
        if runs is not None: metric.append(f"{runs} runs")
        if unread is not None: metric.append(f"{unread} never opened")
        since = human_since(a.get("last_run_at"))
        if since: metric.append(f"last {since}")
        pr = (a.get("prompt") or "").replace("\n", " ").strip()
        what = (f"A scheduled Codex job that automatically runs this prompt {human_rrule(a.get('rrule'))}: "
                f"“{pr[:160]}{'…' if len(pr) > 160 else ''}”." if pr
                else f"A scheduled Codex job that runs automatically {human_rrule(a.get('rrule'))}.")
        if runs:
            opened = runs - (unread or 0)
            usage = (f"It has produced {runs} results and you've opened {opened} of them"
                     + (" — so none of its output has been used." if opened == 0 else f" ({round(opened/runs*100)}%).")) if opened == 0 \
                    else f"It has produced {runs} results and you've opened {opened} of them ({round(opened/runs*100)}%)."
        else:
            usage = "It runs unattended on a schedule."
        impact = ("Switching it off stops the scheduled runs. Your existing results and chat threads stay, nothing on your "
                  "computer is deleted, and you can re-enable it anytime in Codex.")
        waste.append({"kind": "automation",
                      "label": f"Codex automation — {a['name']}",
                      "metric": " · ".join(metric),
                      "what": what, "usage": usage, "impact": impact,
                      "how": f"Open Codex → Automations and switch off “{a['name']}”. (I don't edit Codex's database from outside, to avoid corrupting it.)",
                      "executable": False})
    uid = os.getuid()
    for lbl in list_ai_launch_agents():
        det = agent_details(lbl)
        if det.get("program"):
            what = f"A background helper that starts at login and runs: {det['program']}."
        else:
            what = "A background helper set to start at login (its configuration doesn't name a clear command)."
        if det.get("keepAlive"):
            what += " It's kept alive — macOS restarts it if it exits."
        usage = ("It's running in the background right now." if det.get("running")
                 else "It isn't running at the moment.")
        if det.get("runAtLogin"):
            usage += " It's set to start automatically at every login."
        impact = ("The fix disables it immediately and stops it auto-starting, then moves its .plist to the Trash — so it's "
                  "fully reversible (restore from Trash to undo). No documents or app data are deleted and your other apps "
                  "keep working; only re-enable it if a tool you use turns out to need it.")
        waste.append({"kind": "agent", "agent": lbl,
                      "label": f"Login agent — {lbl}",
                      "metric": "auto-starts at login" + (" · running now" if det.get("running") else " · not running"),
                      "what": what, "usage": usage, "impact": impact,
                      "how": "", "executable": True,
                      "cmd": f"launchctl bootout gui/{uid}/{lbl} 2>/dev/null; mv ~/Library/LaunchAgents/{lbl}.plist ~/.Trash/"})
    return {"suggestions": suggestions, "waste": waste}

def apply_fix(label):
    """Disable + Trash a single AI login agent. Whitelisted to currently-detected agents only."""
    if label not in list_ai_launch_agents():
        return {"ok": False, "error": "Not an AI login agent I recognize."}
    plist = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
    try:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                       capture_output=True, text=True, timeout=10)
    except Exception:
        pass
    if not os.path.exists(plist):
        return {"ok": True, "message": f"{label} disabled (no plist file to move)."}
    trash = os.path.expanduser("~/.Trash")
    dest = os.path.join(trash, f"{label}.plist")
    if os.path.exists(dest):
        dest = os.path.join(trash, f"{label}.{int(time.time())}.plist")
    try:
        os.rename(plist, dest)
        return {"ok": True, "message": f"{label} disabled and moved to Trash (reversible)."}
    except Exception as e:
        return {"ok": False, "error": f"Disabled, but couldn't move the file: {e}"}

def trash_folder(body):
    """Move a flagged heavy folder to the Trash. Whitelisted to the folders the
    insights engine is flagging RIGHT NOW, so a request can never name an
    arbitrary path. The move is os.rename into ~/.Trash — reversible by
    dragging the folder back out, until the user empties the Trash."""
    path = body.get("path") or ""
    flagged = set()
    try:
        if STATE.get("data"):
            for sg in build_insights(STATE["data"]).get("suggestions", []):
                if sg.get("folder"):
                    flagged.add(sg["folder"])
    except Exception:
        pass
    if path not in flagged:
        return {"ok": False, "error": "Not a folder this page is currently flagging."}
    real = os.path.realpath(path)
    home = os.path.realpath(HOME)
    if not os.path.isdir(real) or real in (home, "/") or not real.startswith(home + os.sep):
        return {"ok": False, "error": "That folder is outside what this fix is willing to move."}
    if os.path.realpath(HERE).startswith(real):
        return {"ok": False, "error": "That folder contains the tracker itself."}
    trash = os.path.join(home, ".Trash")
    dest = os.path.join(trash, os.path.basename(real))
    if os.path.exists(dest):
        dest = os.path.join(trash, os.path.basename(real) + "." + str(int(time.time())))
    try:
        os.rename(real, dest)
    except Exception as e:
        return {"ok": False, "error": (type(e).__name__ + ": " + str(e))[:200]}
    return {"ok": True, "message": "Moved to the Trash. Drag it back out of the Trash to undo.",
            "dest": dest}

def find_leftovers():
    """Idle, leftover AI processes that are safe to quit. Strict protections:
    NEVER the Claude desktop app/helpers, NEVER this session's Claude Code backend
    (younger than 24h), NEVER the tracker itself."""
    try:
        raw = subprocess.run(["ps", "-Axo", "pid=,ppid=,%cpu=,rss=,etime=,command="],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return []
    me = os.getpid()
    out = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        parts = s.split(None, 5)
        if len(parts) < 6:
            continue
        pid, ppid, cpu, rss, etime, cmd = parts
        try: pidi = int(pid)
        except Exception: continue
        if pidi == me:
            continue
        low = cmd.lower()
        # hard protections
        if "token-tracker" in low or "/applications/claude.app/" in low:
            continue
        if "ps -axo" in low or "/grep" in low:
            continue
        try: c = float(cpu)
        except Exception: c = 0.0
        secs = etime_to_secs(etime)
        try: rmb = round(int(rss)/1024)
        except Exception: rmb = 0
        kind = None
        if "codex" in low and "installer" not in low:
            # leftover codex worker (the deleted automation spawned these)
            if secs > 3600 and c < 20:
                kind = "Codex leftover"
        elif "/claude-code/" in low and "contents/macos/claude" in low:
            # a Claude Code backend: only the STALE ones (>24h). Active session (4-5h) is protected.
            if secs >= 86400 and c < 20:
                kind = "Stale Claude Code backend"
        if kind:
            out.append({"pid": pid, "ppid": ppid, "cpu": c, "rssMB": rmb,
                        "etime": etime, "ageSecs": secs, "kind": kind, "cmd": cmd[:90]})
    # Final safety: drop any candidate whose session log was written in the last 10 min —
    # that means it's actually in use right now, not a leftover (covers an idle-between-turns session).
    if out:
        of = proc_open_files([t["pid"] for t in out])
        cutoff = time.time() - 600
        kept = []
        for t in out:
            files = (of.get(t["pid"]) or {}).get("jsonl", [])
            active = False
            for jf in files:
                try:
                    if os.path.getmtime(jf) >= cutoff:
                        active = True; break
                except OSError:
                    pass
            if not active:
                kept.append(t)
        out = kept
    return out

def _pid_cmd(pid):
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                              capture_output=True, text=True, timeout=4).stdout.strip()
    except Exception:
        return ""

def kill_leftovers():
    targets = find_leftovers()
    killed, failed = [], []
    for t in targets:
        pidi = int(t["pid"])
        # guard against PID reuse: confirm this PID is STILL the AI process we flagged
        cur = _pid_cmd(pidi).lower()
        if ("codex" not in cur) and ("/claude-code/" not in cur and "contents/macos/claude" not in cur):
            failed.append({**t, "error": "process changed since scan — skipped"})
            continue
        try:
            os.kill(pidi, signal.SIGTERM)
            killed.append(t)
        except ProcessLookupError:
            continue
        except Exception as e:
            failed.append({**t, "error": str(e)})
    time.sleep(1.5)
    for t in killed:
        pidi = int(t["pid"])
        try:
            os.kill(pidi, 0)          # still alive?
            os.kill(pidi, signal.SIGKILL)
            t["forced"] = True
        except OSError:
            pass
    freed = sum(t.get("rssMB", 0) for t in killed)
    return {"ok": True, "killed": killed, "failed": failed,
            "count": len(killed), "freedMB": freed}

def _set_price_override(model, rate):
    """Write/merge a single model rate into prices_override.json (USD per 1M tokens). Custom/Codex
    tokens are unsplit and priced at the input rate, so one rate covers all four kinds. Stdlib, no net."""
    if not model:
        return
    try:
        cur = {}
        if os.path.exists(OVERRIDES_FILE):
            try:
                cur = json.load(open(OVERRIDES_FILE)) or {}
                if not isinstance(cur, dict):
                    cur = {}
            except Exception:
                cur = {}
        cur[str(model).lower()] = [rate, rate, rate, rate]
        json.dump(cur, open(OVERRIDES_FILE, "w"), indent=2)
        _OVR["mtime"] = -1.0   # force _load_overrides() to re-read on next prices()
    except Exception:
        pass

def add_source(body):
    name = (body.get("name") or "").strip()
    glob_ = (body.get("glob") or "").strip()
    token_keys = [k.strip() for k in (body.get("tokenKeys") or "").split(",") if k.strip()]
    ts_keys = [k.strip() for k in (body.get("tsKeys") or "").split(",") if k.strip()]
    process = (body.get("process") or "").strip()
    rate = None
    rv = body.get("rate")
    if rv not in (None, ""):
        try:
            r = float(rv)
            if r > 0:
                rate = r
        except Exception:
            return {"ok": False, "error": "Rate must be a number (USD per 1M tokens), e.g. 3."}
    if not name:
        return {"ok": False, "error": "Give the tool a name."}
    if name in ("Claude Code", "Cowork", "Codex"):
        return {"ok": False, "error": "That name is built-in — pick another."}
    if not glob_ and not process:
        return {"ok": False, "error": "Give a log-file pattern (to count tokens) and/or a process name (for live tracking)."}
    if glob_ and not token_keys:
        return {"ok": False, "error": "Give the token field name(s) to read from the logs (e.g. total_tokens)."}
    lst = [s for s in load_custom_sources() if s.get("name") != name]
    src = {"name": name}
    if glob_:
        src["glob"] = glob_; src["tokenKeys"] = token_keys
    if ts_keys:
        src["tsKeys"] = ts_keys
    if process:
        src["process"] = process
    if body.get("color"):
        src["color"] = body["color"]
    if rate is not None:
        # Price this custom tool by tagging its entries with a stable model key and writing that
        # key's rate into prices_override.json — so its tokens appear in the $ figures (optional; blank = excluded).
        model_key = name.lower()
        src["model"] = model_key
        _set_price_override(model_key, rate)
    lst.append(src)
    if not save_custom_sources(lst):
        return {"ok": False, "error": "Couldn't write the config file."}
    return {"ok": True, "message": f"Added “{name}”. Rescanning your logs…"}

def remove_source(body):
    name = (body.get("name") or "").strip()
    save_custom_sources([s for s in load_custom_sources() if s.get("name") != name])
    return {"ok": True, "message": f"Removed “{name}”."}

def fmt_tok(n):
    n = n or 0
    def _strip(x):
        return re.sub(r"\.?0+$", "", f"{x:.2f}")
    if n >= 1e12: return _strip(n / 1e12) + "T"
    if n >= 1e9: return _strip(n / 1e9) + "B"
    if n >= 1e6: return f"{n/1e6:.0f}M"
    if n >= 1e3: return f"{n/1e3:.0f}K"
    return str(int(n))

def _codex_fn_name(pl):
    for k in ("name", "tool_name"):
        if pl.get(k):
            return pl[k]
    fn = pl.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return fn["name"]
    return None

# friendly labels for raw tool names
def _pretty_tool(nm):
    if not nm:
        return "tool"
    if nm == "shell" or nm == "local_shell" or nm == "bash" or "exec" in nm.lower():
        return "shell"
    if nm.startswith("mcp__"):
        parts = nm.split("__")
        return "MCP·" + (parts[-1] if parts else nm)[:18]
    return nm[:22]

def agent_runs(window=1800, max_runs=6):
    """Per recent agent run: what it's doing (tools/patches/reasoning), sub-agents, status, cost."""
    now = time.time(); cutoff = now - window
    files = []
    for kind, p in gather_files():
        try: mt = os.path.getmtime(p)
        except OSError: continue
        if mt >= cutoff:
            files.append((mt, kind, p))
    files.sort(reverse=True)
    idx = load_codex_index() if any(k == "codex" for _, k, _ in files) else {}
    label = {"claude": "Claude Code", "cowork": "Cowork", "codex": "Codex"}
    runs = []
    for mt, kind, p in files[:max_runs]:
        title = quick_title(p, ("codex" if kind == "codex" else kind), idx)
        tool_counts = {}
        subagents = []
        recent = []          # (ts, label) of recent actions
        n_calls = patches = mcp = reasoning = 0
        completed = False
        last_ts = mt
        try:
            with open(p, errors="ignore") as f:
                lines = deque(f, maxlen=1500)
            for line in lines:
                try: d = json.loads(line)
                except Exception: continue
                ts = parse_ts(d.get("timestamp") or d.get("_audit_timestamp")) or last_ts
                if kind == "codex":
                    pl = d.get("payload") or {}
                    pt = pl.get("type")
                    if pt in ("function_call", "custom_tool_call"):
                        nm = _pretty_tool(_codex_fn_name(pl))
                        n_calls += 1; tool_counts[nm] = tool_counts.get(nm, 0) + 1
                        recent.append((ts, "called " + nm))
                    elif pt == "mcp_tool_call_end":
                        mcp += 1; n_calls += 1; recent.append((ts, "called an MCP tool"))
                    elif pt == "patch_apply_end":
                        patches += 1; recent.append((ts, "applied a code patch"))
                    elif pt == "reasoning":
                        reasoning += 1
                    elif pt == "task_started":
                        recent.append((ts, "task started"))
                    elif pt == "task_complete":
                        completed = True; recent.append((ts, "task complete"))
                else:
                    msg = d.get("message")
                    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                        for it in msg["content"]:
                            if not isinstance(it, dict):
                                continue
                            if it.get("type") == "tool_use":
                                nm = it.get("name", "tool")
                                if nm in ("Task", "Agent"):
                                    inp = it.get("input") or {}
                                    subagents.append({"desc": str(inp.get("description") or "")[:70],
                                                      "type": inp.get("subagent_type") or ""})
                                    recent.append((ts, "spawned sub-agent: " + str(inp.get("description") or inp.get("subagent_type") or "")[:46]))
                                else:
                                    n_calls += 1
                                    pn = _pretty_tool(nm)
                                    tool_counts[pn] = tool_counts.get(pn, 0) + 1
                                    recent.append((ts, "called " + pn))
            if lines:
                try:
                    ld = json.loads(lines[-1])
                    last_ts = parse_ts(ld.get("timestamp") or ld.get("_audit_timestamp")) or mt
                except Exception:
                    pass
        except Exception:
            pass
        status = "done" if completed else ("active" if mt >= now - 120 else "idle")
        top_tools = sorted(tool_counts.items(), key=lambda kv: -kv[1])[:6]
        recent_lbls = [r[1] for r in recent[-12:]]
        runs.append({"tool": label[kind], "title": title, "status": status,
                     "ago": int(now - mt), "toolCalls": n_calls, "topTools": top_tools,
                     "patches": patches, "mcp": mcp, "reasoning": reasoning,
                     "subagents": subagents, "recent": recent_lbls, "file": p})
    return runs

def _esc_html(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def _read_transcript(path):
    kind = "claude"
    if "/.codex/" in path or "rollout-" in path: kind = "codex"
    elif "local-agent" in path: kind = "cowork"
    try:
        with open(path, errors="ignore") as f:
            lines = deque(f, maxlen=8000)   # bound memory on huge sessions; we show the last ~400 turns anyway
    except Exception:
        return None, kind
    turns = []
    if kind == "codex":
        for line in lines:
            try: d = json.loads(line)
            except Exception: continue
            pl = d.get("payload") or {}
            pt = pl.get("type")
            if pt in ("user_message", "message", "agent_message"):
                role = "user" if pt == "user_message" else "assistant"
                txt = pl.get("text") or pl.get("message") or ""
                if isinstance(txt, list):
                    txt = " ".join(str(x.get("text", "")) for x in txt if isinstance(x, dict))
                if txt and str(txt).strip():
                    turns.append((role, str(txt)))
            elif pt in ("function_call", "custom_tool_call"):
                turns.append(("tool", "→ ran " + _pretty_tool(_codex_fn_name(pl))))
            elif pt == "patch_apply_end":
                turns.append(("tool", "→ applied a code patch"))
    else:
        for line in lines:
            try: d = json.loads(line)
            except Exception: continue
            if d.get("isSidechain"):
                pass  # included inline; labeled by content below
            msg = d.get("message")
            if not isinstance(msg, dict): continue
            role = msg.get("role")
            if role == "user":
                t = user_text(msg)
                if t and t.strip():
                    turns.append(("user", t))
            elif role == "assistant":
                c = msg.get("content")
                if isinstance(c, str) and c.strip():
                    turns.append(("assistant", c))
                elif isinstance(c, list):
                    for it in c:
                        if not isinstance(it, dict): continue
                        if it.get("type") == "text" and it.get("text", "").strip():
                            turns.append(("assistant", it["text"]))
                        elif it.get("type") == "tool_use":
                            turns.append(("tool", "→ used " + _pretty_tool(it.get("name"))))
    return turns, kind

def transcript_html(path):
    label = {"claude": "Claude Code", "cowork": "Cowork", "codex": "Codex"}
    turns, kind = _read_transcript(path)
    head = ("<!doctype html><meta charset=utf-8><title>Transcript</title><style>"
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#fdfcf8;color:#1a1a17;max-width:820px;margin:0 auto;padding:28px 22px 80px;line-height:1.55}"
            "h1{font-size:20px;margin:0 0 2px}.sub{color:#6b6a63;font-size:13px;margin:0 0 22px}"
            ".t{margin:14px 0;padding:12px 15px;border-radius:12px;border:1px solid #e6e3da;white-space:pre-wrap;word-wrap:break-word;font-size:13.5px}"
            ".t .r{display:block;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;font-weight:700;margin-bottom:5px}"
            ".user{background:#eef1fb;border-color:#d6dcf5}.user .r{color:#4a52b8}"
            ".assistant{background:#fff}.assistant .r{color:#1f7a34}"
            ".tool{background:#faf8f2;border-style:dashed;color:#7a786f;font-size:12.5px;padding:7px 13px}.tool .r{display:none}"
            ".top{position:sticky;top:0;background:#fdfcf8;padding:6px 0 10px;border-bottom:1px solid #e6e3da;margin-bottom:8px}"
            "a{color:#1f7a34}</style>")
    if turns is None:
        return head + "<h1>Couldn't open this session</h1><p class=sub>The log file may have moved.</p>"
    total = len(turns)
    shown = turns[-400:]
    note = f" · showing last 400 of {total} entries" if total > 400 else ""
    body = [f"<div class=top><h1>Session transcript</h1><div class=sub>{label.get(kind,kind)} · {len(shown)} entries{note} · read-only</div></div>"]
    for role, txt in shown:
        t = _esc_html(txt)
        if len(t) > 6000:
            t = t[:6000] + " …(truncated)"
        if role == "tool":
            body.append(f'<div class="t tool">{t}</div>')
        else:
            body.append(f'<div class="t {role}"><span class="r">{role}</span>{t}</div>')
    return head + "".join(body)

# ---------- time series (stock-style ranges) ----------
_HOURLY = {"files": {}, "hours": {}, "ts": 0.0, "lock": threading.Lock()}
_HOURLY_KEEP_DAYS = 6
_HOURLY_SEED_CAP = 16 * 1024 * 1024   # first read of a huge file starts this far from its end

def _hourly_parse(kind, lines, fst, hours_map, label):
    """Bucket one batch of NEW log lines into per-hour per-tool totals."""
    if kind == "codex":
        prev = fst.get("prev")
        for line in lines:
            if "total_token_usage" not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            info = ((d.get("payload") or {}).get("info")) or {}
            tot = (info.get("total_token_usage") or {}).get("total_tokens")
            if tot is None:
                continue
            ts = parse_ts(d.get("timestamp"))
            if prev is not None and ts:
                if tot > prev:
                    v = tot - prev
                elif tot < prev:
                    v = tot        # counter reset: the fresh segment's opening reading is new usage
                else:
                    v = 0
                if v:
                    h = int(ts // 3600) * 3600
                    hours_map.setdefault(h, {})[label] = hours_map.get(h, {}).get(label, 0) + v
            prev = tot
        fst["prev"] = prev
    else:
        for line in lines:
            if '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = parse_ts(d.get("timestamp") or d.get("_audit_timestamp"))
            if not ts:
                continue
            u = ((d.get("message") or {}) or {}).get("usage") or {}
            v = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                 + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0))
            if v:
                h = int(ts // 3600) * 3600
                hours_map.setdefault(h, {})[label] = hours_map.get(h, {}).get(label, 0) + v

def _hourly_refresh():
    """Keep a rolling per-hour total per tool by reading each log file ONCE
    past its stored byte offset. Nothing is re-read and nothing is truncated
    to a tail window, so the heaviest sessions count in full — this is what
    the headroom ceiling is measured against."""
    now = time.time()
    with _HOURLY["lock"]:
        if now - _HOURLY["ts"] < 20:
            return
        _HOURLY["ts"] = now
        label = {"claude": "Claude Code", "cowork": "Cowork", "codex": "Codex"}
        horizon = now - _HOURLY_KEEP_DAYS * 86400
        for kind, p in gather_files():
            try:
                st = os.stat(p)
            except OSError:
                continue
            fst = _HOURLY["files"].get(p)
            if fst is None:
                fst = {"pos": 0, "prev": None}
                if st.st_mtime < horizon:
                    fst["pos"] = st.st_size          # entirely older than any window we serve
                elif st.st_size > _HOURLY_SEED_CAP:  # bound the very first read of a huge file
                    fst["pos"] = st.st_size - _HOURLY_SEED_CAP
                    fst["drop_first"] = True         # the seek lands mid-line; drop the fragment
                _HOURLY["files"][p] = fst
            if st.st_size == fst["pos"]:
                continue
            if st.st_size < fst["pos"]:              # truncated or rotated: start over
                fst["pos"], fst["prev"] = 0, None
            try:
                with open(p, "rb") as fh:
                    fh.seek(fst["pos"])
                    data = fh.read()
            except OSError:
                continue
            if not data:
                continue
            nl = data.rfind(b"\n")
            if nl < 0:
                continue                             # only a partial line so far; wait for more
            consumed = nl + 1
            chunk = data[:consumed]
            if fst.pop("drop_first", False):
                cut = chunk.find(b"\n")
                chunk = chunk[cut + 1:] if cut >= 0 else b""
            _hourly_parse(kind, chunk.decode("utf-8", "ignore").splitlines(), fst, _HOURLY["hours"], label[kind])
            fst["pos"] += consumed
        cutoff = int((now - _HOURLY_KEEP_DAYS * 86400) // 3600) * 3600
        for h in [h for h in _HOURLY["hours"] if h < cutoff]:
            _HOURLY["hours"].pop(h, None)

def _series_intraday(hours, bucket_secs):
    _hourly_refresh()
    now = time.time()
    start = now - hours * 3600
    nb = max(1, int(round(hours * 3600 / bucket_secs)))
    tools = ["Claude Code", "Cowork", "Codex"]
    data = [{t: 0 for t in tools} for _ in range(nb)]
    with _HOURLY["lock"]:
        items = [(h, dict(tl)) for h, tl in _HOURLY["hours"].items()]
    for h, tl in items:
        i = int((h - start) / bucket_secs)
        if 0 <= i < nb:
            for t, v in tl.items():
                if t in data[i]:
                    data[i][t] += v
    return [{"ts": int((start + i * bucket_secs) * 1000), "tools": data[i]} for i in range(nb)]

def _series_daily(days_back=None):
    d = STATE["data"] or {}
    m = {x["date"]: (x.get("byTool") or {}) for x in d.get("days", [])}
    if not m:
        return []
    dates = sorted(m.keys())
    start = datetime.date.fromisoformat(dates[0])
    end = datetime.date.today()
    if days_back:
        start = max(start, end - datetime.timedelta(days=days_back - 1))
    out = []
    cur = start
    while cur <= end:
        ds = cur.isoformat(); bt = m.get(ds, {})
        out.append({"ts": int(datetime.datetime(cur.year, cur.month, cur.day).timestamp() * 1000),
                    "tools": {"Claude Code": bt.get("Claude Code", 0),
                              "Cowork": bt.get("Cowork", 0), "Codex": bt.get("Codex", 0)}})
        cur += datetime.timedelta(days=1)
    return out

def series(rng):
    now = time.time()
    c = SERIES_CACHE.get(rng)
    if c and now - c[0] < 20:
        return c[1]
    if rng == "1d":
        out = {"range": "1d", "unit": "hour", "scale": "linear", "buckets": _series_intraday(24, 3600)}
    elif rng == "5d":
        out = {"range": "5d", "unit": "hour", "scale": "linear", "buckets": _series_intraday(120, 3600)}
    elif rng == "1m":
        out = {"range": "1m", "unit": "day", "scale": "log", "buckets": _series_daily(31)}
    else:
        out = {"range": "all", "unit": "day", "scale": "log", "buckets": _series_daily(None)}
    SERIES_CACHE[rng] = (now, out)
    return out

# ---------- token cost engine (API-list-price equivalent) ----------
# Turns the usage matrix (tokens by model, split into input/cache-write/cache-read/output)
# into dollar figures at public API list prices. Rates come from LiteLLM's community-maintained
# price sheet (fetched lazily, cached 24h in DATA_DIR/.prices.json, fail-silent) with a small
# bundled fallback so it works offline. IMPORTANT copy note: subscription plans don't bill per
# token — always present this as "API-equivalent value", never as an invoice.
PRICES_FILE = os.path.join(DATA_DIR, ".prices.json")
# Optional local price override, layered on top of the LiteLLM map. Lets any missing/custom model
# be priced with NO network call (local-first). Format: {"model-name": [input, cache_write, cache_read,
# output]} = USD per 1M tokens. Keys are lowercased to match match_price(). mtime-cached, stdlib only.
OVERRIDES_FILE = os.path.join(DATA_DIR, "prices_override.json")
_OVR = {"map": {}, "mtime": -1.0}
def _load_overrides():
    try:
        m = os.stat(OVERRIDES_FILE).st_mtime
        if m != _OVR["mtime"]:
            j = json.load(open(OVERRIDES_FILE))
            _OVR["map"] = {str(k).lower(): v for k, v in j.items()
                           if isinstance(v, (list, tuple)) and len(v) >= 4}
            _OVR["mtime"] = m
    except Exception:
        if _OVR["mtime"] < 0:
            _OVR["map"] = {}
    return _OVR["map"]
PRICES_URL = os.environ.get("TOKENBURN_PRICES_URL",
                            "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json")
PRICES_ON = os.environ.get("TOKENBURN_PRICES", "on").lower() not in ("off", "0", "false", "no")
PRICES_TTL = 24 * 3600
# Offline fallback only — USD per 1M tokens: [input, cache_write, cache_read, output] (approx list prices)
_BUNDLED_PRICES = {
    "claude-opus-4":     [15.0, 18.75, 1.50, 75.0],
    "claude-sonnet-4":   [3.0, 3.75, 0.30, 15.0],
    "claude-3-7-sonnet": [3.0, 3.75, 0.30, 15.0],
    "claude-3-5-haiku":  [0.80, 1.00, 0.08, 4.0],
    "claude-haiku-4-5":  [1.0, 1.25, 0.10, 5.0],
    "gpt-4o":            [2.5, 0.0, 1.25, 10.0],
    "gpt-4o-mini":       [0.15, 0.0, 0.075, 0.60],
    "gpt-5":             [1.25, 0.0, 0.125, 10.0],
}
_PRICES = {"map": None, "ts": 0.0, "source": "bundled"}
_PRICES_LOCK = threading.Lock()

def _parse_litellm(raw):
    """LiteLLM sheet -> {normalized model name: [in, cw, cr, out] USD per 1M tokens}."""
    out = {}
    for name, v in (raw or {}).items():
        if not isinstance(v, dict):
            continue
        ci, co = v.get("input_cost_per_token"), v.get("output_cost_per_token")
        if not isinstance(ci, (int, float)) or not isinstance(co, (int, float)):
            continue
        cw = v.get("cache_creation_input_token_cost") or 0
        cr = v.get("cache_read_input_token_cost") or 0
        rates = [ci * 1e6, (cw or 0) * 1e6, (cr or 0) * 1e6, co * 1e6]
        key = name.lower().split("/")[-1]           # "anthropic/claude-x" -> "claude-x"
        out.setdefault(key, rates)
    return out

def refresh_prices_now():
    """Fetch the price sheet NOW and say what happened — unlike the lazy
    background refresh, which is fail-silent by design."""
    if not PRICES_ON:
        return {"ok": False, "error": "Price fetching is switched off (TOKENBURN_PRICES=off)."}
    try:
        req = urllib.request.Request(PRICES_URL, headers={"User-Agent": "TokenBurnTracker"})
        raw = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace"))
        pm = _parse_litellm(raw)
        if not pm:
            return {"ok": False, "error": "The price sheet came back empty or unreadable."}
        with _PRICES_LOCK:
            _PRICES["map"], _PRICES["ts"], _PRICES["source"] = pm, time.time(), "litellm"
        try:
            json.dump({"ts": _PRICES["ts"], "map": pm}, open(PRICES_FILE, "w"))
        except Exception:
            pass
        return {"ok": True, "message": "Refreshed: %d models priced." % len(pm)}
    except Exception as e:
        return {"ok": False, "error": (type(e).__name__ + ": " + str(e))[:160]}

def _refresh_prices_bg():
    def _run():
        try:
            req = urllib.request.Request(PRICES_URL, headers={"User-Agent": "TokenBurnTracker"})
            raw = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace"))
            pm = _parse_litellm(raw)
            if pm:
                with _PRICES_LOCK:
                    _PRICES["map"], _PRICES["ts"], _PRICES["source"] = pm, time.time(), "litellm"
                try:
                    json.dump({"ts": _PRICES["ts"], "map": pm}, open(PRICES_FILE, "w"))
                except Exception:
                    pass
        except Exception:
            pass   # fail-silent: bundled/stale rates keep working
    threading.Thread(target=_run, daemon=True).start()

def prices():
    """Best available rate map + its source, with any local prices_override.json layered on top.
    Never blocks on the network."""
    def _merge(m, src, ts):
        o = _load_overrides()
        return (({**m, **o}) if o else m), src, ts
    with _PRICES_LOCK:
        if _PRICES["map"] and time.time() - _PRICES["ts"] < PRICES_TTL:
            return _merge(_PRICES["map"], _PRICES["source"], _PRICES["ts"])
    try:   # disk cache from a previous run
        j = json.load(open(PRICES_FILE))
        if isinstance(j.get("map"), dict) and j["map"]:
            with _PRICES_LOCK:
                _PRICES["map"], _PRICES["ts"], _PRICES["source"] = j["map"], j.get("ts", 0), "litellm"
    except Exception:
        pass
    if PRICES_ON and (not _PRICES["map"] or time.time() - _PRICES["ts"] >= PRICES_TTL):
        _refresh_prices_bg()
    if _PRICES["map"]:
        return _merge(_PRICES["map"], _PRICES["source"], _PRICES["ts"])
    return _merge(_BUNDLED_PRICES, "bundled", 0)

def match_price(model, pmap):
    """Model name from the logs -> (rates, matched_key). Tolerates date suffixes and prefixes."""
    if not model or model == "?":
        return None, None
    n = model.lower().split("/")[-1]
    if n in pmap:
        return pmap[n], n
    base = re.sub(r"[-_]\d{8}$", "", n)   # strip trailing -YYYYMMDD build dates
    if base in pmap:
        return pmap[base], base
    best = None
    for k in pmap:
        if (n.startswith(k) or base.startswith(k) or k.startswith(base)) and (best is None or len(k) > len(best)):
            best = k
    return (pmap[best], best) if best else (None, None)

def cost_data():
    """Dollar view of the usage matrix. Unsplit tokens (Codex/custom: totals only) are priced
    at the model's input rate and flagged approximate. Unmatched models are listed, not guessed."""
    d = STATE["data"] or {}
    um = (d.get("usageMatrix") or {})
    pmap, source, ts = prices()
    rate_cache, by_model, unmatched = {}, [], []
    kinds_tok = [0, 0, 0, 0]            # input, cache_write, cache_read, output
    kinds_usd = [0.0, 0.0, 0.0, 0.0]
    def usd(model, v):
        if model not in rate_cache:
            rate_cache[model] = match_price(model, pmap)
        rates, _k = rate_cache[model]
        if not rates:
            return None
        return (v[0] * rates[0] + v[1] * rates[1] + v[2] * rates[2] + v[3] * rates[3] + v[4] * rates[0]) / 1e6
    for m, v in sorted((um.get("byModel") or {}).items(), key=lambda x: -sum(x[1])):
        c = usd(m, v)
        toks = sum(v)
        if c is None:
            if toks > 0:
                unmatched.append(m)
            continue
        rates, mk = rate_cache[m]
        for _i in range(4):                       # accumulate the 4-way split, tokens + $ at real rates
            kinds_tok[_i] += v[_i]
            kinds_usd[_i] += v[_i] * rates[_i] / 1e6
        by_model.append({"model": m, "tokens": toks, "usd": round(c, 2),
                         "approx": v[4] > 0, "rateKey": mk})
    by_day = []
    for date, mm in sorted((um.get("byDayModel") or {}).items()):
        if date == "unknown":
            continue
        c = sum(filter(None, (usd(m, v) for m, v in mm.items())))
        by_day.append([date, round(c, 2)])
    by_tool = {}
    for t, mm in (um.get("byToolModel") or {}).items():
        by_tool[t] = round(sum(filter(None, (usd(m, v) for m, v in mm.items()))), 2)
    today = datetime.date.today().isoformat()
    weekago = (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
    monthago = (datetime.date.today() - datetime.timedelta(days=29)).isoformat()
    kind_names = ["input", "cache_write", "cache_read", "output"]
    token_breakdown = {kind_names[i]: {"tokens": kinds_tok[i], "usd": round(kinds_usd[i], 2)} for i in range(4)}
    tot_usd = sum(x["usd"] for x in by_model)
    tot_tok = sum(x["tokens"] for x in by_model)
    blended_rate = (tot_usd / tot_tok) if tot_tok > 0 else 0.0   # $/token, for live $/hr + period equivalents
    return {"note": "API-list-price equivalent. Subscription plans don't bill per token — "
                    "this is what the same usage would cost at public API rates.",
            "pricesSource": source,
            "pricesAgeHours": round((time.time() - ts) / 3600, 1) if ts else None,
            "total": round(sum(c for _, c in by_day), 2),
            "today": next((c for dt, c in by_day if dt == today), 0.0),
            "week": round(sum(c for dt, c in by_day if dt >= weekago), 2),
            "month": round(sum(c for dt, c in by_day if dt >= monthago), 2),
            "byModel": by_model, "byDay": by_day, "byTool": by_tool,
            "tokenBreakdown": token_breakdown, "blendedRate": blended_rate,
            "unmatchedModels": unmatched, "loading": STATE["loading"]}

# ---------- Prompt Coach (local prompt analyzer — nothing ever leaves the Mac) ----------
# Scores a prompt on the four token-efficiency levers and returns educational, specific
# suggestions. Pure stdlib heuristics: no LLM call, no network, prompt is never stored or
# sent anywhere (and never appears in analytics — those stay content-free).
_VAGUE_PAT = re.compile(r"\b(fix it|make it better|improve (this|it)|look into|check everything|"
                        r"clean (this|it) up|do something|make it work|figure (it|this) out|optimi[sz]e it)\b", re.I)
_BROAD_PAT = re.compile(r"\b(everything|entire|whole (project|codebase|folder|repo)|all (files|of it|the files)|"
                        r"every (file|folder)|full (project|repo|codebase))\b", re.I)
_SCOPE_PAT = re.compile(r"(/[\w.~-]+/|\.[a-z]{2,4}\b|`[^`]+`|\"[^\"]+\"|'[^']+'|\bfunction\b|\bclass\b|"
                        r"\bline \d|\bsection\b|https?://)", re.I)
_FORMAT_PAT = re.compile(r"\b(format|json|table|list|bullet|markdown|csv|one (paragraph|line|sentence)|"
                         r"under \d+|max(imum)? \d+|top \d+|"
                         r"\d+\s+(?:\w+\s+){0,2}(words|lines|sentences|paragraphs|bullets|items|points|examples|"
                         r"ideas|options|suggestions|criticisms?|changes|ways|steps|questions|feedback)|"
                         r"just the (diff|change)|diff only|steps|outline|short|brief|concise|tl;?dr)\b", re.I)
_NEG_PAT = re.compile(r"\b(don'?t|do not|avoid|exclude|skip|ignore|without|no need to|leave out|except)\b", re.I)
_CONT_PAT = re.compile(r"\b(as (we|i) (discussed|said|mentioned)|earlier you|as before|continue (from|where)|"
                       r"remember (when|what)|like (before|last time)|previous(ly)? (you|we))\b", re.I)
_LIGHT_TASK = re.compile(r"\b(extract|classif|categori|label|reformat|convert|translate|rename|summari[sz]e|"
                         r"list (the|all)|count|sort|dedup|proofread|fix typos?|spell)\b", re.I)
_HEAVY_TASK = re.compile(r"\b(architect|refactor (the|across|this whole)|design a system|security review|"
                         r"prove|debug.{0,20}(intermittent|flaky|race)|migrat|multi[- ]file|trade-?offs?|"
                         r"root cause|strategy|from scratch)\b", re.I)
_SELFCHECK_PAT = re.compile(r"\b(double[- ]check|verify (your|the) (work|answer|output)|are you sure|"
                            r"review your own)\b", re.I)
_RESEARCHY_PAT = re.compile(r"\b(research|find out|look up|cite|sources?|statistics|figures|study|studies|"
                            r"facts?|latest|news|market (size|share)|competitors?|who (is|was)|when (did|was)|"
                            r"how (many|much))\b", re.I)
_ESCAPE_PAT = re.compile(r"\b(if (you( a|')re )?(not |un)sure|say so|i don'?t know|don'?t (know|invent|guess|"
                         r"make (things|stuff|anything) up)|only if (you( a|')re )?certain|admit|confidence)\b", re.I)
_REVISION_PAT = re.compile(r"\b(change|update|tweak|edit|revise|adjust|rework|reword|modify)\b", re.I)
_HYPOTHETICAL_PAT = re.compile(r"\b(what|which)?\s*(would|should)\s+(you|we|i|they)\b", re.I)
def _is_revision(text):
    """An instruction to change something — not an opinion question like
    “what would you change”."""
    return bool(_REVISION_PAT.search(text)) and not _HYPOTHETICAL_PAT.search(text)
_DONE_PAT = re.compile(r"\b(done when|success|criteria|acceptance|should (look|read|behave) like|"
                       r"so that|until (it|the)|passes?|checklist)\b", re.I)
_CHECK_PAT = re.compile(r"\b(before (you )?(answer|reply|finish|show)|double[- ]check|verify|"
                        r"check (that|your|it|each)|make sure|confirm (that|it))\b", re.I)
_BUILD_PAT = re.compile(r"\b(build|write|create|make|implement|draft|design|plan|produce|generate)\b", re.I)
# The planning illusion (Nate B Jones, item 3): a complex task collapses itself into one shot
# instead of being staged. Detected as "did the prompt ask for a plan, or for steps, first".
_PLAN_PAT = re.compile(r"\b(plan (it|this|first)|step[- ]by[- ]step|outline (first|the)|"
                       r"break (it|this) (down|into)|in stages|one (step|section|part) at a time|"
                       r"before (you )?(start|begin|write|build)|propose an approach|"
                       r"first .{0,20}\bthen\b)\b", re.I)
# The drift problem (Nate B Jones, item 5): same input, different output. A prompt resists drift
# when it anchors to something stable — a named format, an example to match, or an explicit rule.
_ANCHOR_PAT = re.compile(r"\b(exactly like|same (format|structure|style|shape) as|match the|"
                         r"follow (the|this) (format|template|structure|example|convention)|"
                         r"use (the|this) (template|example|schema)|template|schema|"
                         r"for example[:,]|e\.g\.[:,]?|like this[:,]|as follows[:,])", re.I)

def _live_reread_share():
    """% of all tracked tokens that are cache reads (context being re-sent), from real data."""
    try:
        tb = (STATE["data"] or {}).get("tokenBreakdown") or {}
        tot = sum(tb.values())
        return round(100.0 * tb.get("cache_read", 0) / tot) if tot else None
    except Exception:
        return None

def _model_rate_line():
    """Live per-1M input rates for the light/medium/heavy tiers, from the price map."""
    pmap, _src, _ts = prices()
    out = {}
    for tier, names in (("light", ("claude-haiku-4-5", "claude-3-5-haiku")),
                        ("medium", ("claude-sonnet-4-6", "claude-sonnet-4")),
                        ("heavy", ("claude-opus-4-8", "claude-opus-4-5", "claude-opus-4"))):
        for n in names:
            r, k = match_price(n, pmap)
            if r:
                out[tier] = {"model": k, "in": round(r[0], 2), "out": round(r[3], 2)}
                break
    return out

def estimate_tokens(text):
    """Estimate tokens without a tokenizer, without a network call and without a dependency.

    len/4 is the folklore number and it is wrong in both directions. Measured against
    o200k_base: English prose 4.28 chars per token (len/4 overcounts by 7%), Python 4.99
    (overcounts 25%), JavaScript 4.39 (overcounts 10%), JSON 2.49 and JSONL 2.57 (UNDERCOUNTS
    by 36-42%). The sign flips with content type, so one divisor cannot be right.

    Picking the divisor by content gets the error to roughly 5% for free. This stays an
    estimate and is labelled as one. A real tokenizer is not an option here: tiktoken is a
    compiled Rust extension needing pip, the pure-JS ones add 2.3MB to a 137KB single file,
    and both vendors' counting endpoints need an API key and a network call, which this tool
    promises never to make. Note too that OpenAI's tokenizer is the wrong ruler for Claude
    text, which it undercounts by 22-44%."""
    t = text or ""
    n = len(t)
    if n == 0:
        return 1
    stripped = t.strip()
    # JSON-ish: heavy punctuation density is the tell, and it is the case len/4 gets worst.
    punct = sum(t.count(c) for c in '{}[]":,')
    if punct * 1.0 / n > 0.06 or (stripped[:1] in "{[" and stripped[-1:] in "}]"):
        per = 2.5
    # Code-ish: indentation, semicolons, or the usual keywords.
    elif (t.count("\n    ") + t.count("\n\t") + t.count(";")) > max(3, n / 400) or \
         any(k in t for k in ("def ", "function ", "class ", "import ", "const ", "=> ", "</")):
        per = 4.8
    else:
        per = 4.3
    return max(1, int(round(n / per)))

def analyze_prompt(text):
    """Grade a prompt on eight dimensions, each tied to a detectable signal.

    Six implement the prompt issues Nate B Jones set out in "Here's How to Solve
    the 6 Top Prompt Issues (Based on 29,000 OpenAI Comments)", 6 Nov 2025,
    https://www.youtube.com/watch?v=KwQpPbLEBMA : the projection trap (goal),
    the cognitive bandwidth trap (scope), the planning illusion (staging), the
    confidence illusion (honesty), the revision loop (revision) and the drift
    problem (consistency). Two more are ours and are not his: the output
    contract and verification. Keep that line intact — crediting him for the
    two we added would be as wrong as not crediting him for the six.

    Each graded dimension quotes what the prompt actually says, criticises it
    plainly, and hands back one ready sentence that fixes it. Runs locally; the
    prompt is never stored or sent."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty prompt"}
    words = len(text.split())
    est_tokens = estimate_tokens(text)
    low = text.lower()
    codeish = any(w in low for w in ("code", "file", "project", "folder", "repo", "app", "site", "docs"))
    researchy = bool(_RESEARCHY_PAT.search(text))
    revision = _is_revision(text) 
    multipart = words >= 25 and (low.count(" and ") + low.count(";") + low.count(" then ") + text.count("?")) >= 3

    def q(m, cap=40):
        t = m.group(0) if m else ""
        return (t[:cap] + "…") if len(t) > cap else t

    rubric = []
    def dim(key, name, weight, status, note, fix, principle):
        rubric.append({"key": key, "name": name, "weight": weight, "status": status,
                       "note": note, "fix": fix, "principle": principle})

    # ---- 1. The goal (30): what does done look like? ----
    vague = _VAGUE_PAT.search(text)
    if words < 5:
        dim("goal", "The goal", 25, "fail",
            f"{words} words. The model has to invent the task, the scope and the finish line, and every wrong invention costs a retry.",
            "Say what you want, where to look, and what the result should look like: one clear sentence each.",
            "The projection trap: under-specified asks make the model fill gaps with assumptions.")
    elif vague:
        dim("goal", "The goal", 25, "fail",
            f"“{q(vague)}” names neither the exact problem nor what fixed looks like, so the model decides both.",
            "Name the fault and the finish line: what is wrong, and the one check that proves it is done.",
            "The projection trap: under-specified asks make the model fill gaps with assumptions.")
    elif _BUILD_PAT.search(text) and words >= 12 and not _DONE_PAT.search(text) and not _FORMAT_PAT.search(text):
        dim("goal", "The goal", 25, "mixed",
            "The task is clear, but nothing says how to tell when it is done, so the model decides when to stop.",
            "Add one clause: done when <the check you will apply>.",
            "State the outcome and the test for done, or you pay for the review loop.")
    else:
        dim("goal", "The goal", 25, "pass",
            "The ask is concrete enough to act on without guessing.", "",
            "The projection trap: avoided.")

    # ---- 2. Scope and context (25): where to look, what to skip ----
    broad = _BROAD_PAT.search(text)
    if broad and not _NEG_PAT.search(text):
        dim("scope", "Scope and context", 20, "fail",
            f"“{q(broad)}” with no exclusions invites the tool to read every file it can find: the single biggest token explosion in agentic tools.",
            "Point at what matters and say what to skip: only <the folder>, ignore tests and dependencies.",
            "The cognitive bandwidth trap: context is something to filter, not accumulate.")
    elif est_tokens >= 700:
        dim("scope", "Scope and context", 20, "mixed",
            f"About {est_tokens:,} tokens of pasted material. Past a point, extra context makes answers worse, and every pasted line is billed on every later turn.",
            "Paste only the relevant excerpts, or name files by path and let the tool read what it needs.",
            "The cognitive bandwidth trap: minimal necessary context beats a full dump.")
    elif (codeish or researchy) and not _SCOPE_PAT.search(text):
        dim("scope", "Scope and context", 20, "mixed",
            "No file, folder, function, link or quoted name is given, so the tool searches for the target first and you pay for the search.",
            "Name the thing: a path, a function, a quoted heading, a URL.",
            "Label the context you provide; do not make the model find it.")
    else:
        dim("scope", "Scope and context", 20, "pass",
            "The prompt says where to look, or does not need to.", "",
            "The cognitive bandwidth trap: avoided.")

    # ---- 3. Output contract (20): exact shape and size ----
    if _FORMAT_PAT.search(text):
        dim("output", "Output contract", 14, "pass",
            "The answer's shape and size are pinned down.", "",
            "Contracts matter: format first, prose second.")
    elif words >= 8:
        dim("output", "Output contract", 14, "fail",
            "No shape or size is specified, and unbounded asks get long answers by default. Output is the most expensive kind of token.",
            "Add one clause: answer in 5 bullets, just the diff, or one paragraph.",
            "Specify the output shape: the first beginner move, and the cheapest.")
    else:
        dim("output", "Output contract", 14, "na",
            "Too short to judge separately from the goal.", "",
            "")

    # ---- 4. Honesty guard (10): permission to say "I don't know" ----
    if researchy:
        if _ESCAPE_PAT.search(text):
            dim("honesty", "Honesty guard", 10, "pass",
                "The model is allowed to admit uncertainty, so it does not have to bluff.", "",
                "The confidence illusion: handled.")
        else:
            dim("honesty", "Honesty guard", 10, "fail",
                "Facts are requested with no way out, so a fluent guess beats an honest blank, and a made-up answer costs a full retry plus the checking.",
                "Add one line: if you are not sure, say so; do not invent sources.",
                "The confidence illusion: permit unknown, require confidence labels.")
    else:
        dim("honesty", "Honesty guard", 10, "na", "No factual claims are being requested.", "", "")

    # ---- 5. Surgical revision (10): quote the target ----
    if revision:
        if _SCOPE_PAT.search(text):
            dim("revision", "Surgical revision", 8, "pass",
                "The change names its target, so only that section needs to come back.", "",
                "The revision loop: avoided.")
        else:
            dim("revision", "Surgical revision", 8, "fail",
                "A change is requested without quoting its target, so the model rewrites the whole thing and may touch what you never mentioned. You pay for a full regeneration every round.",
                "Quote the exact snippet, say what is wrong with it, and ask for only that section back.",
                "The revision loop: be surgical, patch one section at a time.")
    else:
        dim("revision", "Surgical revision", 8, "na", "This is not a revision request.", "", "")

    # ---- 6. Verification (5): a self-check before it answers ----
    if words >= 12:
        if _CHECK_PAT.search(text):
            dim("check", "Verification", 4, "pass",
                "A check is requested before the answer comes back.", "",
                "Quality checks: a verification loop in the same turn is nearly free.")
        else:
            dim("check", "Verification", 4, "mixed",
                ("Several asks are stacked and nothing verifies the answer covers them all." if multipart
                 else "Nothing asks the model to check its answer before showing it."),
                "Add: before you answer, check it against each point I asked for.",
                "Quality checks: one line buys a free verification pass.")
    else:
        dim("check", "Verification", 4, "na", "Short ask; a self-check would cost more than it saves.", "", "")

    # ---- 7. The planning illusion (14): a big build that was never staged ----
    # Nate B Jones, item 3 of six: "complex tasks will often collapse themselves into one shot".
    # A one-shot attempt at a large build is the most expensive failure on this list, because the
    # rework is a second full generation rather than a patch.
    if _BUILD_PAT.search(text) and (multipart or words >= 40):
        if _PLAN_PAT.search(text):
            dim("planning", "Staging", 14, "pass",
                "The work is staged rather than demanded in one shot.", "",
                "The planning illusion: avoided.")
        else:
            dim("planning", "Staging", 14, "fail",
                "This asks for a large piece of work in a single shot. Complex tasks collapse into "
                "one attempt, and when the attempt misses you pay to generate the whole thing again.",
                "Add: plan the approach first and wait for me to confirm before you build it.",
                "The planning illusion: stage the work, do not one-shot it.")
    else:
        dim("planning", "Staging", 14, "na",
            "Small enough to answer in one pass.", "", "")

    # ---- 8. The drift problem (5): nothing anchors the answer, so runs disagree ----
    # Nate B Jones, item 5 of six: "same inputs and different outputs".
    if words >= 20 and _BUILD_PAT.search(text):
        if _ANCHOR_PAT.search(text) or _FORMAT_PAT.search(text):
            dim("drift", "Consistency", 5, "pass",
                "An example or fixed shape is given, so repeat runs land in the same place.", "",
                "The drift problem: anchored.")
        else:
            dim("drift", "Consistency", 5, "mixed",
                "Nothing here pins the answer to a fixed shape, so asking twice gives two different "
                "answers and you pay to reconcile them.",
                "Add one anchor: follow the same structure as the example below, or name the format.",
                "The drift problem: anchor the output or runs will disagree.")
    else:
        dim("drift", "Consistency", 5, "na",
            "Too short for run-to-run drift to cost anything.", "", "")

    # ---- order for reading: what is actually graded first, heaviest first, and every
    # n/a dimension last. Dimensions are appended in detection order, which is not the order
    # a person wants to read them in. Sorting here keeps display order stable no matter where
    # a new dimension gets inserted in the code above. ----
    _rank = {"fail": 0, "mixed": 1, "pass": 2, "na": 3}
    rubric.sort(key=lambda r: (3 if r["status"] == "na" else 0, -r["weight"], _rank[r["status"]]))

    # ---- score: earned share of the applicable weights ----
    pts = {"pass": 1.0, "mixed": 0.5, "fail": 0.0}
    applicable = [r for r in rubric if r["status"] != "na"]
    wsum = sum(r["weight"] for r in applicable) or 1
    score = int(round(100.0 * sum(r["weight"] * pts[r["status"]] for r in applicable) / wsum))
    if score >= 85:
        verdict = "Send it."
    elif score >= 60:
        verdict = "Thirty seconds of tightening will pay for itself."
    else:
        verdict = "As written, this will burn tokens on guesses and retries."

    # ---- the tightened version: their prompt plus ready default lines for
    # every dimension that failed, nothing to fill in ----
    core = " ".join(text.split())
    adds = []
    if broad and not _NEG_PAT.search(text):
        adds.append("Skip anything not needed to answer: tests, generated files, dependency folders, and files you have already read in this session.")
    elif (codeish or researchy) and not _SCOPE_PAT.search(text) and est_tokens < 700:
        adds.append("Read only the files you need, tell me which ones you read, and ask before reading a whole folder.")
    if revision and not _SCOPE_PAT.search(text):
        adds.append("Return only the changed section, not the whole thing.")
    if not _FORMAT_PAT.search(text) and words >= 8:
        adds.append("Keep the answer short: a list or the diff, no preamble.")
    if researchy and not _ESCAPE_PAT.search(text):
        adds.append("If you are not sure, say so; do not invent sources.")
    if words >= 12 and not _CHECK_PAT.search(text):
        adds.append("Before you answer, check it against each point I asked for.")
    scaffold = core + (("\n\n" + "\n".join(adds)) if adds else "")

    return {"ok": True, "estTokens": est_tokens, "words": words, "score": score,
            "verdict": verdict, "rubric": rubric,
            "scaffold": scaffold, "scaffoldHasAdds": bool(adds),
            "rereadShare": _live_reread_share()}

# ---------- server ----------
THEME_FILE = os.path.join(DATA_DIR, "theme.json")
def _is_hex(a):
    return isinstance(a, str) and len(a) == 7 and a[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in a[1:])
def load_theme():
    t = {"primary": "", "accent": "#9c2a2c"}   # primary = background (empty until chosen), accent = highlight
    try:
        with open(THEME_FILE, "r", encoding="utf-8") as f:
            j = json.load(f) or {}
        if _is_hex(j.get("primary")): t["primary"] = j["primary"]
        if _is_hex(j.get("accent")):  t["accent"]  = j["accent"]
    except Exception:
        pass
    return t
def save_theme(primary=None, accent=None):
    t = load_theme()
    if primary is not None and (primary == "" or _is_hex(primary)): t["primary"] = primary
    if _is_hex(accent):  t["accent"]  = accent
    try:
        with open(THEME_FILE, "w", encoding="utf-8") as f:
            json.dump(t, f)
        return True
    except Exception:
        return False

# ---------- Übersicht desktop widget (one-click install from the dashboard) ----------
# One button does everything: if the free Übersicht app is missing it is downloaded and
# installed first, then the widget is written with __TRACKER_DIR__ resolved and Übersicht opened.
UBERSICHT_URL = "https://tracesof.net/uebersicht/"
UBERSICHT_ZIP_FALLBACK = "https://tracesof.net/uebersicht/releases/Uebersicht-1.6.82.app.zip"   # last known good
WIDGET_SRC = os.path.join(HERE, "widget", "index.jsx")
WIDGET_DEST_DIR = os.path.join(HOME, "Library", "Application Support", "Übersicht", "widgets", "token-burn.widget")

def ubersicht_app():
    """Path of an installed Übersicht.app, or None. Scans the usual spots by name so
    umlaut-less renames (Uebersicht.app) are found too."""
    for parent in ("/Applications", os.path.join(HOME, "Applications")):
        try:
            for n in sorted(os.listdir(parent)):
                if n.endswith(".app") and "bersicht" in n and os.path.isdir(os.path.join(parent, n)):
                    return os.path.join(parent, n)
        except Exception:
            continue
    return None

def _ubersicht_download_url():
    """Current app zip URL scraped from the Übersicht homepage (version is in the filename);
    falls back to the last known release if the page can't be read."""
    try:
        req = urllib.request.Request(UBERSICHT_URL, headers={"User-Agent": "TokenBurnTracker"})
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "replace")
        m = re.search(r'href="([^"]*releases/Uebersicht-[\d.]+\.app\.zip)"', html)
        if m:
            return urllib.parse.urljoin(UBERSICHT_URL, m.group(1))
    except Exception:
        pass
    return UBERSICHT_ZIP_FALLBACK

def install_ubersicht():
    """Download + install the free Übersicht app (GPL, tracesof.net). Returns the .app path or None.
    Prefers /Applications when writable, else ~/Applications. ditto keeps the code signature intact."""
    tmpdir = tempfile.mkdtemp(prefix="tokenburn-uber-")
    zpath = os.path.join(tmpdir, "uebersicht.zip")
    try:
        req = urllib.request.Request(_ubersicht_download_url(), headers={"User-Agent": "TokenBurnTracker"})
        with urllib.request.urlopen(req, timeout=120) as r, open(zpath, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        extract = os.path.join(tmpdir, "x")
        os.makedirs(extract, exist_ok=True)
        for cmd in (["ditto", "-xk", zpath, extract], ["unzip", "-oq", zpath, "-d", extract]):
            try:
                if subprocess.run(cmd, capture_output=True, timeout=180).returncode == 0:
                    break
            except Exception:
                continue
        app_src = next((os.path.join(extract, n) for n in sorted(os.listdir(extract))
                        if n.endswith(".app") and "bersicht" in n), None)   # umlaut-safe match
        if not app_src:
            return None
        dest_parent = "/Applications" if os.access("/Applications", os.W_OK) else os.path.join(HOME, "Applications")
        os.makedirs(dest_parent, exist_ok=True)
        if subprocess.run(["mv", "-f", app_src, dest_parent + "/"], capture_output=True, timeout=60).returncode != 0:
            return None
        return ubersicht_app()
    except Exception as e:
        analytics_error("install_ubersicht", e)
        return None
    finally:
        subprocess.run(["rm", "-rf", tmpdir], capture_output=True)

def widget_source():
    """Widget code with __TRACKER_DIR__ resolved to this install (mirrors install.sh's sed)."""
    with open(WIDGET_SRC, "r", encoding="utf-8") as f:
        return f.read().replace("__TRACKER_DIR__", DATA_DIR)

def widget_status():
    """Everything the dashboard control needs to pick its state."""
    return {"ubersichtInstalled": bool(ubersicht_app()),
            "widgetInstalled": os.path.isfile(os.path.join(WIDGET_DEST_DIR, "index.jsx")),
            "widgetSourceOk": os.path.isfile(WIDGET_SRC),
            "ubersichtUrl": UBERSICHT_URL}

def install_widget():
    """The whole one-click flow: get Übersicht if needed, write the filled-in widget, open the app.
    Also serves as 'refresh' after a self-update (self-update only refreshes the source file)."""
    st = widget_status()
    if not st["widgetSourceOk"]:
        return dict({"ok": False, "error": "widget/index.jsx is missing — re-run the one-line installer"}, **st)
    app = ubersicht_app()
    auto_installed = False
    if not app:
        app = install_ubersicht()
        auto_installed = bool(app)
        if not app:
            return dict({"ok": False, "error": "couldn't download Übersicht — use the links below"}, **widget_status())
    try:
        code = widget_source()
        os.makedirs(WIDGET_DEST_DIR, exist_ok=True)
        with open(os.path.join(WIDGET_DEST_DIR, "index.jsx"), "w", encoding="utf-8") as f:
            f.write(code)
        try:
            subprocess.Popen(["open", app], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass   # widget is installed either way; Übersicht picks it up on next launch
        analytics_event("widget_installed", {"app_version": local_version(),
                        "macos": (platform.mac_ver()[0] or "?"), "$os": "Mac OS X",
                        "ubersicht_autoinstalled": auto_installed})   # anonymous + content-free, like all events
        return dict({"ok": True, "ubersichtAutoInstalled": auto_installed}, **widget_status())
    except Exception as e:
        analytics_error("install_widget", e)
        return dict({"ok": False, "error": (type(e).__name__ + ": " + str(e))[:200]}, **widget_status())

class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ct="application/json", cors=False, extra=None):
        b = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ct)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        # Local-only + same-origin for the dashboard; no CORS in general, so other websites can't
        # read your data or the action token. EXCEPTION: the /api/theme color endpoint is CORS-open
        # (cors=True) so the desktop widget can sync one accent color (a harmless string).
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except BrokenPipeError:
            pass

    def _local_host(self):
        h = (self.headers.get("Host") or "").lower()
        return h.startswith("127.0.0.1") or h.startswith("localhost")

    def do_GET(self):
        if not self._local_host():   # block DNS-rebinding / non-local Host headers
            self._send(403, json.dumps({"error": "non-local request refused"})); return
        # The page is one file, so the root route has to tolerate a query string. Without this,
        # any cache-busting reload (/?v=123) or a link carrying tracking params 404s.
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            try:
                html = open(os.path.join(HERE, os.environ.get("TOKENBURN_HTML", "tracker.html")), "r", encoding="utf-8").read()
                _t = load_theme()
                html = html.replace("__FIX_TOKEN__", FIX_TOKEN).replace("__ACCENT__", _t["accent"]).replace("__PRIMARY__", _t["primary"])
                self._send(200, html, "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, "tracker.html missing", "text/plain")
        elif self.path.startswith("/api/summary"):
            self._send(200, json.dumps(summary()))
        elif self.path.startswith("/api/live"):
            self._send(200, json.dumps(live_data()))
        elif self.path.startswith("/api/theme"):
            self._send(200, json.dumps(load_theme()))
        elif self.path.startswith("/api/widget"):
            self._send(200, json.dumps(widget_status()))
        elif self.path.startswith("/api/costs"):
            self._send(200, json.dumps(cost_data()))
        elif self.path.startswith("/api/leaks"):
            from urllib.parse import urlparse, parse_qs
            win = (parse_qs(urlparse(self.path).query).get("days", ["30"])[0])
            d = _fresh_session_titles(STATE["data"] or {})   # live titles, same as /api/summary
            all_w = d.get("leaks") or {}
            if win not in all_w:
                win = "30" if "30" in all_w else (next(iter(all_w), "all"))
            self._send(200, json.dumps({"leaks": all_w.get(win), "window": win,
                                        "available": sorted(all_w.keys()),
                                        "loading": STATE["loading"]}))
        elif self.path.startswith("/widget.jsx"):
            # Plain-download fallback (no Übersicht detected, or user prefers manual):
            # a ready-to-use single-file widget — drop it into Übersicht's widgets folder.
            try:
                self._send(200, widget_source(), "application/javascript; charset=utf-8",
                           extra={"Content-Disposition": 'attachment; filename="token-burn.jsx"'})
            except Exception:
                self._send(404, json.dumps({"error": "widget source missing"}))
        elif self.path.startswith("/api/leftovers"):
            self._send(200, json.dumps({"leftovers": find_leftovers()}))
        elif self.path.startswith("/api/agents"):
            self._send(200, json.dumps({"runs": _cached("agents", 10, agent_runs)}))
        elif self.path.startswith("/api/series"):
            from urllib.parse import urlparse, parse_qs
            rng = (parse_qs(urlparse(self.path).query).get("range", ["all"])[0])
            self._send(200, json.dumps(series(rng)))
        elif self.path.startswith("/transcript"):
            from urllib.parse import urlparse, parse_qs, unquote
            fp = unquote(parse_qs(urlparse(self.path).query).get("file", [""])[0])
            allowed = {p for _, p in gather_files()}   # only real session logs, no traversal
            if fp in allowed:
                self._send(200, transcript_html(fp), "text/html; charset=utf-8")
            else:
                self._send(403, "<h1>Not an available session log.</h1>", "text/html; charset=utf-8")
        elif self.path.startswith("/api/data"):
            out = {"loading": STATE["loading"], "error": STATE["error"],
                   "files": STATE["files"], "parsed": STATE["parsed"]}
            if STATE["data"]:
                out.update(_fresh_session_titles(STATE["data"]))
            try:
                out["update"] = check_update()
            except Exception:
                out["update"] = {"current": local_version(), "latest": None, "outdated": False, "cmd": UPDATE_INSTALL_CMD}
            self._send(200, json.dumps(out))
        elif self.path.startswith("/api/checkupdate"):
            # Manual, uncached update check — used by the Rescan button so it's never stale.
            try:
                out = force_check_update()
            except Exception:
                out = {"current": local_version(), "latest": None, "outdated": False, "cmd": UPDATE_INSTALL_CMD}
            self._send(200, json.dumps(out))
        elif self.path.startswith("/api/refresh"):
            if not STATE["loading"]:
                STATE["loading"] = True
                STATE["error"] = None
                threading.Thread(target=build, daemon=True).start()
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path.startswith("/api/theme"):   # shared accent color, set same-origin from the dashboard (no token; harmless color string)
            if not self._local_host():
                self._send(403, json.dumps({"ok": False})); return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._send(400, json.dumps({"ok": False})); return
            ok = save_theme(body.get("primary"), body.get("accent"))
            self._send(200 if ok else 400, json.dumps(dict({"ok": ok}, **load_theme()))); return
        POSTS = ("/api/fix", "/api/kill_leftovers", "/api/add_source", "/api/remove_source", "/api/applyupdate", "/api/install_widget", "/api/prompt_check", "/api/trash_folder", "/api/refresh_prices")
        if not any(self.path.startswith(x) for x in POSTS):
            self._send(404, json.dumps({"error": "not found"})); return
        # Security: local origin only + per-launch secret that only our served page knows.
        if not self._local_host():
            self._send(403, json.dumps({"ok": False, "error": "non-local request refused"})); return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._send(400, json.dumps({"ok": False, "error": "bad request"})); return
        if body.get("token") != FIX_TOKEN:
            self._send(403, json.dumps({"ok": False, "error": "invalid token"})); return
        if self.path.startswith("/api/applyupdate"):
            ok, msg = apply_update()
            self._send(200, json.dumps({"ok": ok, "message": msg}))
            if ok:
                restart_self()   # detached relaunch; this process exits ~1s after the response is sent
            return
        if self.path.startswith("/api/install_widget"):
            self._send(200, json.dumps(install_widget())); return
        if self.path.startswith("/api/prompt_check"):
            # analysis happens in-process; the prompt is not stored, logged, or sent anywhere
            self._send(200, json.dumps(analyze_prompt(body.get("prompt") or ""))); return
        if self.path.startswith("/api/refresh_prices"):
            self._send(200, json.dumps(refresh_prices_now())); return
        if self.path.startswith("/api/trash_folder"):
            res = trash_folder(body); _LIVE_CACHE.clear()
            self._send(200, json.dumps(res)); return
        if self.path.startswith("/api/kill_leftovers"):
            res = kill_leftovers(); _LIVE_CACHE.clear()
            self._send(200, json.dumps(res)); return
        if self.path.startswith("/api/add_source") or self.path.startswith("/api/remove_source"):
            res = add_source(body) if "add_source" in self.path else remove_source(body)
            _LIVE_CACHE.clear(); SERIES_CACHE.clear()
            if res.get("ok"):
                threading.Thread(target=build, daemon=True).start()   # re-scan to pick up the new source
            self._send(200, json.dumps(res)); return
        label = body.get("agent") or ""
        result = apply_fix(label); _LIVE_CACHE.clear()
        self._send(200, json.dumps(result))

    def log_message(self, *a):
        pass

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def periodic_rebuild():
    import time
    while True:
        time.sleep(480)
        if not STATE["loading"]:
            STATE["loading"] = True
            STATE["error"] = None
            build()

if __name__ == "__main__":
    threading.Thread(target=build, daemon=True).start()
    threading.Thread(target=periodic_rebuild, daemon=True).start()
    print(f"Token Burn Tracker -> http://localhost:{PORT}")
    print("Scanning your Claude Code / Cowork / Codex logs… (first scan can take a bit)")
    analytics_launch()
    Server(("127.0.0.1", PORT), Handler).serve_forever()
