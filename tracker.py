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
CACHE_VERSION = 11   # bumped: entries now carry a per-record input/cache/output split (cost engine)
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
    entries, titles = [], {}
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
                    continue
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
    return entries, titles, tb

def codex_entries(path, file_date, index_map):
    cwd = model = first_ts = sid = None
    prev_total = None
    by_date = defaultdict(int)   # the cumulative total is distributed across the days it accrued
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
                    tt = (p["info"].get("total_token_usage") or {}).get("total_tokens")
                    if not isinstance(tt, (int, float)):
                        continue
                    ev_date = _ts_date(d.get("timestamp")) or _ts_date(first_ts) or file_date
                    if prev_total is None:
                        by_date[ev_date] += tt            # baseline so far → this event's day
                        prev_total = tt
                    elif tt > prev_total:
                        by_date[ev_date] += tt - prev_total   # new tokens → the day they happened
                        prev_total = tt
    except Exception:
        pass
    total = sum(by_date.values())
    if total > 0:
        sk = "Codex:" + (sid or os.path.basename(path))
        ents = [[dt, "Codex", model or "codex", cwd or "?", int(tok), sk, path]
                for dt, tok in by_date.items() if tok > 0]
        # Codex's rollout logs only expose a cumulative total_tokens per event (no
        # input/cache/output split), so its share of the token breakdown is left at 0 —
        # same limitation as everywhere else this file reads Codex usage (see series/live-burn).
        return ents, {sk: index_map.get(sid or "", "Codex session")}, _empty_breakdown()
    return [], {}, _empty_breakdown()

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
        return [], {}, _empty_breakdown()
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
    return ents, ({sk: (ctitle or os.path.basename(path))} if ents else {}), _empty_breakdown()

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
            else:
                fdate = datetime.date.fromtimestamp(mt).isoformat()
                if kind == "claude":
                    ents, tts, tb = claude_entries(path, "Claude Code", fdate)
                elif kind == "cowork":
                    ents, tts, tb = claude_entries(path, "Cowork", fdate)
                else:
                    ents, tts, tb = codex_entries(path, fdate, index_map)
                parsed += 1
                STATE["parsed"] = parsed
            newcache[path] = {"mtime": mt, "entries": ents, "titles": tts, "tb": tb}
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
                    ents, tts, tb = custom_entries(path, src, fdate)
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
        d = aggregate(entries, titles)
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
_AGENT_META = {"idx": {}, "ts": 0.0}
def load_agent_meta():
    idx = {}
    base = HOME + "/Library/Application Support/Claude/local-agent-mode-sessions"
    for mp in glob.glob(base + "/**/local_*.json", recursive=True):
        m = re.search(r"(local_[0-9a-fA-F-]+)\.json$", mp)
        if not m:
            continue
        try:
            d = json.load(open(mp, errors="ignore"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        idx[m.group(1)] = {"title": (d.get("title") or "").strip(),
                           "created": d.get("createdAt"), "last": d.get("lastActivityAt")}
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
    m = re.search(r"(local_[0-9a-fA-F-]+)", path or "")
    return agent_meta().get(m.group(1)) if m else None

def _fresh_session_titles(d):
    """bySession is baked at scan time, but the app writes/renames chat titles asynchronously —
    so a chat can get its real sidebar title AFTER we scanned it. Re-resolve from the live
    metadata index at serve time (the same source the Agents view uses, which is why that view
    was always right). Cheap: agent_meta() is cached ~8s and this touches ≤30 rows."""
    try:
        for row in (d or {}).get("bySession") or []:
            if len(row) >= 5 and row[4]:
                m = agent_meta_for(row[4])
                if m and m.get("title"):
                    row[0] = m["title"]
    except Exception:
        pass
    return d

def aggregate(entries, titles):
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
        else:   # tools that only expose totals (Codex, custom sources)
            um[4] += tk; udm[4] += tk; utm[4] += tk
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
    bySession = []
    for sk, v in sorted(sess.items(), key=lambda x: -x[1])[:30]:
        tl, sp = sess_meta.get(sk, ("?", "?"))
        fp = sess_file.get(sk, "")
        meta = agent_meta_for(fp)   # real sidebar title + start time from the app's own per-chat metadata (Cowork)
        if not (meta and meta.get("title")):   # a chat can span several transcript files — scan them all for the real title
            for cand in sess_paths.get(sk, ()):
                m = agent_meta_for(cand)
                if m and m.get("title"):
                    meta = m
                    break
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
        bySession.append([title, sp, tl, v, fp, sess_date.get(sk, ""), when])
    return {"generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "grand": grand, "today": day_total.get(today, 0), "week": week,
            "byTool": dict(tool), "days": days, "byProject": byProject,
            "byModel": byModel, "bySession": bySession, "projectPaths": proj_path,
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
    # 3) heaviest real project (named by its folder)
    if projects:
        p = next((x for x in projects if x[0] not in ("Cowork sessions", "(unknown)")), None)
        if p and p[1] / grand > 0.05:
            disp = p[0]
            ppaths = data.get("projectPaths", {})
            realpath = ppaths.get(disp)              # actual absolute cwd recorded in the logs
            cands = ([realpath] if realpath else []) + [os.path.expanduser("~/" + disp), "/" + disp, disp]
            real = next((c for c in cands if c and os.path.isdir(c)), None)
            path = real or realpath or os.path.expanduser("~/" + disp)
            hidden = any(seg.startswith(".") for seg in path.split("/") if seg)
            sug.append({"tag": "Heavy folder", "score": p[1] * 0.6,
                        "text": (f"The folder “{disp}” has burned {fmt_tok(p[1])}. If work there is exploratory, scope each "
                                 f"session to one file/task — that's where tighter prompts save the most. If you no longer need it, you can delete it."),
                        "revealLabel": "How do I find or delete this folder?",
                        "reveal": _folder_delete_help(disp, path, real is not None, hidden)})
    sug.sort(key=lambda s: -s["score"])
    suggestions = [{"tag": s["tag"], "text": s["text"],
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

def add_source(body):
    name = (body.get("name") or "").strip()
    glob_ = (body.get("glob") or "").strip()
    token_keys = [k.strip() for k in (body.get("tokenKeys") or "").split(",") if k.strip()]
    ts_keys = [k.strip() for k in (body.get("tsKeys") or "").split(",") if k.strip()]
    process = (body.get("process") or "").strip()
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
    if n >= 1e9: return f"{n/1e9:.2f}B"
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
def _series_intraday(hours, bucket_secs):
    now = time.time(); start = now - hours * 3600
    nb = max(1, int(round(hours * 3600 / bucket_secs)))
    tools = ["Claude Code", "Cowork", "Codex"]
    data = [{t: 0 for t in tools} for _ in range(nb)]
    label = {"claude": "Claude Code", "cowork": "Cowork", "codex": "Codex"}
    def bidx(ts):
        i = int((ts - start) / bucket_secs)
        return i if 0 <= i < nb else None
    for kind, p in gather_files():
        try: mt = os.path.getmtime(p)
        except OSError: continue
        if mt < start - bucket_secs:
            continue
        try:
            with open(p, errors="ignore") as f:
                lines = deque(f, maxlen=20000)
        except Exception:
            continue
        tl = label[kind]
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
                if prev is not None and tot >= prev and ts:
                    bi = bidx(ts)
                    if bi is not None:
                        data[bi][tl] += tot - prev
                prev = tot
        else:
            for line in lines:
                if '"usage"' not in line:
                    continue
                try: d = json.loads(line)
                except Exception: continue
                ts = parse_ts(d.get("timestamp") or d.get("_audit_timestamp"))
                if not ts:
                    continue
                bi = bidx(ts)
                if bi is None:
                    continue
                u = ((d.get("message") or {}) or {}).get("usage") or {}
                data[bi][tl] += (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                                 + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0))
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
    """Best available rate map + its source. Never blocks on the network."""
    with _PRICES_LOCK:
        if _PRICES["map"] and time.time() - _PRICES["ts"] < PRICES_TTL:
            return _PRICES["map"], _PRICES["source"], _PRICES["ts"]
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
        return _PRICES["map"], _PRICES["source"], _PRICES["ts"]
    return _BUNDLED_PRICES, "bundled", 0

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
    return {"note": "API-list-price equivalent. Subscription plans don't bill per token — "
                    "this is what the same usage would cost at public API rates.",
            "pricesSource": source,
            "pricesAgeHours": round((time.time() - ts) / 3600, 1) if ts else None,
            "total": round(sum(c for _, c in by_day), 2),
            "today": next((c for dt, c in by_day if dt == today), 0.0),
            "week": round(sum(c for dt, c in by_day if dt >= weekago), 2),
            "month": round(sum(c for dt, c in by_day if dt >= monthago), 2),
            "byModel": by_model, "byDay": by_day, "byTool": by_tool,
            "unmatchedModels": unmatched, "loading": STATE["loading"]}

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
        if self.path in ("/", "/index.html"):
            try:
                html = open(os.path.join(HERE, "tracker.html"), "r", encoding="utf-8").read()
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
        POSTS = ("/api/fix", "/api/kill_leftovers", "/api/add_source", "/api/remove_source", "/api/applyupdate", "/api/install_widget")
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
