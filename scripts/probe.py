#!/usr/bin/env python3
"""Diagnose why the lineup pipeline got 0 fixtures."""
import json
import os
import urllib.error
import urllib.request

KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
BASE = "https://v3.football.api-sports.io"


def call(path):
    req = urllib.request.Request(BASE + path, headers={"x-apisports-key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def show(label, path):
    code, body = call(path)
    print("\n" + "=" * 70)
    print(label)
    print("GET", path, "->", code)
    if isinstance(body, str):
        print("  raw:", body)
        return
    errs = body.get("errors")
    resp = body.get("response")
    print("  errors :", json.dumps(errs) if errs else "(none)")
    print("  results:", body.get("results"))
    if isinstance(resp, list) and resp:
        print("  first  :", json.dumps(resp[0])[:400])
    else:
        print("  response is empty")


if not KEY:
    raise SystemExit("API_FOOTBALL_KEY is not set")

print("key present, length =", len(KEY))

show("1. account status", "/status")
show("2. EPL fixtures, season 2026", "/fixtures?league=39&season=2026&next=3")
show("3. EPL fixtures, season 2025", "/fixtures?league=39&season=2025&next=3")
show("4. EPL fixtures, no season param", "/fixtures?league=39&next=3")
show("5. EPL league record", "/leagues?id=39")
