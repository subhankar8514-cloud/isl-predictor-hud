#!/usr/bin/env python3
"""Lineup capture pipeline - single file. Captures confirmed team sheets before
kickoff and attaches player ratings after full time.

    python3 lineup_pipeline.py schedule    # once a day: cache upcoming fixtures
    python3 lineup_pipeline.py capture     # every 15 min: poll for lineups
    python3 lineup_pipeline.py enrich      # hourly: attach post-match stats
    python3 lineup_pipeline.py report      # what have we got

Env: API_FOOTBALL_KEY (required), SEASON=2026, MAX_CALLS=10,
     CAPTURE_WINDOW_MIN=75, LATE_CAPTURE_MIN=120, MAX_ENRICH=4
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://v3.football.api-sports.io"
OUT = os.environ.get("OUT_DIR", "data/lineups")
SEASON = int(os.environ.get("SEASON", "2026"))
MAX_CALLS = int(os.environ.get("MAX_CALLS", "10"))
WINDOW = int(os.environ.get("CAPTURE_WINDOW_MIN", "75"))
LATE = int(os.environ.get("LATE_CAPTURE_MIN", "120"))
MAX_ENRICH = int(os.environ.get("MAX_ENRICH", "4"))
MIN_LEAD = 3
LEAGUES = [(323, "Indian Super League"), (39, "Premier League"), (140, "La Liga")]
GIVE_UP_H = 6
calls = 0


def now():
    return datetime.now(timezone.utc)


def dt(raw):
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def path(fid):
    return os.path.join(OUT, "%s.json" % fid)


def sched_path():
    return os.path.join(OUT, "schedule.json")


def read(path_, default=None):
    try:
        with open(path_, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write(path_, obj):
    os.makedirs(os.path.dirname(path_) or ".", exist_ok=True)
    tmp = path_ + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path_)


class Budget(Exception):
    pass


def get(path_, key, **params):
    global calls
    if calls >= MAX_CALLS:
        raise Budget()
    url = BASE + path_ + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-apisports-key": key})
    try:
            with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            calls += 1
            payload = json.loads(body)
            errs = payload.get("errors")
            if errs:
                raise SystemExit("API error: %s" % errs)
            return payload.get("response") or []
    except urllib.error.HTTPError as e:
        calls += 1
        if e.code == 429:
            raise Budget()
        raise SystemExit("HTTP %s from %s" % (e.code, path_))
    except urllib.error.URLError as e:
        raise SystemExit("network error: %s" % e.reason)


def key():
    k = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not k:
        raise SystemExit("API_FOOTBALL_KEY is not set")
    return k


def fetch_schedule(k):
    out = []
    for lid, lname in LEAGUES:
        try:
            rows = get("/fixtures", k, league=lid, season=SEASON, next=10)
        except Budget:
            break
        for row in rows:
            fx = row.get("fixture") or {}
            if not fx.get("date"):
                continue
            out.append({
                "fixture_id": fx.get("id"), "league_id": lid, "league": lname,
                "kickoff_utc": fx.get("date"),
                "home": ((row.get("teams") or {}).get("home") or {}).get("name"),
                "away": ((row.get("teams") or {}).get("away") or {}).get("name"),
            })
    seen, uniq = set(), []
    for f in out:
        if f["fixture_id"] in seen:
            continue
        seen.add(f["fixture_id"])
        uniq.append(f)
    return uniq


def cmd_schedule():
    k = key()
    fx = fetch_schedule(k)
    write(sched_path(), {"updated_utc": now().isoformat(), "season": SEASON,
                         "count": len(fx), "fixtures": fx})
    print("schedule: %d fixtures cached (%d calls)" % (len(fx), calls))


def load_schedule(k):
    cached = read(sched_path())
    if cached and cached.get("fixtures"):
        age = (now() - dt(cached.get("updated_utc"))).total_seconds() / 3600
        if age <= 24:
            print("cached schedule: %d fixtures, %.1fh old" % (len(cached["fixtures"]), age))
            return cached["fixtures"]
    print("schedule missing/stale - fetching")
    fx = fetch_schedule(k)
    if fx:
        write(sched_path(), {"updated_utc": now().isoformat(), "season": SEASON,
                             "count": len(fx), "fixtures": fx})
    return fx


def norm_lineups(raw):
    out = []
    for side in raw:
        team = side.get("team") or {}

        def players(lst):
            rows = []
            for slot in lst or []:
                p = slot.get("player") or {}
                rows.append({"player_id": p.get("id") or p.get("player_id"),
                             "name": p.get("name"), "number": p.get("number"),
                             "pos": p.get("pos")})
            return rows

        out.append({"team_id": team.get("id"), "team": team.get("name"),
                    "formation": side.get("formation"),
                    "coach": (side.get("coach") or {}).get("name"),
                    "startXI": players(side.get("startXI")),
                    "substitutes": players(side.get("substitutes"))})
    return out


def cmd_capture():
    k = key()
    t = now()
    horizon = t + timedelta(minutes=WINDOW)
    cands = []
    for f in load_schedule(k):
        ko = dt(f.get("kickoff_utc"))
        if ko is None or not (t - timedelta(hours=GIVE_UP_H) <= ko <= horizon):
            continue
        if os.path.exists(path(f["fixture_id"])):
            continue
        cands.append(dict(f, kickoff=ko))
    cands.sort(key=lambda c: c["kickoff"])
    print("%d fixture(s) in window" % len(cands))

    got = 0
    for c in cands:
        lead = (c["kickoff"] - now()).total_seconds() / 60
        if lead <= -LATE:
            print("  skip %s (%.0f min past KO)" % (c["fixture_id"], -lead))
            continue
        if os.path.exists(path(c["fixture_id"])):
            continue
        pre = lead >= MIN_LEAD
        try:
            lineups = get("/fixtures/lineups", k, fixture=c["fixture_id"])
        except Budget:
            print("  budget reached")
            break
        if not lineups:
            print("  %s no lineup yet (T-%.0f)" % (c["fixture_id"], lead))
            continue
        write(path(c["fixture_id"]), {
            "fixture_id": c["fixture_id"],
            "league": {"id": c["league_id"], "name": c["league"]},
            "season": SEASON, "kickoff_utc": c["kickoff"].isoformat(),
            "home": c["home"], "away": c["away"],
            "capture": {"confirmed": True, "pre_match": pre, "late": not pre,
                        "captured_utc": now().isoformat(),
                        "minutes_before_kickoff": round(lead, 1)},
            "lineups": norm_lineups(lineups), "result": None, "player_stats": None,
        })
        got += 1
        when = ("T-%.0f min" % lead) if lead >= 0 else ("%.0f min after KO" % -lead)
        print("  CAPTURED %s %s v %s at %s%s"
              % (c["fixture_id"], c["home"], c["away"], when, "" if pre else "  [late]"))
    print("captured %d, %d calls" % (got, calls))


def rating(raw):
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        return None


def as_int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def cmd_enrich():
    k = key()
    todo = []
    if not os.path.isdir(OUT):
        raise SystemExit("%s missing - run capture first" % OUT)
    for name in sorted(os.listdir(OUT)):
        if not name.endswith(".json"):
            continue
        p = os.path.join(OUT, name)
        rec = read(p)
        if not rec or not (rec.get("capture") or {}).get("confirmed"):
            continue
        if rec.get("player_stats"):
            continue
        ko = dt(rec.get("kickoff_utc"))
        if ko is None or now() < ko + timedelta(minutes=150):
            continue
        todo.append((ko, p, rec))
    todo.sort()
    todo = todo[:MAX_ENRICH]
    print("%d match(es) ready to enrich" % len(todo))

    for ko, p, rec in todo:
        fid = rec["fixture_id"]
        try:
            stats = get("/fixtures/players", k, fixture=fid)
        except Budget:
            print("  budget reached")
            break
        if not stats:
            print("  %s no stats yet" % fid)
            continue
        started = set()
        for side in rec.get("lineups") or []:
            for pl in side.get("startXI") or []:
                started.add(pl.get("player_id"))
        rows = []
        for block in stats:
            team = block.get("team") or {}
            for e in block.get("players") or []:
                pl = e.get("player") or {}
                st = (e.get("statistics") or [{}])[0] or {}
                g, go = st.get("games") or {}, st.get("goals") or {}
                rows.append({"player_id": pl.get("id"), "name": pl.get("name"),
                             "team": team.get("name"), "team_id": team.get("id"),
                             "minutes": g.get("minutes"), "rating": rating(g.get("rating")),
                             "goals": as_int(go.get("total")),
                             "assists": as_int(go.get("assists")),
                             "started": pl.get("id") in started})
        rec["player_stats"] = rows
        try:
            res = get("/fixtures", k, id=fid)
            if res:
                gl = res[0].get("goals") or {}
                stt = (res[0].get("fixture") or {}).get("status") or {}
                rec["result"] = {"status": stt.get("short"), "home_goals": gl.get("home"),
                                 "away_goals": gl.get("away")}
        except Budget:
            pass
        write(p, rec)
        print("  enriched %s: %d players" % (fid, len(rows)))
    print("done, %d calls" % calls)


def cmd_report():
    if not os.path.isdir(OUT):
        print("nothing captured yet")
        return
    n = enriched = pre = 0
    leads, by_league = [], {}
    for name in sorted(os.listdir(OUT)):
        if not name.endswith(".json"):
            continue
        rec = read(os.path.join(OUT, name))
        if not rec or not (rec.get("capture") or {}).get("confirmed"):
            continue
        cap = rec["capture"]
        n += 1
        enriched += 1 if rec.get("player_stats") else 0
        pre += 1 if cap.get("pre_match") else 0
        if cap.get("minutes_before_kickoff") is not None:
            leads.append(cap["minutes_before_kickoff"])
        lg = (rec.get("league") or {}).get("name", "?")
        by_league[lg] = by_league.get(lg, 0) + 1
    print("captured      : %d (%d pre-match, %d late)" % (n, pre, n - pre))
    print("enriched      : %d" % enriched)
    if leads:
        leads.sort()
        print("median lead   : %.0f min before kickoff" % leads[len(leads) // 2])
        print("earliest/latest: %.0f / %.0f" % (leads[0], leads[-1]))
    for lg, c in sorted(by_league.items(), key=lambda x: -x[1]):
        print("  %-24s %d" % (lg, c))
    if n < 50:
        print("\n%d matches is not a dataset yet - you need a full season." % n)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"schedule": cmd_schedule, "capture": cmd_capture,
     "enrich": cmd_enrich, "report": cmd_report}.get(cmd, cmd_report)()
