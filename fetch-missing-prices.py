#!/usr/bin/env python3
"""
fetch-missing-prices.py — OPT-IN maintenance tool (NOT part of the always-on server).

Token Burn Tracker prices your usage at public API list prices using a community-maintained
sheet (LiteLLM). A brand-new / preview / internal / custom model that isn't in that sheet gets
listed under `unmatchedModels` and is left out of the "$" figure until a rate is supplied.

This script is a deliberate, run-it-yourself helper (run manually, or have an agent run it) to fill
those missing rates. It:
  1. Asks the RUNNING dashboard's /api/costs (default port 8800; use --port for another) which
     models couldn't be priced, and prints them.
  2. Merges any rates you supply — via --set "model=in,cw,cr,out" (USD per 1M tokens) and/or a JSON
     file (--file) shaped like {"model-name": [input, cache_write, cache_read, output]} — into
     DATA_DIR/prices_override.json, which tracker.py layers on top of the live price map (no restart
     needed; it's mtime-cached).

It is intentionally NOT wired into tracker.py and NEVER scrapes the web on its own — the always-on
server stays offline-capable and local-first. If you want public rates looked up, do that yourself
(or via an agent) and pass them in with --set/--file. The only reads it performs are:
  - the LOCAL dashboard's /api/costs (to discover unmatched models), and
  - the LOCAL prices_override.json file.

Examples:
  # Just see what's missing on the dev dashboard (port 8800):
  python3 fetch-missing-prices.py

  # Fill a couple of rates (USD per 1M tokens: input, cache_write, cache_read, output):
  python3 fetch-missing-prices.py --set "my-model=3,3.75,0.30,15" --set "preview-x=1.25,0,0.125,10"

  # Or from a JSON file, and against a custom port / data dir:
  python3 fetch-missing-prices.py --file rates.json --port 8799 --data-dir ~/path/to/data
"""
import argparse
import json
import os
import sys
import urllib.request

DEFAULT_PORT = 8800


def default_data_dir():
    """Match tracker.py's resolution: TOKENBURN_DATA_DIR, else this script's own folder."""
    return os.environ.get("TOKENBURN_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))


def fetch_costs(port):
    url = "http://127.0.0.1:%d/api/costs" % port
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print("Couldn't reach the dashboard at %s (%s)." % (url, e), file=sys.stderr)
        print("Is Token Burn Tracker running on that port? Try --port.", file=sys.stderr)
        return None


def parse_set(spec):
    """'model=in,cw,cr,out' -> (model_lower, [in,cw,cr,out] floats), or raises ValueError."""
    if "=" not in spec:
        raise ValueError("expected model=in,cw,cr,out")
    name, rates = spec.split("=", 1)
    name = name.strip().lower()
    parts = [p.strip() for p in rates.split(",") if p.strip() != ""]
    if not name:
        raise ValueError("empty model name")
    if len(parts) not in (1, 4):
        raise ValueError("give 1 rate (applied to all kinds) or 4: input,cache_write,cache_read,output")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError("rates must be numbers")
    if len(nums) == 1:
        nums = [nums[0]] * 4
    return name, nums


def load_json_rates(path):
    with open(path) as f:
        j = json.load(f)
    out = {}
    if not isinstance(j, dict):
        raise ValueError("JSON file must be an object of {model: [in,cw,cr,out]}")
    for k, v in j.items():
        if isinstance(v, (list, tuple)) and len(v) >= 4:
            try:
                out[str(k).lower()] = [float(x) for x in v[:4]]
            except (TypeError, ValueError):
                print("Skipping %r: rates must be numbers." % k, file=sys.stderr)
        else:
            print("Skipping %r: expected a list [input, cache_write, cache_read, output]." % k, file=sys.stderr)
    return out


def merge_overrides(data_dir, new_rates):
    path = os.path.join(data_dir, "prices_override.json")
    cur = {}
    if os.path.exists(path):
        try:
            cur = json.load(open(path)) or {}
            if not isinstance(cur, dict):
                cur = {}
        except Exception:
            cur = {}
    cur.update(new_rates)
    try:
        os.makedirs(data_dir, exist_ok=True)
    except Exception:
        pass
    with open(path, "w") as f:
        json.dump(cur, f, indent=2)
    return path


def main():
    ap = argparse.ArgumentParser(description="Fill missing model prices for Token Burn Tracker (opt-in, manual).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="port the running dashboard is on (default %d)" % DEFAULT_PORT)
    ap.add_argument("--data-dir", default=None,
                    help="where prices_override.json lives (default: TOKENBURN_DATA_DIR or this script's folder)")
    ap.add_argument("--set", action="append", default=[], metavar="MODEL=in,cw,cr,out",
                    help="set a model's rate (USD per 1M tokens); repeatable")
    ap.add_argument("--file", default=None,
                    help='JSON file of {"model": [input, cache_write, cache_read, output]} rates to merge')
    args = ap.parse_args()

    data_dir = args.data_dir or default_data_dir()

    # 1) Report what the running dashboard couldn't price.
    costs = fetch_costs(args.port)
    if costs is not None:
        unmatched = costs.get("unmatchedModels") or []
        if unmatched:
            print("Models the dashboard couldn't price (not in the $ figure):")
            for m in unmatched:
                print("  - %s" % m)
            print("Supply rates with --set \"model=in,cw,cr,out\" (USD per 1M tokens) or --file rates.json.")
        else:
            print("Good news: every model in use is already priced. Nothing missing.")

    # 2) Merge any supplied rates into the override file.
    new_rates = {}
    for spec in args.set:
        try:
            name, nums = parse_set(spec)
            new_rates[name] = nums
        except ValueError as e:
            print("Bad --set %r: %s" % (spec, e), file=sys.stderr)
            return 2
    if args.file:
        try:
            new_rates.update(load_json_rates(args.file))
        except Exception as e:
            print("Couldn't read --file %s (%s)." % (args.file, e), file=sys.stderr)
            return 2

    if new_rates:
        path = merge_overrides(data_dir, new_rates)
        print("Wrote %d rate(s) to %s" % (len(new_rates), path))
        for k, v in new_rates.items():
            print("  %s -> input=%s cache_write=%s cache_read=%s output=%s (USD / 1M tokens)"
                  % (k, v[0], v[1], v[2], v[3]))
        print("The dashboard will pick these up automatically (mtime-cached; no restart needed).")
    else:
        print("No rates supplied — nothing written. (Add --set or --file to fill rates.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
