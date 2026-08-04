"""Match Kalshi MLB game-winner markets to Odds API games by team + date."""
import datetime
import logging
import re

log = logging.getLogger("match")

# Full name pieces -> canonical key. Covers The Odds API full names and
# the team names Kalshi uses in market titles/subtitles.
TEAM_KEYS = {
    "diamondbacks": "ARI", "braves": "ATL", "orioles": "BAL", "red sox": "BOS",
    "cubs": "CHC", "white sox": "CWS", "reds": "CIN", "guardians": "CLE",
    "rockies": "COL", "tigers": "DET", "astros": "HOU", "royals": "KC",
    "angels": "LAA", "dodgers": "LAD", "marlins": "MIA", "brewers": "MIL",
    "twins": "MIN", "mets": "NYM", "yankees": "NYY", "athletics": "OAK",
    "phillies": "PHI", "pirates": "PIT", "padres": "SD", "mariners": "SEA",
    "giants": "SF", "cardinals": "STL", "rays": "TB", "rangers": "TEX",
    "blue jays": "TOR", "nationals": "WSH",
}


# Alternate codes seen in tickers -> our canonical keys
TICKER_ALIASES = {
    "CHW": "CWS", "WAS": "WSH", "SDP": "SD", "SFG": "SF", "TBR": "TB",
    "KCR": "KC", "ANA": "LAA", "ATH": "OAK", "AZ": "ARI",
}
ALL_CODES = set(TEAM_KEYS.values()) | set(TICKER_ALIASES.keys())


def norm_code(code: str):
    return TICKER_ALIASES.get(code, code)


def team_key(name: str):
    if not name:
        return None
    n = name.lower()
    for frag, key in TEAM_KEYS.items():
        if frag in n:
            return key
    return None


def game_date(iso_ts: str):
    """Return the US/Eastern-ish calendar date for a commence timestamp."""
    try:
        dt = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        # shift 5h back so late-night UTC still maps to the US game date
        return (dt - datetime.timedelta(hours=5)).date()
    except Exception:
        return None


MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

# 2026 ticker format: KXMLBGAME-26JUL191920LADNYY-NYY
#   26JUL19 = game date, 1920 = start time (ET, optional in older tickers),
#   LADNYY = away+home codes concatenated, -NYY = the YES team.
# close_time is now a settlement deadline DAYS after the game — never use
# it for game-date matching.
TICKER_RE = re.compile(
    r"KXMLBGAME-(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z]{4,6})-([A-Z]{2,3})$")


def parse_ticker(ticker: str):
    """-> (date, away_code, home_code, yes_code) or None."""
    m = TICKER_RE.match(ticker or "")
    if not m:
        return None
    yy, mon, dd, _hhmm, blob, yes = m.groups()
    if mon not in MONTHS:
        return None
    try:
        d = datetime.date(2000 + int(yy), MONTHS[mon], int(dd))
    except ValueError:
        return None
    # split the AWAYHOME blob: codes are 2-3 chars; try both splits
    for i in (3, 2):
        a, h = blob[:i], blob[i:]
        if (a in ALL_CODES and h in ALL_CODES):
            return d, norm_code(a), norm_code(h), norm_code(yes)
    return None


