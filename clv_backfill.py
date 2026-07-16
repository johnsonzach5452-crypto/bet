"""One-off CLV backfill — pulls historical closing lines from The Odds API
for settled game-level bets (moneyline / total / spread) and writes CLV
into the bets DB. Run /resync afterwards to push CLV columns to the sheet.

Usage (Railway console on the bet service):
    pip install requests --break-system-packages -q
    python3 clv_backfill.py --dry-run          # shows plan + cost estimate
    python3 clv_backfill.py                    # runs it
    python3 clv_backfill.py --max-credits 1500 # optional spend cap
"""
import argparse
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

DB_PATH = os.environ.get("DB_PATH", "/data/bets.db")
API_KEY = os.environ.get("ODDS_API_KEY", "")
BASE = "https://api.the-odds-api.com/v4"

LEAGUE_KEYS = {
    "MLB": "baseball_mlb", "NBA": "basketball_nba", "WNBA": "basketball_wnba",
    "NFL": "americanfootball_nfl", "NCAAF": "americanfootball_ncaaf",
    "NCAAB": "basketball_ncaab", "NHL": "icehockey_nhl",
    "EPL": "soccer_epl", "MLS": "soccer_usa_mls",
}
SPORT_KEYS = {"baseball": "baseball_mlb", "basketball": "basketball_nba",
              "football": "americanfootball_nfl", "hockey": "icehockey_nhl"}

TOTAL_RE = re.compile(r"\b(over|under)\s*([0-9]+(?:\.5)?)", re.I)
SPREAD_RE = re.compile(r"([+-][0-9]+(?:\.5)?)")

credits_used = 0


def sport_key(sport, league):
    if league and str(league).upper().strip() in LEAGUE_KEYS:
        return LEAGUE_KEYS[str(league).upper().strip()]
    if sport and str(sport).lower().strip() in SPORT_KEYS:
        return SPORT_KEYS[str(sport).lower().strip()]
    return None


def implied(a):
    a = float(a)
    return (-a / (-a + 100)) if a < 0 else (100 / (a + 100))


def to_american(o):
    o = float(o)
    if abs(o) >= 100:
        return o
    if 1 <= o < 100:
        p = o / 100
        return -round(p / (1 - p) * 100) if p >= 0.5 else round((1 - p) / p * 100)
    if 0 < o < 1:
        return -round(o / (1 - o) * 100) if o >= 0.5 else round((1 - o) / o * 100)
    return o


def prob_to_american(p):
    if p <= 0 or p >= 1:
        return None
    return round(-p / (1 - p) * 100) if p >= 0.5 else round((1 - p) / p * 100)


def frag(team):
    return (team or "").strip().split(" ")[-1].lower()


def get(url, params, cost_note=""):
    global credits_used
    params["apiKey"] = API_KEY
    r = requests.get(url, params=params, timeout=20)
    used = r.headers.get("x-requests-last", "?")
    try:
        credits_used += int(float(used))
    except (ValueError, TypeError):
        pass
    if r.status_code == 401:
        sys.exit("401 — key invalid or historical access not on your plan")
    if r.status_code == 422:
        return None  # no snapshot at that time
    if r.status_code != 200:
        print(f"  ! {r.status_code} {cost_note}: {r.text[:120]}")
        return None
    return r.json()


