"""All tunable constants in one place. Every value is env-overridable.

Money values are CENTS unless the name says otherwise.
Change a value here (or set the env var on Railway) — nothing else to edit.
"""
import logging
import os


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _s(name: str, default: str) -> str:
    return os.environ.get(name, default)


# ── identity / endpoints ───────────────────────────────────────────────
KALSHI_BASE_URL   = _s("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_SERIES     = _s("KALSHI_SERIES", "KXMLBGAME")
DISCORD_WEBHOOK   = _s("DISCORD_WEBHOOK", "")

# ── risk caps ──────────────────────────────────────────────────────────
PER_MARKET_CAP    = _i("PER_MARKET_CAP", 2000)     # $20 max cost basis per market
TOTAL_CAP         = _i("TOTAL_CAP", 40000)         # $400 max total working capital
DAILY_LOSS_LIMIT  = _i("DAILY_LOSS_LIMIT", 6000)   # $60 realized loss -> halt for the day
DRAWDOWN_LIMIT    = _i("DRAWDOWN_LIMIT", 20000)    # $200 equity drawdown -> hard kill
START_BANKROLL    = _i("START_BANKROLL", 50000)    # $500; set to actual deposit

# ── quoting: edge & spread ─────────────────────────────────────────────
MIN_EDGE_CENTS    = _i("MIN_EDGE_CENTS", 3)   # required edge after fees, normal side
OFFSET_MIN_EDGE   = _i("OFFSET_MIN_EDGE", 1)  # relaxed edge floor on the side that FLATTENS inventory
MARGIN_CENTS      = _i("MARGIN_CENTS", 4)     # never post closer than this to fair
REQUOTE_MOVE      = _i("REQUOTE_MOVE", 2)     # cancel/re-quote if fair moved this many cents

# ── quoting: sizing ────────────────────────────────────────────────────
NO_SIZE_PCT       = _i("NO_SIZE_PCT", 60)     # % of per-market cap to the NO side
                                              # (retail overbets YES; surplus is on NO)

# ── timing windows (minutes to first pitch) ────────────────────────────
MAX_HOURS_OUT     = _i("MAX_HOURS_OUT", 48)   # don't quote games further out than this
WIDEN_MIN         = _i("WIDEN_MIN", 60)       # inside 60m: widen quotes (pre-game vol)
TIME_WIDEN_CENTS  = _i("TIME_WIDEN_CENTS", 2) # extra margin applied inside WIDEN_MIN
PULL_MIN          = _i("PULL_MIN", 30)        # inside 30m: pull all quotes entirely
                                              # (replaces old CUTOFF_MIN)

# ── uncertainty filter (book disagreement, prob points 0-1) ────────────
# stddev of devigged home-prob across books used for consensus
UNC_WIDEN         = _f("UNC_WIDEN", 0.015)    # >1.5c disagreement: widen quotes
UNC_WIDEN_CENTS   = _i("UNC_WIDEN_CENTS", 2)  # extra margin when widening
UNC_SKIP          = _f("UNC_SKIP", 0.030)     # >3c disagreement: skip market entirely
MIN_BOOKS         = _i("MIN_BOOKS", 2)        # need at least this many books for a fair

# ── inventory skew ─────────────────────────────────────────────────────
MAX_INV_SKEW_CENTS = _i("MAX_INV_SKEW_CENTS", 3)  # max cents of shading from inventory
# skew scales linearly with |position cost| / PER_MARKET_CAP up to the max.
FILL_COOLDOWN_SECS = _i("FILL_COOLDOWN_SECS", 600)  # pause re-quoting a filled side

# ── fees ───────────────────────────────────────────────────────────────
# Kalshi taker fee = ceil(0.07 * C * P * (1-P)). Most series charge resting
# (maker) orders nothing; 0.25 multiplier is a conservative default.
MAKER_FEE_MULT    = _f("MAKER_FEE_MULT", 0.25)

# ── odds api ───────────────────────────────────────────────────────────
ODDS_API_KEY      = _s("ODDS_API_KEY", "")
ODDS_SPORT        = _s("ODDS_SPORT", "baseball_mlb")
ODDS_REGIONS      = _s("ODDS_REGIONS", "us,eu")
# Credit math: calls cost (markets x regions-groups) = 2 credits with us,eu.
#   CACHE=90s -> ~57.6K credits/mo   CACHE=60s -> ~86.4K/mo (fits 100K tier
#   with room for the bet tracker)   CACHE=30s -> ~172K/mo (DOES NOT FIT).
ODDS_CACHE_SECS   = _i("ODDS_CACHE_SECS", 60)
STALE_FAIR_SECS   = _i("STALE_FAIR_SECS", 240)  # cancel everything if feed this stale
QUOTA_ALERT_REMAINING = _i("QUOTA_ALERT_REMAINING", 8000)  # Discord alert below this

# ── kalshi rate limits (basic tier: ~10 read/s, ~5 transactions/s) ─────
# We pace below the published caps. Verify your tier at
# https://trading-api.readme.io/reference/tiers if you upgrade.
READ_RPS          = _f("READ_RPS", 8.0)
WRITE_RPS         = _f("WRITE_RPS", 4.0)

# ── ops ────────────────────────────────────────────────────────────────
LOOP_SECS         = _i("LOOP_SECS", 30)
WATCHDOG_STALL_SECS = _i("WATCHDOG_STALL_SECS", 300)   # alert if loop silent 5 min
WATCHDOG_REALERT_SECS = _i("WATCHDOG_REALERT_SECS", 900)
PAPER_MODE        = os.environ.get("KILL", "") == "1"  # KILL=1 -> paper trading
ORDERBOOK_DEPTH   = _i("ORDERBOOK_DEPTH", 10)

# ── storage ────────────────────────────────────────────────────────────
DB_PATH = _s("MAKER_DB", "/data/maker.db")
if not os.path.isdir(os.path.dirname(DB_PATH) or "."):
    logging.getLogger("config").warning(
        f"{os.path.dirname(DB_PATH)} not mounted — falling back to ./maker.db "
        "(state will NOT survive redeploys; mount a Railway volume at /data)")
    DB_PATH = "./maker.db"

# ── edge research add-ons (v4.1) ───────────────────────────────────────
# Favorite-longshot bias (Bürgi/Deng/Whelan, 300K+ Kalshi contracts):
# low-priced contracts win LESS often than price implies (<10c contracts
# lose >60% of money); high-priced contracts earn small positive returns.
# So: demand extra edge when our bid would be a longshot price, and let
# favorite-side bids through at standard edge.
LONGSHOT_CENTS    = _i("LONGSHOT_CENTS", 25)     # a bid below this is a longshot
LONGSHOT_EXTRA_EDGE = _i("LONGSHOT_EXTRA_EDGE", 2)  # extra required edge there

# Steam guard: MLB fairs move fastest on lineup posts (~3-5h out) and
# pitcher scratches (20-30c swings). If OUR fair moved fast recently, the
# market is repricing — resting quotes are pick-off bait. Pause the market.
STEAM_WINDOW_SECS = _i("STEAM_WINDOW_SECS", 180)
STEAM_MOVE_CENTS  = _f("STEAM_MOVE_CENTS", 3.0)  # move within window -> pause
STEAM_PAUSE_SECS  = _i("STEAM_PAUSE_SECS", 240)

# ── v4.2 fair-value quality ────────────────────────────────────────────
# Multiplicative devig is known to overstate longshot probabilities on
# lopsided lines; the power method (solve k: pa^k + pb^k = 1) corrects it.
# Since Kalshi's longshot side is the trap side, sharper fairs on big
# favorites matter. Options: "power" | "multiplicative"
DEVIG_METHOD      = _s("DEVIG_METHOD", "power")
# Cross-book consensus: median is robust to one stale/outlier book
# dragging the fair. Options: "median" | "mean"
CONSENSUS         = _s("CONSENSUS", "median")

# ── v4.3 practitioner fixes ────────────────────────────────────────────
# REST polling staleness is the documented pick-off vector for Kalshi bots
# ("you're trading prices already stale when your order lands"). When any
# matched game is inside FAST_WINDOW_MIN of first pitch, tighten the loop.
FAST_WINDOW_MIN   = _i("FAST_WINDOW_MIN", 120)
FAST_LOOP_SECS    = _i("FAST_LOOP_SECS", 12)

# ── v4.5 fill optimization ─────────────────────────────────────────────
# Queue-position: pricing purely off fair often leaves us 1-2c below the
# best bid, where only deep sweeps reach us -> zero fills. When another
# maker is at/above our price, step up to best_bid+1 as long as the edge
# after fees still clears JOIN_MIN_EDGE. Buys top-of-queue with the
# minimum edge concession.
JOIN_BEST         = _i("JOIN_BEST", 1)          # 0 to disable
JOIN_MIN_EDGE     = _i("JOIN_MIN_EDGE", 2)      # edge floor for step-ups

# ── v4.6 pause switch ──────────────────────────────────────────────────
# Dashboard Pause/Resume button. Set DASH_TOKEN to any secret word to
# enable it; the button asks for the token. Without a token the control
# endpoint stays disabled (the dashboard URL is public).
DASH_TOKEN        = _s("DASH_TOKEN", "")

# ── v4.9 queue preservation ────────────────────────────────────────────
# Every cancel/repost surrenders our place in line — and queue position
# is the #1 fill factor. Hold a resting order through price drift of up
# to this many cents; only genuine moves re-quote. (13.7K churned posts
# in one night taught this lesson.)
PRICE_TOLERANCE   = _i("PRICE_TOLERANCE", 1)

# ── v5.0 profitable pennying ───────────────────────────────────────────
# Research: front-of-queue earns, back-of-queue breaks even — but a jump
# costs a full tick of edge, so it must buy something real.
# Rules: JUMP over a level only when the depth ahead makes waiting
# expensive; otherwise JOIN it and keep the tick. Never escalate a penny
# war against a faster bot — stand down and rest deep instead.
JUMP_QUEUE_MIN    = _i("JUMP_QUEUE_MIN", 100)   # contracts ahead to justify a jump
PENNY_WAR_MAX     = _i("PENNY_WAR_MAX", 3)      # step-ups on one market/side...
PENNY_WAR_WINDOW  = _i("PENNY_WAR_WINDOW", 240) # ...within this window = a war
PENNY_WAR_PAUSE   = _i("PENNY_WAR_PAUSE", 600)  # stand down this long

# ── v5.1 WNBA ladder markets ───────────────────────────────────────────
# 4-9c spreads, thin touches, daily games: the pond the scanner picked.
WNBA_ENABLED      = _i("WNBA_ENABLED", 1)
WNBA_ODDS_SPORT   = _s("WNBA_ODDS_SPORT", "basketball_wnba")
WNBA_SERIES_SPREAD = _s("WNBA_SERIES_SPREAD", "KXWNBASPREAD")
WNBA_SERIES_TOTAL  = _s("WNBA_SERIES_TOTAL", "KXWNBATOTAL")
LADDER_RUNG_CAP   = _i("LADDER_RUNG_CAP", 1500)  # $15/rung (was $10; +1.93c CLV earned it)
LADDER_RUNG_MAX_CONTRACTS = _i("LADDER_RUNG_MAX_CONTRACTS", 60)  # hard
# contract ceiling per rung — a $15 cap on a 4c longshot would otherwise
# buy 375 contracts; cap the COUNT too so cheap rungs can't pile up.
# LADDER_MIN_FAIR / MAX_FAIR / edge / margin / PER_EVENT_CAP defined in the
# v5.3 block below (single source of truth)
# (per-event cap defined below in the v5.3 block — rungs correlate, so
# the cap is enforced per GAME across all its rungs)
# Odds API budget: MLB+WNBA both at 180s cache with h2h,spreads,totals
# for WNBA (us region) lands ~72K credits/mo — inside the 100K plan.

# ── v5.3 fill expansion (edge preserved) ───────────────────────────────
# WNBA maker fills are FEE-FREE and we're collecting spread. Expand
# coverage without lowering the edge floor:
#  - widen the tradeable rung band (more strikes per ladder in play)
#  - a dedicated, higher edge floor for ladders (thin books let us keep
#    more edge than the 1c MLB war floor — take it)
#  - raise per-event cap now that we know fills are real & fee-free
LADDER_MIN_FAIR   = _f("LADDER_MIN_FAIR", 0.08)   # was 0.12 — more rungs live
LADDER_MAX_FAIR   = _f("LADDER_MAX_FAIR", 0.92)   # was 0.88
LADDER_MIN_EDGE   = _i("LADDER_MIN_EDGE", 3)      # ladder-specific floor
LADDER_MARGIN     = _i("LADDER_MARGIN", 3)        # min cents off fair
PER_EVENT_CAP     = _i("PER_EVENT_CAP", 9000)     # $90/game (was 60; measured scale-up)
# Distribution confidence gate: only quote a ladder if the fit used >=2
# real spread/total lines (not just moneyline) — thin fits misprice tails.
LADDER_MIN_LINES  = _i("LADDER_MIN_LINES", 1)
# v6.1: ENFORCED min sharp lines per ladder before we quote it. With the
# alternate-ladder pull, a healthy game gives many total/spread strikes; if
# alternates fail to return we fall back to ~1 line and the fit shape becomes
# prior-driven again -- the exact totals bias. Better to SKIP such a game than
# quote a prior. Totals were the biased side, so require a real ladder (>=4);
# spreads were already calibrated on sparse data, so a light floor (>=2).
LADDER_MIN_TOTAL_LINES  = _i("LADDER_MIN_TOTAL_LINES", 4)
LADDER_MIN_SPREAD_LINES = _i("LADDER_MIN_SPREAD_LINES", 2)

# ── v5.4 WNBA-first allocation ─────────────────────────────────────────
# MLB game markets fill ~nothing (institutional 1c walls); WNBA ladders
# are where fills happen. Shift capital and slots toward WNBA.
#   MLB_ENABLED=0     -> stop quoting MLB entirely (pure WNBA)
#   MLB_MAX_MARKETS   -> cap how many MLB markets can rest at once
#   MLB_TOTAL_CAP     -> hard dollar cap on ALL resting MLB exposure
# WNBA keeps the full TOTAL_CAP minus whatever MLB is using.
MLB_ENABLED       = _i("MLB_ENABLED", 1)
MLB_MAX_MARKETS   = _i("MLB_MAX_MARKETS", 4)     # was effectively unlimited
MLB_TOTAL_CAP     = _i("MLB_TOTAL_CAP", 8000)    # $80 max across all MLB

# ── v5.6 logit-space inventory skew (Avellaneda-Stoikov, opt-in) ───────
# Our cent-based skew shades a flat N cents at any price, but a cent is
# ~2% of the odds at 50c and ~12% at 8c. The prediction-market A&S
# adaptation (arXiv 2510.15205) skews in log-odds instead: scale-free,
# and it decays as settlement approaches (less time for the price to move
# against inventory). OFF by default -- turn on and compare CLV.
LOGIT_SKEW        = _i("LOGIT_SKEW", 0)        # 1 = use logit skew
LOGIT_GAMMA       = _f("LOGIT_GAMMA", 0.35)    # risk aversion
LOGIT_SIGMA       = _f("LOGIT_SIGMA", 1.10)    # belief vol (logit units)
LOGIT_HORIZON_MIN = _f("LOGIT_HORIZON_MIN", 2880.0)  # tau=1 at 48h out

# ── v6.0 fat-tailed ladder pricer (the model-bias fix) ─────────────────
# The Normal pricer has tails that decay as exp(-x^2/2) — too thin for
# basketball margins/totals, so it UNDERPRICES big overs and big covers.
# Measured live: 42% win rate vs ~70% break-even, p~0.4% the fairs were
# correctly priced. Switching the fitted tail to Student-t fattens exactly
# those tails while still matching interior sharp lines. Normal is the
# df->inf special case, so the closed-form fit is unchanged.
#
# These are read directly by ladder.py (kept local there, like SIGMA_PRIOR
# always was). Documented here so the tunable surface stays discoverable.
#   LADDER_DIST   "normal" (default, = current live behavior) | "t"
#   LADDER_T_DF   Student-t degrees of freedom (fat tails). ~5-7 realistic;
#                 TUNE against calibrate.py until TOTAL/SPREAD bias -> 0.
#   SIGMA_WNBA_TOTAL / SIGMA_WNBA_MARGIN  point std-dev priors (only bind on
#                 moneyline-only fits). Fattened 13.5->16.0 / 11.5->13.0.
# Validation path: leave normal live, flip LADDER_DIST=t in PAPER mode
# (KILL=1), let it log fairs+settlements, then `python3 calibrate.py --sim`
# to confirm the tail bias is gone before going live. Nothing here changes
# production until you set LADDER_DIST=t.
LADDER_DIST_DOC = "see ladder.py: LADDER_DIST / LADDER_T_DF"