def match_markets(kalshi_markets, fair_games):
    """Return list of quote targets:
    {ticker, yes_team, fair_prob, commence, title, ...}
    One Kalshi market = 'will TEAM win' -> YES prob = that team's fair prob.
    """
    # Index fair games by (US game date, frozenset of team keys)
    fair_idx = {}
    for g in fair_games:
        hk, ak = team_key(g["home"]), team_key(g["away"])
        d = game_date(g["commence"])
        if hk and ak and d:
            fair_idx[(d, frozenset((hk, ak)))] = g

    out = []
    for m in kalshi_markets:
        ticker = m.get("ticker", "")
        parsed = parse_ticker(ticker)
        if parsed:
            d, away, home, yes_team = parsed
            pair = frozenset((away, home))
        else:
            # legacy fallback: teams from title/ticker scan, date from
            # ticker if present, else close_time (old format only)
            title = (f"{m.get('title','')} {m.get('subtitle','')} "
                     f"{m.get('yes_sub_title','')}").lower()
            keys_found = []
            for frag, key in TEAM_KEYS.items():
                if frag in title and key not in keys_found:
                    keys_found.append(key)
            if len(keys_found) < 2:
                for k in re.findall(r"[A-Z]{2,3}", ticker):
                    if k in ALL_CODES and norm_code(k) not in keys_found:
                        keys_found.append(norm_code(k))
            if len(keys_found) < 2:
                continue
            pair = frozenset(keys_found[:2])
            dm = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker)
            if dm and dm.group(2) in MONTHS:
                d = datetime.date(2000 + int(dm.group(1)),
                                  MONTHS[dm.group(2)], int(dm.group(3)))
            else:
                d = game_date(m.get("close_time") or "")
            if not d:
                continue
            yes_team = None
            yst = (m.get("yes_sub_title") or "").lower()
            for frag, key in TEAM_KEYS.items():
                if frag in yst:
                    yes_team = key
                    break
            if not yes_team:
                sm = re.search(r"-([A-Z]{2,3})$", ticker)
                if sm and sm.group(1) in ALL_CODES:
                    yes_team = norm_code(sm.group(1))
            if not yes_team:
                continue

        if parsed:
            # 2026 tickers embed the exact game date — require an EXACT
            # date match. The ±1-day fallback made one odds game match
            # tickers on adjacent days, quoting (e.g.) Sunday's market
            # with Saturday's pitcher-dependent odds. Never again.
            game = fair_idx.get((d, pair))
        else:
            game = fair_idx.get((d, pair)) or fair_idx.get(
                (d + datetime.timedelta(days=1), pair)) or fair_idx.get(
                (d - datetime.timedelta(days=1), pair))
        if not game:
            continue

        hk, ak = team_key(game["home"]), team_key(game["away"])
        if yes_team == hk:
            fair = game["home_prob"]
        elif yes_team == ak:
            fair = game["away_prob"]
        else:
            continue

        out.append({
            "ticker": ticker,
            "yes_team": yes_team,
            "fair_prob": fair,
            "commence": game["commence"],
            "title": m.get("title", ticker),
            "sharp": game.get("sharp", False),
            "uncertainty": game.get("uncertainty", 0.0),
            "n_books": game.get("n_books", 0),
        })
    return out


# ── WNBA ladder matching (KXWNBASPREAD / KXWNBATOTAL) ─────────────────
# Ticker anatomy (verified against live markets):
#   KXWNBASPREAD-26JUL20WSHGS-WSH7   -> WSH wins by over 6.5 (suffix-0.5)
#   KXWNBATOTAL-26JUL20WSHGS-158     -> over 157.5 total points
# No time component; close_time unreliable (again) — ticker date only.
WNBA_TEAMS = {
    "atlanta": "ATL", "dream": "ATL", "chicago": "CHI", "sky": "CHI",
    "connecticut": "CONN", "sun": "CONN", "dallas": "DAL", "wings": "DAL",
    "golden state": "GS", "valkyries": "GS", "indiana": "IND",
    "fever": "IND", "las vegas": "LV", "aces": "LV",
    "los angeles": "LA", "sparks": "LA", "minnesota": "MIN", "lynx": "MIN",
    "new york": "NY", "liberty": "NY", "phoenix": "PHX", "mercury": "PHX",
    "portland": "POR", "fire": "POR", "seattle": "SEA", "storm": "SEA",
    "toronto": "TOR", "tempo": "TOR", "washington": "WSH", "mystics": "WSH",
}
WNBA_CODES = set(WNBA_TEAMS.values())

_WNBA_SPREAD_RE = re.compile(
    r"KXWNBASPREAD-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)-([A-Z]+?)(\d+)$")
_WNBA_TOTAL_RE = re.compile(
    r"KXWNBATOTAL-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)-(\d+)$")


def _wnba_key(name: str):
    n = (name or "").lower()
    for frag, code in WNBA_TEAMS.items():
        if frag in n:
            return code
    return None