def consensus(data, market, pick):
    probs = []
    for bk in (data or {}).get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != market:
                continue
            for oc in mkt.get("outcomes", []):
                if market == "h2h" and oc.get("name") == pick:
                    probs.append(implied(oc["price"]))
                elif market in ("totals", "spreads") and \
                        oc.get("name") == pick[0] and oc.get("point") == pick[1]:
                    probs.append(implied(oc["price"]))
    # strict: require at least 2 books quoting this exact market/point
    if len(probs) < 2:
        return None
    return prob_to_american(sum(probs) / len(probs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-credits", type=int, default=4000)
    args = ap.parse_args()

    if not API_KEY:
        sys.exit("ODDS_API_KEY env var not set")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    bets = [dict(r) for r in con.execute(
        "SELECT * FROM bets WHERE status IN ('won','lost','push') "
        "AND clv IS NULL AND odds IS NOT NULL "
        "AND lower(bet_type) IN ('moneyline','total','spread')")]
    print(f"{len(bets)} settled game-level bets missing CLV")

    # group bets by (sport_key, date) using created_at
    by_day = {}
    skipped = 0
    for b in bets:
        sk = sport_key(b.get("sport"), b.get("league"))
        d = (b.get("created_at") or "")[:10]
        if not sk or not d:
            skipped += 1
            continue
        by_day.setdefault((sk, d), []).append(b)

    est = len(by_day) * 1 + sum(1 for _ in bets) * 30  # events lists + per-event odds (upper bound)
    print(f"{len(by_day)} sport-days to scan · worst-case ~{est} credits "
          f"(cap {args.max_credits}) · skipped {skipped} (unmapped sport)")
    if args.dry_run:
        for (sk, d), bs in sorted(by_day.items()):
            print(f"  {d} {sk}: {len(bs)} bets")
        return

    done = failed = 0
    for (sk, day), day_bets in sorted(by_day.items()):
        if credits_used >= args.max_credits:
            print(f"credit cap reached ({credits_used}), stopping")
            break
        # events list snapshot at noon UTC that day (cheap)
        events = get(f"{BASE}/historical/sports/{sk}/events",
                     {"date": f"{day}T12:00:00Z"}, f"events {day}")
        ev_list = (events or {}).get("data", []) if isinstance(events, dict) else (events or [])
        if not ev_list:
            failed += len(day_bets)
            continue

        odds_cache = {}
        for b in day_bets:
            if credits_used >= args.max_credits:
                break
            desc = (b.get("description") or "").lower()
            btype = (b.get("bet_type") or "").lower()
            cands = [e for e in ev_list
                     if frag(e.get("home_team")) in desc
                     or frag(e.get("away_team")) in desc]
            if len(cands) != 1:          # 0 = no match, 2+ = ambiguous -> don't count
                failed += 1
                continue
            ev = cands[0]
            eid = ev["id"]
            if eid not in odds_cache:
                # snapshot at commence time = the closing line
                odds_cache[eid] = get(
                    f"{BASE}/historical/sports/{sk}/events/{eid}/odds",
                    {"date": ev["commence_time"], "regions": "us",
                     "markets": "h2h,totals,spreads", "oddsFormat": "american"},
                    f"odds {eid}")
                time.sleep(0.4)
            snap = odds_cache[eid]
            data = snap.get("data") if isinstance(snap, dict) else None
            if not data:
                failed += 1
                continue

            price = None
            hf, af = frag(ev.get("home_team")), frag(ev.get("away_team"))
            both_named = (hf in desc) and (af in desc)
            if btype == "moneyline":
                if both_named:           # can't be sure which side -> don't count
                    failed += 1
                    continue
                team = ev["home_team"] if hf in desc else ev["away_team"]
                price = consensus(data, "h2h", team)
            elif btype == "total":
                m = TOTAL_RE.search(desc)
                if m:
                    price = consensus(data, "totals",
                                      (m.group(1).capitalize(), float(m.group(2))))
            elif btype == "spread":
                m = SPREAD_RE.search(b.get("description") or "")
                if m and not both_named:  # ambiguous side -> don't count
                    team = ev["home_team"] if hf in desc else ev["away_team"]
                    price = consensus(data, "spreads", (team, float(m.group(1))))
            if price is None:
                failed += 1
                continue

            entry = to_american(b["odds"])
            clv = round((implied(price) - implied(entry)) * 100, 1)
            if abs(clv) > 20:            # implausible -> likely bad match, don't count
                failed += 1
                print(f"  ? skipped (CLV {clv:+.1f} implausible): {b['description'][:50]}")
                continue
            con.execute("UPDATE bets SET closing_odds=?, clv=? WHERE message_id=?",
                        (price, clv, b["message_id"]))
            con.commit()
            done += 1
            print(f"  ✓ {b['description'][:50]:<50} entry {entry:>5} close {price:>5} "
                  f"CLV {clv:+.1f}")

    print(f"\nDone: {done} backfilled, {failed} unmatched · ~{credits_used} credits used")
    print("Run /resync in Discord to push CLV columns to the Google Sheet.")


if __name__ == "__main__":
    main()
