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
import http.server, socketserver, json, os, re, glob, threading, datetime, traceback, subprocess, time, sqlite3, secrets, signal, platform, urllib.request
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
# One small launch event (install id, app + macOS version, which integrations exist)
# so improvements can be prioritized. Never sends prompts, titles, or token amounts.
# Turn it off with:  TOKENBURN_ANALYTICS=off
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
def analytics_event(event, props=None):
    if not (ANALYTICS_ON and PH_KEY): return
    def _send():
        try:
            body = json.dumps({"api_key": PH_KEY, "event": event,
                               "distinct_id": _install_id(), "properties": dict(props or {})}).encode()
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

CACHE_FILE = os.path.join(DATA_DIR, ".cache.json")
CACHE_VERSION = 5   # bumped: cache entries now also store a per-file token breakdown (input/cache/output)
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

# ---------- parsers -> (entries[[date,tool,model,project,tokens,sessionKey]], titles{sk:title}) ----------
def _empty_breakdown():
    return {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}

def claude_entries(path, tool, file_date):
    entries, titles = [], {}
    tb = _empty_breakdown()   # input/cache/output split, summed across every usage record in this file
    last_ts = None
    default_cwd = "Cowork sessions" if tool == "Cowork" else "?"
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                is_usage = '"usage"' in line
                is_user = ('"role": "user"' in line) or ('"role":"user"' in line)
                if not is_usage and not is_user:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                sk = session_key(tool, d.get("sessionId"), path)
                if msg.get("role") == "user":
                    t = user_text(msg)
                    if t:
                        titles.setdefault(sk, " ".join(t.split())[:90])
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
                entries.append([date, tool, msg.get("model") or "?", d.get("cwd") or default_cwd, tk, sk])
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
        ents = [[dt, "Codex", model or "codex", cwd or "?", int(tok), sk]
                for dt, tok in by_date.items() if tok > 0]
        # Codex's rollout logs only expose a cumulative total_tokens per event (no
        # input/cache/output split), so its share of the token breakdown is left at 0 —
        # same limitation as everywhere else this file reads Codex usage (see series/live-burn).
        return ents, {sk: index_map.get(sid or "", "Codex session")}, _empty_breakdown()
    return [], {}, _empty_breakdown()

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
    by_date = defaultdict(int)
    try:
        with open(path, errors="ignore") as f:
            for line in deque(f, maxlen=20000):
                line = line.strip()
                if not line or not any(k in line for k in tkeys):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                tok = _find_token_sum(d, tkeys)
                if tok <= 0:
                    continue
                dt = _ts_date(_find_first(d, tskeys)) or file_date
                by_date[dt] += tok
    except Exception:
        pass
    ents = [[dt, name, src.get("model") or "?", proj, int(tok), sk] for dt, tok in by_date.items() if tok > 0]
    # Custom sources only declare which field(s) hold a token count, not which kind
    # (input/cache/output), so they don't contribute to the token breakdown split.
    return ents, ({sk: os.path.basename(path)} if ents else {}), _empty_breakdown()

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
    except Exception as e:
        STATE["error"] = str(e) + "\n" + traceback.format_exc()
        STATE["loading"] = False
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
    grand = 0
    for date, tl, md, cwd, tk, sk in entries:
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
        title = titles.get(sk) or sp or "(session)"
        bySession.append([title, sp, tl, v])
    return {"generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "grand": grand, "today": day_total.get(today, 0), "week": week,
            "byTool": dict(tool), "days": days, "byProject": byProject,
            "byModel": byModel, "bySession": bySession, "projectPaths": proj_path}

def summary():
    d = STATE["data"] or {}
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

class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ct="application/json", cors=False):
        b = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ct)
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
                out.update(STATE["data"])
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
        POSTS = ("/api/fix", "/api/kill_leftovers", "/api/add_source", "/api/remove_source")
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