def _split_wnba_blob(blob: str):
    """Team codes run 2-4 chars (GS, CONN); try every split."""
    for i in range(2, min(5, len(blob) - 1)):
        a, h = blob[:i], blob[i:]
        if a in WNBA_CODES and h in WNBA_CODES:
            return a, h
    return None, None


def match_wnba_ladders(spread_mkts, total_mkts, fair_games, dists_by_key):
    """Return quote targets for ladder rungs. dists_by_key caches fitted
    GameDists per (date, away, home) so a 10-rung ladder fits once."""
    import ladder
    try:
        from config import LADDER_MIN_TOTAL_LINES as _MIN_TOTAL, \
            LADDER_MIN_SPREAD_LINES as _MIN_SPREAD
    except Exception:
        _MIN_TOTAL, _MIN_SPREAD = 4, 2
    fair_idx = {}
    for g in fair_games:
        hk, ak = _wnba_key(g["home"]), _wnba_key(g["away"])
        d = game_date(g["commence"])
        if hk and ak and d:
            fair_idx[(d, ak, hk)] = g

    def dists_for(key, game):
        if key not in dists_by_key:
            dists_by_key[key] = ladder.GameDists(
                ladder.build_margin_points(game),
                ladder.build_total_points(game))
            dists_by_key[key]._n_spread = len(game.get("spread_lines", []))
            dists_by_key[key]._n_total = len(game.get("total_lines", []))
        return dists_by_key[key]

    out = []
    for m in spread_mkts:
        t = m.get("ticker", "")
        mm = _WNBA_SPREAD_RE.match(t)
        if not mm:
            continue
        yy, mon, dd, blob, team, k = mm.groups()
        if mon not in MONTHS:
            continue
        try:
            d = datetime.date(2000 + int(yy), MONTHS[mon], int(dd))
        except ValueError:
            continue
        away, home = _split_wnba_blob(blob)
        if not away or team not in (away, home):
            continue
        game = fair_idx.get((d, away, home))
        if not game:
            continue
        thresh = m.get("floor_strike")
        if thresh is None:
            thresh = int(k) - 0.5
        dist = dists_for((d, away, home), game)
        if getattr(dist, "_n_spread", 0) < _MIN_SPREAD:
            continue      # too few sharp lines -> shape would be prior-driven
        fair = dist.spread_fair(team_is_home=(team == home),
                                threshold=float(thresh))
        if fair is None:
            continue
        out.append({"ticker": t, "yes_team": team, "fair_prob": fair,
                    "commence": game["commence"],
                    "title": m.get("title", t), "sharp": True,
                    "uncertainty": game.get("uncertainty", 0.0),
                    "n_books": game.get("n_books", 0),
                    "event_key": t.rsplit("-", 1)[0], "ladder": "spread"})
    for m in total_mkts:
        t = m.get("ticker", "")
        mm = _WNBA_TOTAL_RE.match(t)
        if not mm:
            continue
        yy, mon, dd, blob, k = mm.groups()
        if mon not in MONTHS:
            continue
        try:
            d = datetime.date(2000 + int(yy), MONTHS[mon], int(dd))
        except ValueError:
            continue
        away, home = _split_wnba_blob(blob)
        if not away:
            continue
        game = fair_idx.get((d, away, home))
        if not game:
            continue
        thresh = m.get("floor_strike")
        if thresh is None:
            thresh = int(k) - 0.5
        dist = dists_for((d, away, home), game)
        if getattr(dist, "_n_total", 0) < _MIN_TOTAL:
            continue      # too few sharp lines -> shape would be prior-driven
        fair = dist.total_fair(float(thresh))
        if fair is None:
            continue
        out.append({"ticker": t, "yes_team": None, "fair_prob": fair,
                    "commence": game["commence"],
                    "title": m.get("yes_sub_title", t), "sharp": True,
                    "uncertainty": game.get("uncertainty", 0.0),
                    "n_books": game.get("n_books", 0),
                    "event_key": t.rsplit("-", 1)[0], "ladder": "total"})
    return out
