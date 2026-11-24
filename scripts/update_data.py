#!/usr/bin/env python3
"""
Refresh data/isl.json with the current ISL season fixtures + results.

The HUD reads this file on load (only when served over http/https, e.g. GitHub
Pages) and rebuilds standings, Elo and the remaining-fixture list from it.

Run by .github/workflows/update-isl.yml once a day, or by hand:

    GOAL_API_KEY=... LEAGUE_ID=... python3 scripts/update_data.py

Environment:
    GOAL_API_KEY   your GOAL API key (gapi_...)      [source=goal]
    LEAGUE_ID      GOAL API league id                [default below]
    DATA_SOURCE    "goal" (default) or "thesportsdb"
    OUT_PATH       output path                       [data/isl.json]

Nothing secret is written to the output file.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GOAL_BASE = "https://api.goal-api.com/v1"
DEFAULT_LEAGUE_ID = "cmr77dvry008arx06q35z3h2m"
THESPORTSDB_LEAGUE_ID = "4791"          # Indian Super League
THESPORTSDB_SEASON = "2026-2027"
OUT_PATH = os.environ.get("OUT_PATH", "data/isl.json")

# Statuses that mean "not played yet" - the HUD clears the score for these.
UPCOMING = {"scheduled", "not_started", "ns", "timed", "upcoming", "tbd", "postponed", ""}


def http_json(url, headers=None, timeout=45):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"HTTP {e.code} from {url}\n{body}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error reaching {url}: {e.reason}")


# --------------------------------------------------------------------------- #
# GOAL API
# --------------------------------------------------------------------------- #
def fetch_goal(key, league_id):
    out, offset, limit = [], 0, 50
    for _ in range(15):
        url = f"{GOAL_BASE}/leagues/{urllib.parse.quote(league_id)}/fixtures?limit={limit}&offset={offset}"
        payload = http_json(url, {"Authorization": f"Bearer {key}", "Accept": "application/json"})
        batch = payload if isinstance(payload, list) else (
            payload.get("data") or payload.get("fixtures") or payload.get("events") or []
        )
        if not batch:
            break
        out.extend(batch)
        more = (payload.get("pagination") or {}).get("hasMore") if isinstance(payload, dict) else None
        if more is False or (more is None and len(batch) < limit):
            break
        offset += limit
    return out


def _name(obj):
    if isinstance(obj, dict):
        return (obj.get("name") or obj.get("shortName") or obj.get("title") or "").strip()
    return str(obj or "").strip()


def _score(raw):
    """Pull (home, away) goals out of the shapes we have seen so far."""
    if isinstance(raw, dict):
        s = raw.get("score") if isinstance(raw.get("score"), dict) else raw
        h = s.get("home", s.get("homeTeamScore", s.get("home_goals")))
        a = s.get("away", s.get("awayTeamScore", s.get("away_goals")))
        return h, a
    return None, None


def normalise_goal(rows):
    """Map GOAL API rows onto the schema the HUD understands."""
    fixtures, skipped = [], 0
    for m in rows:
        home = _name(m.get("homeTeam") or m.get("home_team") or m.get("home"))
        away = _name(m.get("awayTeam") or m.get("away_team") or m.get("away"))
        ko = (m.get("utcDate") or m.get("kickoff_utc") or m.get("kickoffUtc")
              or m.get("date") or m.get("startTime") or "")
        status = str(m.get("matchStatus") or m.get("status") or "").strip().lower()
        hg, ag = _score(m)
        if not (home and away and ko):
            skipped += 1
            continue
        played = status not in UPCOMING and hg is not None and ag is not None
        fixtures.append({
            "home": home,
            "away": away,
            "utcDate": ko,
            "matchStatus": "FINISHED" if played else "SCHEDULED",
            "score": {"home": int(hg) if played else None, "away": int(ag) if played else None},
            "season": (m.get("season") or {}).get("name") if isinstance(m.get("season"), dict) else m.get("season"),
        })
    return fixtures, skipped


# --------------------------------------------------------------------------- #
# TheSportsDB (free fallback, no key needed)
# --------------------------------------------------------------------------- #
def fetch_thesportsdb():
    base = f"https://www.thesportsdb.com/api/v1/json/123"
    out = []
    for endpoint in ("eventspastleague.php?id=" + THESPORTSDB_LEAGUE_ID,
                     "eventsnextleague.php?id=" + THESPORTSDB_LEAGUE_ID):
        try:
            payload = http_json(base + "/" + endpoint)
        except SystemExit as e:
            print(f"  warn: {endpoint} failed ({e})", file=sys.stderr)
            continue
        events = payload.get("events") if isinstance(payload, dict) else None
        if events:
            out.extend(events)
    return out


def normalise_thesportsdb(rows):
    fixtures = []
    for m in rows:
        if str(m.get("strSport", "")).lower() != "soccer":
            continue
        ko = m.get("strTimestamp") or (
            f"{m.get('dateEvent','')}T{m.get('strTime','00:00:00')}Z" if m.get("dateEvent") else ""
        )
        hg, ag = m.get("intHomeScore"), m.get("intAwayScore")
        played = hg is not None and ag is not None and str(hg).isdigit() and str(ag).isdigit()
        fixtures.append({
            "home": m.get("strHomeTeam", ""),
            "away": m.get("strAwayTeam", ""),
            "utcDate": ko,
            "matchStatus": "FINISHED" if played else "SCHEDULED",
            "score": {"home": int(hg) if played else None, "away": int(ag) if played else None},
            "season": m.get("strSeason"),
        })
    return fixtures


# --------------------------------------------------------------------------- #
def main():
    source = os.environ.get("DATA_SOURCE", "goal").lower()
    if source == "goal":
        key = os.environ.get("GOAL_API_KEY", "").strip()
        if not key:
            raise SystemExit("GOAL_API_KEY is not set. Add it as a repository secret.")
        league_id = os.environ.get("LEAGUE_ID", DEFAULT_LEAGUE_ID)
        print(f"Fetching GOAL API league {league_id} ...")
        raw = fetch_goal(key, league_id)
        fixtures, skipped = normalise_goal(raw)
        if skipped:
            print(f"  {skipped} rows skipped (no team names or kickoff time)")
    elif source == "thesportsdb":
        print("Fetching TheSportsDB (free key) ...")
        raw = fetch_thesportsdb()
        fixtures = normalise_thesportsdb(raw)
    else:
        raise SystemExit(f"Unknown DATA_SOURCE '{source}'")

    if len(fixtures) < 5:
        raise SystemExit(
            f"Only {len(fixtures)} usable fixtures came back - refusing to overwrite good data.\n"
            "Check the league id, the API key, or the response schema."
        )

    finished = sum(1 for f in fixtures if f["matchStatus"] == "FINISHED")
    payload = {
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "count": len(fixtures),
        "finished": finished,
        "fixtures": fixtures,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    print(f"Wrote {OUT_PATH}: {len(fixtures)} fixtures ({finished} finished) from {source}")


if __name__ == "__main__":
    main()
