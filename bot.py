import os
import re
import json
import types
import base64
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from itertools import takewhile

CENTRAL = ZoneInfo("America/Chicago")

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
from anthropic import Anthropic
from dotenv import load_dotenv

try:
    import gspread
    from google.oauth2.service_account import Credentials
    SHEETS_AVAILABLE = True
except ImportError:
    SHEETS_AVAILABLE = False

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DB_PATH = os.environ.get("DB_PATH", "bets.db")
# If True, anyone can settle a bet by reacting, not just the original poster
ALLOW_ANYONE_TO_SETTLE = os.environ.get("ALLOW_ANYONE_TO_SETTLE", "false").lower() == "true"

# GROUPS lets you run multiple totally separate bet-tracking setups in one bot/server.
# Each group has its own intake channel, output channel, and stats channel, and its
# stats/leaderboard never mix with another group's.
#
# Format (single line JSON):
# [{"name":"Jack","bet_channel_id":"123","output_channel_id":"456","stats_channel_id":"789"},
#  {"name":"Friend","bet_channel_id":"111","output_channel_id":"222","stats_channel_id":"333"}]
#
# Backward-compatible fallback: if GROUPS isn't set, build one group called "default"
# from the older BET_CHANNEL_ID / OUTPUT_CHANNEL_ID / STATS_CHANNEL_ID variables.
GROUPS_RAW = os.environ.get("GROUPS")
if GROUPS_RAW:
    try:
        GROUPS = json.loads(GROUPS_RAW)
    except json.JSONDecodeError:
        logging.getLogger("betbot").exception("Failed to parse GROUPS env var as JSON")
        GROUPS = []
else:
    GROUPS = []
    _legacy_bet = os.environ.get("BET_CHANNEL_ID")
    _legacy_output = os.environ.get("OUTPUT_CHANNEL_ID")
    _legacy_stats = os.environ.get("STATS_CHANNEL_ID")
    if _legacy_bet or _legacy_output or _legacy_stats:
        GROUPS.append(
            {
                "name": "default",
                "bet_channel_id": _legacy_bet,
                "output_channel_id": _legacy_output,
                "stats_channel_id": _legacy_stats,
            }
        )

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("betbot")

# Google Sheets integration (optional)
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
_sheets_client = None
_calendar_built = {}

# Monitor channel — single channel that reports bot health, errors, and activity.
# Set MONITOR_CHANNEL_ID in Railway Variables to a dedicated #bot-status channel.
MONITOR_CHANNEL_ID = os.environ.get("MONITOR_CHANNEL_ID")
_monitor_status_message_id: str | None = None  # ID of the pinned status message we keep editing

SHEET_HEADERS = [
    "Date Posted (CT)",    # A  1
    "Date Settled (CT)",   # B  2
    "Username",            # C  3
    "Group",               # D  4
    "Description",         # E  5
    "Sport",               # F  6
    "League",              # G  7
    "Sportsbook",          # H  8
    "Bet Type",            # I  9
    "Prop Category",       # J  10
    "Legs (#)",            # K  11
    "Odds",                # L  12
    "Stake ($)",           # M  13
    "To Win ($)",          # N  14
    "Status",              # O  15
    "Profit ($)",          # P  16
    "ROI %",               # Q  17
    "Cumulative P&L ($)",  # R  18
    "Streak",              # S  19
    "Message ID",          # T  20
    "Live Odds",           # U  21
    "Closing Odds",        # V  22
    "CLV (pts)",           # W  23
    "Game Time",           # X  24
]

# Column index constants (1-based for gspread)
COL_SETTLED    = 2
COL_STATUS     = 15
COL_PROFIT     = 16
COL_ROI        = 17
COL_CUMULATIVE = 18
COL_STREAK     = 19
COL_MSG_ID     = 20


def _c(r, g, b):
    """Convert 0-255 RGB to Sheets API 0-1 format."""
    return {"red": r/255, "green": g/255, "blue": b/255}


# ── Monitor channel helpers ──────────────────────────────────────────────────

LEVEL_COLORS = {
    "ok":      0x2ECC71,   # green
    "info":    0x3498DB,   # blue
    "warning": 0xF5A623,   # amber
    "error":   0xE74C3C,   # red
}
LEVEL_ICONS = {
    "ok":      "✅",
    "info":    "ℹ️",
    "warning": "⚠️",
    "error":   "🔴",
}


async def post_monitor(title: str, body: str = "", level: str = "info"):
    """Post a notification to the monitor channel. Silent if MONITOR_CHANNEL_ID not set."""
    if not MONITOR_CHANNEL_ID:
        return
    channel = bot.get_channel(int(MONITOR_CHANNEL_ID))
    if channel is None:
        return
    icon = LEVEL_ICONS.get(level, "ℹ️")
    embed = discord.Embed(
        title=f"{icon}  {title}",
        description=body or None,
        color=LEVEL_COLORS.get(level, 0x3498DB),
    )
    embed.set_footer(text=datetime.now(CENTRAL).strftime("%b %d  %I:%M %p CT"))
    try:
        await channel.send(embed=embed)
    except Exception:
        log.exception("post_monitor failed")


async def update_status_message():
    """Edit (or create) a single pinned status message in the monitor channel.
    Persists the message ID in the DB so restarts reuse the same message."""
    global _monitor_status_message_id
    if not MONITOR_CHANNEL_ID:
        return
    channel = bot.get_channel(int(MONITOR_CHANNEL_ID))
    if channel is None:
        return

    # Load persisted message ID from DB if we don't have it in memory
    if not _monitor_status_message_id:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT value FROM monitor_state WHERE key = 'status_message_id'"
            )
            row = await cur.fetchone()
            if row:
                _monitor_status_message_id = row[0]

    # Gather stats
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM bets WHERE status='pending'")
        (pending_total,) = await cur.fetchone()
        cur2 = await db.execute("SELECT COUNT(*) FROM bets WHERE status!='pending'")
        (settled_total,) = await cur2.fetchone()
        cur3 = await db.execute("SELECT last_run FROM audit_schedule WHERE key='last_audit'")
        audit_row = await cur3.fetchone()

    last_audit = "Never"
    if audit_row:
        try:
            dt = datetime.fromisoformat(audit_row[0])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            last_audit = dt.astimezone(CENTRAL).strftime("%b %d  %I:%M %p CT")
        except Exception:
            pass

    sheets_ok = _sheets_client is not None
    pending_conf = len(_awaiting_confirmation)

    lines = [
        f"**Groups:** {len(GROUPS)}  ·  **Bets tracked:** {settled_total}  ·  **Pending:** {pending_total}",
        f"**Google Sheets:** {'🟢 Connected' if sheets_ok else '🔴 Not connected'}",
        f"**Last audit:** {last_audit}",
    ]
    if pending_conf:
        lines.append(f"**Awaiting confirmation:** {pending_conf} bet(s) need review")

    embed = discord.Embed(
        title="📡  Bot Status",
        description="\n".join(lines),
        color=0x2ECC71 if sheets_ok else 0xF5A623,
    )
    embed.set_footer(text=f"Updated {datetime.now(CENTRAL).strftime('%b %d  %I:%M %p CT')}  ·  /status to refresh")

    try:
        if _monitor_status_message_id:
            try:
                msg = await channel.fetch_message(int(_monitor_status_message_id))
                await msg.edit(embed=embed)
                return
            except (discord.NotFound, discord.HTTPException):
                _monitor_status_message_id = None  # message gone — create a new one

        new_msg = await channel.send(embed=embed)
        _monitor_status_message_id = str(new_msg.id)

        # Persist so restarts reuse the same message
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO monitor_state (key, value) VALUES ('status_message_id', ?)",
                (_monitor_status_message_id,),
            )
            await db.commit()

        try:
            await new_msg.pin()
        except discord.HTTPException:
            pass
    except Exception:
        log.exception("update_status_message failed")


def _apply_data_sheet_formatting(ss, ws):
    """Apply header colors, row conditional formatting, number formats, column widths."""
    sid = ws.id
    reqs = []

    # ── Header row ──────────────────────────────────────────────────────
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 24},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _c(26, 26, 46),
            "textFormat": {"foregroundColor": _c(255, 255, 255), "bold": True, "fontSize": 10},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
    }})

    # ── Column widths ───────────────────────────────────────────────────
    widths = [140,140,100,80,300,100,70,110,90,130,55,70,80,80,75,85,70,120,70,160,80,90,75,130]
    for i, w in enumerate(widths):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i+1},
            "properties": {"pixelSize": w}, "fields": "pixelSize"
        }})

    # ── Number formats ──────────────────────────────────────────────────
    # Stake, To Win, Profit, Cumulative P&L → currency
    for start, end in [(12, 14), (15, 16), (17, 18)]:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2000,
                      "startColumnIndex": start, "endColumnIndex": end},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": '"$"#,##0.00'}}},
            "fields": "userEnteredFormat.numberFormat"
        }})
    # ROI %
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2000,
                  "startColumnIndex": 16, "endColumnIndex": 17},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": '#,##0.0"%"'}}},
        "fields": "userEnteredFormat.numberFormat"
    }})

    # ── Row conditional formatting by status ────────────────────────────
    row_range = {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2000,
                 "startColumnIndex": 0, "endColumnIndex": 20}
    status_styles = [
        ("won",     _c(198, 239, 206), _c(0, 97, 0)),
        ("lost",    _c(255, 199, 206), _c(156, 0, 6)),
        ("push",    _c(220, 220, 220), _c(80, 80, 80)),
        ("pending", _c(255, 242, 204), _c(100, 80, 0)),
    ]
    for status, bg, fg in status_styles:
        reqs.append({"addConditionalFormatRule": {"rule": {
            "ranges": [row_range],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue": f'=$O2="{status}"'}]},
                "format": {"backgroundColor": bg, "textFormat": {"foregroundColor": fg}}
            }
        }, "index": 0}})

    # ── Profit + Cumulative P&L: bold green/red ─────────────────────────
    for col_start, col_end in [(15, 16), (17, 18)]:
        pr = {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2000,
              "startColumnIndex": col_start, "endColumnIndex": col_end}
        reqs.append({"addConditionalFormatRule": {"rule": {
            "ranges": [pr],
            "booleanRule": {
                "condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
                "format": {"textFormat": {"foregroundColor": _c(0, 97, 0), "bold": True}}
            }
        }, "index": 0}})
        reqs.append({"addConditionalFormatRule": {"rule": {
            "ranges": [pr],
            "booleanRule": {
                "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]},
                "format": {"textFormat": {"foregroundColor": _c(156, 0, 6), "bold": True}}
            }
        }, "index": 0}})

    try:
        ss.batch_update({"requests": reqs})
    except Exception:
        log.exception("data sheet formatting failed")


def _format_dashboard(ss, dash, summary_rows, sport_rows, type_rows, book_rows):
    """Apply color formatting to the dashboard tab."""
    sid = dash.id
    reqs = []

    # ── Title row ────────────────────────────────────────────────────────
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 8},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _c(26, 26, 46),
            "textFormat": {"foregroundColor": _c(255, 255, 255), "bold": True, "fontSize": 14},
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)"
    }})

    # ── Section header rows ──────────────────────────────────────────────
    section_header_rows = [
        summary_rows[0],                         # "SUMMARY"
        summary_rows[0] + len(summary_rows) + 1, # "BY SPORT"
        summary_rows[0] + len(summary_rows) + len(sport_rows) + 2,  # "BY BET TYPE"
        summary_rows[0] + len(summary_rows) + len(sport_rows) + len(type_rows) + 3,  # "BY SPORTSBOOK"
    ]
    for row_idx in section_header_rows:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                      "startColumnIndex": 0, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _c(52, 73, 94),
                "textFormat": {"foregroundColor": _c(255, 255, 255), "bold": True},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"
        }})

    # ── Column header rows (W/L/Net etc.) ───────────────────────────────
    col_header_rows = [r + 1 for r in section_header_rows]
    for row_idx in col_header_rows:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                      "startColumnIndex": 0, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _c(189, 195, 199),
                "textFormat": {"bold": True},
                "horizontalAlignment": "CENTER",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
        }})

    # ── Green/red conditional on all numeric value cells ─────────────────
    value_range = {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 120,
                   "startColumnIndex": 2, "endColumnIndex": 6}
    reqs.append({"addConditionalFormatRule": {"rule": {
        "ranges": [value_range],
        "booleanRule": {
            "condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
            "format": {"textFormat": {"foregroundColor": _c(0, 97, 0), "bold": True}}
        }
    }, "index": 0}})
    reqs.append({"addConditionalFormatRule": {"rule": {
        "ranges": [value_range],
        "booleanRule": {
            "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]},
            "format": {"textFormat": {"foregroundColor": _c(156, 0, 6), "bold": True}}
        }
    }, "index": 0}})

    # ── Summary value column width ────────────────────────────────────────
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
        "properties": {"pixelSize": 130}, "fields": "pixelSize"
    }})

    try:
        ss.batch_update({"requests": reqs})
    except Exception:
        log.exception("dashboard formatting failed")


def _get_sheets_client():
    """Get or initialize the gspread client. Sanitizes the credentials JSON
    to handle copy-paste issues (extra whitespace, newlines, BOM characters)."""
    global _sheets_client
    if _sheets_client is not None:
        return _sheets_client
    if not SHEETS_AVAILABLE or not GOOGLE_CREDENTIALS_JSON or not SPREADSHEET_ID:
        return None
    try:
        raw = GOOGLE_CREDENTIALS_JSON.strip().replace("\n", "").replace("\r", "")
        if raw.startswith("\ufeff"):
            raw = raw[1:]
        creds_dict = json.loads(raw)
        required = {"type", "project_id", "private_key", "client_email"}
        missing = required - set(creds_dict.keys())
        if missing:
            log.error(f"GOOGLE_CREDENTIALS_JSON is missing fields: {missing}")
            return None
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        _sheets_client = gspread.authorize(creds)
        log.info("Google Sheets connected successfully")
        return _sheets_client
    except json.JSONDecodeError as e:
        log.error(
            f"GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}. "
            "Make sure you copied the entire .json file contents as one block into Railway."
        )
        return None
    except Exception:
        log.exception("Failed to initialize Google Sheets client")
        return None


def _sheets_call(fn):
    """Run a sheets function, resetting auth and retrying once on failure.
    Handles expired tokens and dropped sessions automatically."""
    global _sheets_client
    try:
        return fn()
    except Exception as e:
        log.warning(f"Sheets call failed ({e}) — resetting client and retrying")
        _sheets_client = None   # force fresh auth on retry
        try:
            return fn()
        except Exception:
            raise  # let the caller handle it after two failures


def _get_or_create_data_sheet(ss, username):
    """Get or create the raw data worksheet for this user."""
    try:
        ws = ss.worksheet(username)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=username, rows=2000, cols=len(SHEET_HEADERS))
        ws.append_row(SHEET_HEADERS, value_input_option="RAW")
        _apply_data_sheet_formatting(ss, ws)
    return ws


def _find_row_by_message_id(ws, message_id):
    try:
        col_values = ws.col_values(COL_MSG_ID)
        for i, val in enumerate(col_values):
            if val == str(message_id):
                return i + 1
    except Exception:
        pass
    return None


def _q(username):
    """Quote a sheet name for use in formulas."""
    return f"'{username}'"


def _setup_dashboard(ss, username):
    """Create or refresh the dashboard tab with formula tables and charts."""
    dash_title = f"{username} Dashboard"
    try:
        dash = ss.worksheet(dash_title)
        dash.clear()
    except gspread.WorksheetNotFound:
        dash = ss.add_worksheet(title=dash_title, rows=120, cols=12)

    q = _q(username)
    data_range = f"{q}!A:R"
    o_col = f"{q}!O:O"
    p_col = f"{q}!P:P"
    f_col = f"{q}!F:F"   # sport
    i_col = f"{q}!I:I"   # bet type
    h_col = f"{q}!H:H"   # sportsbook
    m_col = f"{q}!M:M"   # stake

    def cifs(status, col=o_col):
        return f'COUNTIFS({col},"<>pending",{o_col},"{status}")'

    def sifs(filter_col, filter_val):
        return f'SUMIFS({p_col},{o_col},"<>pending",{filter_col},"{filter_val}")'

    def cifs2(filter_col, filter_val, status):
        return f'COUNTIFS({o_col},"<>pending",{filter_col},"{filter_val}",{o_col},"{status}")'

    # ── Summary block ──────────────────────────────────────────────────
    summary = [
        [f"📊  {username.upper()}  —  BET TRACKER", "", "", ""],
        ["", "", "", ""],
        ["SUMMARY", "", "VALUE", ""],
        ["Total Bets",       "", f"=COUNTA({q}!A2:A)", ""],
        ["Won",              "", f"=COUNTIF({o_col},\"won\")", ""],
        ["Lost",             "", f"=COUNTIF({o_col},\"lost\")", ""],
        ["Push",             "", f"=COUNTIF({o_col},\"push\")", ""],
        ["Win %",            "", f"=IFERROR(C7/(C7+C8)*100,0)", "%"],
        ["Net Profit",       "", f"=SUM({p_col})", "$"],
        ["Total Staked",     "", f"=SUMIF({o_col},\"<>pending\",{m_col})", "$"],
        ["ROI %",            "", f"=IFERROR(C12/C13*100,0)", "%"],
        ["Pending Bets",     "", f"=COUNTIF({o_col},\"pending\")", ""],
        ["Current Streak",   "", f"=IFERROR(INDEX({q}!S:S,MATCH(2,1/({q}!S:S<>\"\"),1)),\"—\")", ""],
        ["", "", "", ""],
        ["ROLLING WINDOWS", "", "NET ($)", "ROI %"],
        ["Last 30 Days",     "",
         f'=SUMIFS({p_col},{q}!B:B,">="&TODAY()-30,{o_col},"<>pending")',
         f'=IFERROR(C19/SUMIFS({m_col},{q}!B:B,">="&TODAY()-30,{o_col},"<>pending")*100,0)'],
        ["Last 60 Days",     "",
         f'=SUMIFS({p_col},{q}!B:B,">="&TODAY()-60,{o_col},"<>pending")',
         f'=IFERROR(C20/SUMIFS({m_col},{q}!B:B,">="&TODAY()-60,{o_col},"<>pending")*100,0)'],
        ["Last 90 Days",     "",
         f'=SUMIFS({p_col},{q}!B:B,">="&TODAY()-90,{o_col},"<>pending")',
         f'=IFERROR(C21/SUMIFS({m_col},{q}!B:B,">="&TODAY()-90,{o_col},"<>pending")*100,0)'],
        ["", "", "", ""],
        ["BEST / WORST DAY", "", "DATE", "NET ($)"],
        ["Best Day",         "",
         f'=IFERROR(TEXT(INDEX({q}!B:B,MATCH(MAX(SUMIFS({p_col},{q}!B:B,{q}!B:B,{o_col},"<>pending")),SUMIFS({p_col},{q}!B:B,{q}!B:B,{o_col},"<>pending"),0)),"YYYY-MM-DD"),"—")',
         f'=IFERROR(MAX(SUMIFS({p_col},{q}!B:B,{q}!B:B,{o_col},"<>pending")),0)'],
        ["Worst Day",        "",
         f'=IFERROR(TEXT(INDEX({q}!B:B,MATCH(MIN(SUMIFS({p_col},{q}!B:B,{q}!B:B,{o_col},"<>pending")),SUMIFS({p_col},{q}!B:B,{q}!B:B,{o_col},"<>pending"),0)),"YYYY-MM-DD"),"—")',
         f'=IFERROR(MIN(SUMIFS({p_col},{q}!B:B,{q}!B:B,{o_col},"<>pending")),0)'],
        ["", "", "", ""],
    ]

    # ── By Sport ───────────────────────────────────────────────────────
    sports = ["Football", "Basketball", "Baseball", "Hockey", "Soccer",
              "MMA", "Tennis", "Golf", "Other"]
    sport_block = [
        ["BY SPORT", "", "", "", "", ""],
        ["Sport", "Bets", "Won", "Lost", "Net ($)", "Win %"],
    ]
    for s in sports:
        w = cifs2(f_col, s, "won")
        l = cifs2(f_col, s, "lost")
        sport_block.append([
            s,
            f"=COUNTIFS({o_col},\"<>pending\",{f_col},\"{s}\")",
            f"={w}", f"={l}",
            f"={sifs(f_col, s)}",
            f"=IFERROR({w}/({w}+{l})*100,0)",
        ])
    sport_block.append(["", "", "", "", "", ""])

    # ── By Bet Type ────────────────────────────────────────────────────
    bet_types = ["moneyline", "spread", "total", "parlay", "prop", "future", "other"]
    type_block = [
        ["BY BET TYPE", "", "", "", "", ""],
        ["Type", "Bets", "Won", "Lost", "Net ($)", "Win %"],
    ]
    for bt in bet_types:
        w = cifs2(i_col, bt, "won")
        l = cifs2(i_col, bt, "lost")
        type_block.append([
            bt.title(),
            f"=COUNTIFS({o_col},\"<>pending\",{i_col},\"{bt}\")",
            f"={w}", f"={l}",
            f"={sifs(i_col, bt)}",
            f"=IFERROR({w}/({w}+{l})*100,0)",
        ])
    type_block.append(["", "", "", "", "", ""])

    # ── By Sportsbook ──────────────────────────────────────────────────
    books = ["DraftKings", "FanDuel", "BetMGM", "Caesars", "Novig",
             "Kalshi", "ESPN BET", "PrizePicks", "Underdog"]
    book_block = [
        ["BY SPORTSBOOK", "", "", "", "", ""],
        ["Book", "Bets", "Won", "Lost", "Net ($)", "Win %"],
    ]
    for b in books:
        w = cifs2(h_col, b, "won")
        l = cifs2(h_col, b, "lost")
        book_block.append([
            b,
            f"=COUNTIFS({o_col},\"<>pending\",{h_col},\"{b}\")",
            f"={w}", f"={l}",
            f"={sifs(h_col, b)}",
            f"=IFERROR({w}/({w}+{l})*100,0)",
        ])
    book_block.append(["", "", "", "", "", ""])

    # ── Monthly breakdown via QUERY ────────────────────────────────────
    monthly_block = [
        ["BY MONTH (QUERY)", "", "", ""],
        [f'=QUERY({data_range},"SELECT YEAR(B),MONTH(B),COUNT(A),SUM(P) WHERE O<>\'pending\' AND B IS NOT NULL GROUP BY YEAR(B),MONTH(B) ORDER BY YEAR(B),MONTH(B) LABEL YEAR(B) \'Year\',MONTH(B) \'Month\',COUNT(A) \'Bets\',SUM(P) \'Net ($)\'",0)',
         "", "", ""],
    ]

    # Write all blocks
    row = 1
    all_blocks = [summary, sport_block, type_block, book_block, monthly_block]
    for block in all_blocks:
        end_row = row + len(block) - 1
        cell_range = f"A{row}:F{end_row}"
        dash.update(cell_range, block, value_input_option="USER_ENTERED")
        row = end_row + 1

    # ── Charts ─────────────────────────────────────────────────────────
    dash_id = dash.id
    sport_data_start = len(summary) + 2   # row where sport data starts (after header)
    type_data_start  = sport_data_start + len(sports) + 3
    book_data_start  = type_data_start + len(bet_types) + 3

    data_ws = ss.worksheet(username)
    data_sid = data_ws.id

    chart_requests = [
        # ── Equity curve: Cumulative P&L over time ──────────────────────
        {"addChart": {"chart": {
            "spec": {
                "title": f"{username} — Equity Curve (Cumulative P&L)",
                "basicChart": {
                    "chartType": "LINE",
                    "legendPosition": "BOTTOM_LEGEND",
                    "lineSmoothing": True,
                    "axis": [
                        {"position": "BOTTOM_AXIS", "title": "Date Settled"},
                        {"position": "LEFT_AXIS",   "title": "Cumulative P&L ($)"},
                    ],
                    "domains": [{"domain": {"sourceRange": {"sources": [
                        {"sheetId": data_sid,
                         "startRowIndex": 0, "endRowIndex": 2000,
                         "startColumnIndex": 1, "endColumnIndex": 2}  # Date Settled (B)
                    ]}}}],
                    "series": [{"series": {"sourceRange": {"sources": [
                        {"sheetId": data_sid,
                         "startRowIndex": 0, "endRowIndex": 2000,
                         "startColumnIndex": 17, "endColumnIndex": 18}  # Cumulative P&L (R)
                    ]}}, "targetAxis": "LEFT_AXIS",
                    "color": _c(46, 204, 113)}],  # green line
                },
            },
            "position": {"overlayPosition": {
                "anchorCell": {"sheetId": dash_id, "rowIndex": 0, "columnIndex": 7},
                "widthPixels": 600, "heightPixels": 350,
            }},
        }}},
        # ── Net profit by sport (column chart) ──────────────────────────
        {"addChart": {"chart": {
            "spec": {
                "title": f"{username} — Net Profit by Sport",
                "basicChart": {
                    "chartType": "COLUMN",
                    "legendPosition": "BOTTOM_LEGEND",
                    "axis": [
                        {"position": "BOTTOM_AXIS", "title": "Sport"},
                        {"position": "LEFT_AXIS",   "title": "Net Profit ($)"},
                    ],
                    "domains": [{"domain": {"sourceRange": {"sources": [
                        {"sheetId": dash_id,
                         "startRowIndex": sport_data_start, "endRowIndex": sport_data_start + len(sports),
                         "startColumnIndex": 0, "endColumnIndex": 1}
                    ]}}}],
                    "series": [{"series": {"sourceRange": {"sources": [
                        {"sheetId": dash_id,
                         "startRowIndex": sport_data_start, "endRowIndex": sport_data_start + len(sports),
                         "startColumnIndex": 4, "endColumnIndex": 5}
                    ]}}, "targetAxis": "LEFT_AXIS"}],
                },
            },
            "position": {"overlayPosition": {
                "anchorCell": {"sheetId": dash_id, "rowIndex": 20, "columnIndex": 7},
                "widthPixels": 500, "heightPixels": 300,
            }},
        }}},
        # Win % by bet type (bar chart)
        {"addChart": {"chart": {
            "spec": {
                "title": f"{username} — Win % by Bet Type",
                "basicChart": {
                    "chartType": "BAR",
                    "legendPosition": "BOTTOM_LEGEND",
                    "axis": [
                        {"position": "BOTTOM_AXIS", "title": "Win %"},
                        {"position": "LEFT_AXIS",   "title": "Bet Type"},
                    ],
                    "domains": [{"domain": {"sourceRange": {"sources": [
                        {"sheetId": dash_id,
                         "startRowIndex": type_data_start, "endRowIndex": type_data_start + len(bet_types),
                         "startColumnIndex": 0, "endColumnIndex": 1}
                    ]}}}],
                    "series": [{"series": {"sourceRange": {"sources": [
                        {"sheetId": dash_id,
                         "startRowIndex": type_data_start, "endRowIndex": type_data_start + len(bet_types),
                         "startColumnIndex": 5, "endColumnIndex": 6}
                    ]}}, "targetAxis": "BOTTOM_AXIS"}],
                },
            },
            "position": {"overlayPosition": {
                "anchorCell": {"sheetId": dash_id, "rowIndex": 36, "columnIndex": 7},
                "widthPixels": 500, "heightPixels": 300,
            }},
        }}},
        # W/L/P pie chart
        {"addChart": {"chart": {
            "spec": {
                "title": f"{username} — W / L / P",
                "pieChart": {
                    "legendPosition": "RIGHT_LEGEND",
                    "domain": {"sourceRange": {"sources": [
                        {"sheetId": dash_id,
                         "startRowIndex": 3, "endRowIndex": 6,
                         "startColumnIndex": 0, "endColumnIndex": 1}
                    ]}},
                    "series": {"sourceRange": {"sources": [
                        {"sheetId": dash_id,
                         "startRowIndex": 3, "endRowIndex": 6,
                         "startColumnIndex": 2, "endColumnIndex": 3}
                    ]}},
                },
            },
            "position": {"overlayPosition": {
                "anchorCell": {"sheetId": dash_id, "rowIndex": 52, "columnIndex": 7},
                "widthPixels": 500, "heightPixels": 300,
            }},
        }}},
    ]

    try:
        ss.batch_update({"requests": chart_requests})
    except Exception:
        log.exception("chart creation failed — dashboard data still written")

    _format_dashboard(ss, dash, summary, sport_block, type_block, book_block)
    _create_calendar_tab(ss, username)


def _create_calendar_tab(ss, username):
    """Create a Pikkit-style calendar tab showing daily P&L for the current month."""
    import calendar as cal_lib
    now = datetime.now(CENTRAL)
    year, month = now.year, now.month
    month_name = now.strftime("%B %Y")
    tab_title = f"{username} Calendar"

    try:
        cal_ws = ss.worksheet(tab_title)
        cal_ws.clear()
    except gspread.WorksheetNotFound:
        cal_ws = ss.add_worksheet(title=tab_title, rows=12, cols=7)

    q = _q(username)
    p_col = f"{q}!P:P"
    a_col = f"{q}!A:A"   # Date Posted (CT) — group by placement date
    o_col = f"{q}!O:O"

    # Title row
    rows = [[f"📅  {username.upper()} — {month_name}", "", "", "", "", "", ""]]
    rows.append(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])

    # Build the month calendar
    first_weekday, num_days = cal_lib.monthrange(year, month)
    start_offset = (first_weekday + 1) % 7  # convert to Sunday-first

    week = [""] * 7
    day = 1
    calendar_rows = []

    col = start_offset
    while day <= num_days:
        # SUMIFS: sum profit for bets PLACED on this date (column A = Date Posted)
        formula = (
            f'=IFERROR(SUMIFS({p_col},{a_col},">="&DATE({year},{month},{day}),'
            f'{a_col},"<"&DATE({year},{month},{day})+1,{o_col},"<>pending"),"")'
        )
        week[col] = formula
        col += 1
        if col == 7:
            calendar_rows.append(week)
            week = [""] * 7
            col = 0
        day += 1

    if any(c != "" for c in week):
        calendar_rows.append(week)

    rows.extend(calendar_rows)
    cal_ws.update("A1", rows, value_input_option="USER_ENTERED")

    # Formatting
    sid = cal_ws.id
    reqs = []

    # Title row — dark navy
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 7},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _c(26, 26, 46),
            "textFormat": {"foregroundColor": _c(255, 255, 255), "bold": True, "fontSize": 13},
            "horizontalAlignment": "CENTER",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
    }})

    # Day header row — medium blue-grey
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2,
                  "startColumnIndex": 0, "endColumnIndex": 7},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _c(52, 73, 94),
            "textFormat": {"foregroundColor": _c(255, 255, 255), "bold": True},
            "horizontalAlignment": "CENTER",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
    }})

    # Calendar cells — currency format + taller rows
    cal_range = {"sheetId": sid, "startRowIndex": 2,
                 "endRowIndex": 2 + len(calendar_rows),
                 "startColumnIndex": 0, "endColumnIndex": 7}
    reqs.append({"repeatCell": {
        "range": cal_range,
        "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "CURRENCY", "pattern": '"$"#,##0.00;"-$"#,##0.00'},
            "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
            "textFormat": {"fontSize": 11, "bold": True},
        }},
        "fields": "userEnteredFormat(numberFormat,horizontalAlignment,verticalAlignment,textFormat)"
    }})

    # Green if positive, red if negative
    reqs.append({"addConditionalFormatRule": {"rule": {
        "ranges": [cal_range],
        "booleanRule": {
            "condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
            "format": {"backgroundColor": _c(198, 239, 206),
                       "textFormat": {"foregroundColor": _c(0, 97, 0), "bold": True}}
        }
    }, "index": 0}})
    reqs.append({"addConditionalFormatRule": {"rule": {
        "ranges": [cal_range],
        "booleanRule": {
            "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]},
            "format": {"backgroundColor": _c(255, 199, 206),
                       "textFormat": {"foregroundColor": _c(156, 0, 6), "bold": True}}
        }
    }, "index": 0}})

    # Column widths and row heights
    for i in range(7):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i+1},
            "properties": {"pixelSize": 130}, "fields": "pixelSize"
        }})
    for i in range(2, 2 + len(calendar_rows)):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": i, "endIndex": i+1},
            "properties": {"pixelSize": 60}, "fields": "pixelSize"
        }})

    try:
        ss.batch_update({"requests": reqs})
    except Exception:
        log.exception("calendar tab formatting failed")


async def sheets_log_bet(bet_data, username, group_name, message_id):
    """Append a new pending bet row to the user's sheet."""
    client = _get_sheets_client()
    if client is None:
        return
    loop = asyncio.get_event_loop()
    try:
        def _write():
            ss = client.open_by_key(SPREADSHEET_ID)
            ws = _get_or_create_data_sheet(ss, username)
            legs_count = len(bet_data.get("legs") or [])
            row = [
                datetime.now(CENTRAL).strftime("%Y-%m-%d %I:%M %p"),  # A Date Posted
                "",                               # B Date Settled — filled on grade
                username,                         # C
                group_name,                       # D
                bet_data.get("description") or "",# E
                (bet_data.get("sport") or "").title(),   # F
                (bet_data.get("league") or "").upper(),  # G
                bet_data.get("sportsbook") or "", # H
                (bet_data.get("bet_type") or "").title(),# I
                bet_data.get("prop_category") or "",     # J
                legs_count if legs_count > 1 else "",    # K
                bet_data.get("odds") or "",       # L
                bet_data.get("stake") or "",      # M
                bet_data.get("potential_payout") or "",  # N
                "pending",                        # O Status
                "",                               # P Profit — filled on grade
                "",                               # Q ROI % — filled on grade
                "",                               # R Cumulative P&L — filled on grade
                "",                               # S Streak — filled on grade
                str(message_id),                  # T Message ID
            ]
            ws.append_row(row, value_input_option="USER_ENTERED")
            # Create/refresh dashboard and calendar on first use
            existing_titles = [w.title for w in ss.worksheets()]
            dash_title = f"{username} Dashboard"
            if dash_title not in existing_titles:
                _setup_dashboard(ss, username)  # already calls _create_calendar_tab
        await loop.run_in_executor(None, lambda: _sheets_call(_write))
    except Exception:
        log.exception("sheets_log_bet failed")
        await post_monitor("Sheets Write Failed", "Could not log a new bet to the spreadsheet. Check Railway logs.", level="error")


async def sheets_update_bet(username, message_id, status, profit, cumulative_pnl=None):
    """Update status, profit, ROI, cumulative P&L, streak, and settled date.
    Verifies the status cell after writing and retries once if it didn't stick."""
    client = _get_sheets_client()
    if client is None:
        return
    loop = asyncio.get_event_loop()
    try:
        def _update():
            ss = client.open_by_key(SPREADSHEET_ID)
            ws = _get_or_create_data_sheet(ss, username)
            row_num = _find_row_by_message_id(ws, str(message_id))
            if row_num is None:
                return

            stake_val = ws.cell(row_num, 13).value
            try:
                stake = float(stake_val) if stake_val else None
            except (ValueError, TypeError):
                stake = None
            roi = round(profit / stake * 100, 1) if (profit is not None and stake) else ""

            # Cumulative P&L — use pre-calculated value from DB if provided,
            # otherwise fall back to summing the sheet column (less accurate for out-of-order grades)
            if cumulative_pnl is not None:
                cumulative = cumulative_pnl
            else:
                running = 0.0
                for p in ws.col_values(COL_PROFIT)[1:]:
                    try:
                        if p and p not in ("", "pending"):
                            running += float(p)
                    except (ValueError, TypeError):
                        pass
                cumulative = round(running + (profit or 0), 2)

            # Streak — look at all status values, find current run
            all_statuses = ws.col_values(COL_STATUS)[1:]
            settled = [s for s in all_statuses if s in ("won", "lost")]
            streak_str = ""
            if settled:
                last = settled[-1]
                count = sum(1 for _ in takewhile(lambda s: s == last, reversed(settled)))
                streak_str = f"{'W' if last == 'won' else 'L'}{count}"

            settled_dt = datetime.now(CENTRAL).strftime("%Y-%m-%d %I:%M %p")
            ws.batch_update([
                {"range": f"B{row_num}", "values": [[settled_dt]]},
                {"range": f"O{row_num}", "values": [[status]]},
                {"range": f"P{row_num}", "values": [[round(profit, 2) if profit is not None else ""]]},
                {"range": f"Q{row_num}", "values": [[roi]]},
                {"range": f"R{row_num}", "values": [[cumulative]]},
                {"range": f"S{row_num}", "values": [[streak_str]]},
            ], value_input_option="USER_ENTERED")

            # Auto-rebuild calendar tabs for ALL users if month has rolled over
            now_ct = datetime.now(CENTRAL)
            current_month = (now_ct.year, now_ct.month)
            cal_key = username
            if _calendar_built.get(cal_key) != current_month:
                try:
                    existing_titles = [w.title for w in ss.worksheets()]
                    for ws_title in existing_titles:
                        if ws_title.endswith(" Calendar"):
                            uname = ws_title[: -len(" Calendar")]
                            _create_calendar_tab(ss, uname)
                            _calendar_built[uname] = current_month
                    if not any(t.endswith(" Calendar") for t in existing_titles):
                        _create_calendar_tab(ss, username)
                        _calendar_built[cal_key] = current_month
                except Exception:
                    log.exception("calendar auto-rebuild failed")
        await loop.run_in_executor(None, lambda: _sheets_call(_update))

        # verify the write landed; retry once if not
        def _verify():
            ss = client.open_by_key(SPREADSHEET_ID)
            ws = _get_or_create_data_sheet(ss, username)
            row_num = _find_row_by_message_id(ws, str(message_id))
            if row_num is None:
                return True   # row missing is a different problem, not a write failure
            return (ws.cell(row_num, COL_STATUS).value or "").strip().lower() == str(status).lower()

        ok = await loop.run_in_executor(None, lambda: _sheets_call(_verify))
        if ok is False:
            log.warning(f"sheet grade for {message_id} didn't stick — retrying once")
            await loop.run_in_executor(None, lambda: _sheets_call(_update))
            ok2 = await loop.run_in_executor(None, lambda: _sheets_call(_verify))
            if ok2 is False:
                await post_monitor("Sheet Grade Verify Failed",
                                   f"Status for bet {message_id} ({username}) didn't save to the "
                                   f"sheet after retry. Run /resync to force it.", level="error")
    except Exception:
        log.exception("sheets_update_bet failed")
        await post_monitor("Sheets Update Failed", "Could not update a graded bet in the spreadsheet. Check Railway logs.", level="error")


async def sheets_delete_bet(username, message_id):
    """Delete a bet row from the Google Sheet by message ID."""
    client = _get_sheets_client()
    if client is None:
        return
    loop = asyncio.get_event_loop()
    try:
        def _delete():
            ss = client.open_by_key(SPREADSHEET_ID)
            ws = _get_or_create_data_sheet(ss, username)
            row_num = _find_row_by_message_id(ws, str(message_id))
            if row_num is None:
                log.warning(f"sheets_delete_bet: message_id {message_id} not found in sheet for {username}")
                return
            ws.delete_rows(row_num)
            log.info(f"Deleted sheet row {row_num} for message_id {message_id} ({username})")
        await loop.run_in_executor(None, lambda: _sheets_call(_delete))
    except Exception:
        log.exception("sheets_delete_bet failed")
        await post_monitor("Sheets Delete Failed", f"Could not delete bet from spreadsheet for {username}. Remove manually from the sheet.", level="error")

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

intents = discord.Intents.default()
intents.message_content = True  # required (privileged) - enable in Discord Dev Portal too

bot = commands.Bot(command_prefix="!", intents=intents)

REACTIONS = {"✅": "won", "❌": "lost", "↩️": "push"}
CONFIRM_EMOJI = "☑️"   # visually distinct from grade ✅ — poster reacts to confirm a flagged parse
REJECT_EMOJI  = "📝"   # poster reacts to discard and re-enter manually

# Bets parsed but flagged for review — stored here until poster confirms or rejects.
# Format: {message_id: {data, group_name, user_id, username, source_url, channel_id, guild_id, created_at}}
# Entries older than CONFIRMATION_TTL_HOURS are pruned automatically.
_awaiting_confirmation: dict = {}
CONFIRMATION_TTL_HOURS = 24


def validate_parsed_bet(data: dict) -> tuple[str, str | None]:
    """
    Validate AI-parsed bet data before saving.
    Returns (severity, message):
      "ok"      → save immediately, no issues
      "warning" → save but flag — odds/payout math is off, poster should verify
      "error"   → don't save — not enough data to do anything useful
    """
    desc  = (data.get("description") or "").strip()
    odds  = data.get("odds")
    stake = data.get("stake")
    payout = data.get("potential_payout")
    book  = data.get("sportsbook") or ""

    # Complete failure — nothing useful was read
    if not desc and not book and odds is None and stake is None:
        return "error", (
            "Couldn't read enough from this slip. "
            "Try a clearer screenshot or use `/logbet` to enter it manually."
        )

    # Odds/payout math check — flag if they don't agree within 5%
    if odds is not None and stake is not None and payout is not None:
        try:
            s = float(stake)
            p = float(payout)
            o = int(odds)
            if o > 0:
                expected = s + s * (o / 100)
            else:
                expected = s + s * (100 / abs(o))
            if p > 0 and abs(expected - p) / p > 0.05:
                return "warning", (
                    f"Odds/payout mismatch: {o:+d} × ${s:.2f} should pay "
                    f"~${expected:.2f}, got ${p:.2f}. "
                    f"React {CONFIRM_EMOJI} to save anyway or {REJECT_EMOJI} to re-enter."
                )
        except (ValueError, TypeError, ZeroDivisionError):
            pass

    # Stake present but no odds AND no payout — can't calculate profit later
    if stake is not None and odds is None and payout is None:
        return "warning", (
            f"No odds or payout found — profit can't be calculated automatically. "
            f"React {CONFIRM_EMOJI} to save anyway or {REJECT_EMOJI} to re-enter with `/logbet`."
        )

    return "ok", None

PARSE_PROMPT = """You are extracting structured data from a sports betting slip screenshot.
Respond with ONLY valid JSON (no markdown fences, no extra text) matching exactly this schema:

{
  "sportsbook": string or null,
  "sport": string or null,           // Must be one of: "Football", "Basketball", "Baseball", "Soccer", "Hockey", "MMA", "Tennis", "Golf", "Other"
  "league": string or null,          // e.g. "NFL", "NBA", "WNBA", "MLB", "NHL", "NCAAF", "NCAAB", "EPL", "UEFA", "UFC", "PGA", "MLS"
  "bet_type": one of "moneyline", "spread", "total", "parlay", "prop", "future", "other",
  "prop_category": string or null,   // ONLY for props: e.g. "Points", "Assists", "Rebounds", "Strikeouts", "Passing Yards", "Rushing Yards", "Receiving Yards", "Home Runs", "Saves", "First TD Scorer", "Anytime TD", "Goals"
  "description": string,             // short human-readable summary of the bet
  "legs": [string],                  // one entry per leg; straight bets still have exactly 1 entry
  "odds": number or null,            // American odds, e.g. -110 or +150
  "stake": number or null,           // dollars risked, no $ sign
  "potential_payout": number or null // total payout if it wins (stake + profit), no $ sign
}

Sport classification rules (follow strictly):
- NBA, WNBA, NCAA basketball, college basketball → "Basketball"
- NFL, college football, NCAAF → "Football"
- MLB, college baseball → "Baseball"
- NHL, college hockey → "Hockey"
- EPL, MLS, UEFA, Champions League, World Cup, international soccer → "Soccer"
- UFC, Bellator, MMA → "MMA"
- ATP, WTA, tennis → "Tennis"
- PGA, golf → "Golf"
- WNBA is basketball NOT football — women's basketball players score points not touchdowns
- SEASONS MATTER: the WNBA plays May–October; the NBA plays October–June. A basketball bet placed in July, August, or September is almost certainly WNBA. Check team names: Aces/Liberty/Lynx/Storm/Sun/Mercury/Sparks/Fever/Sky/Wings/Mystics/Dream/Valkyries/Tempo/Fire are WNBA
- These WNBA team names are BASKETBALL teams, never soccer, hockey, or any other sport, even if the name sounds generic (e.g. "Storm", "Fire", "Dream", "Sun" are basketball teams, not weather or soccer club references). If you don't recognize a team name from a major men's league, check it against this WNBA list before guessing another sport
- If the bet mentions player stats like points/rebounds/assists it is almost certainly Basketball not Football
- If unsure, use the league name to determine sport

If a field isn't visible or determinable from the image, use null. Only fill prop_category when bet_type is "prop"."""


def group_for_bet_channel(channel_id):
    cid = str(channel_id)
    for g in GROUPS:
        if g.get("bet_channel_id") and str(g["bet_channel_id"]) == cid:
            return g
    return None


def group_for_any_channel(channel_id):
    cid = str(channel_id)
    for g in GROUPS:
        if cid in filter(None, [
            g.get("bet_channel_id") and str(g["bet_channel_id"]),
            g.get("output_channel_id") and str(g["output_channel_id"]),
            g.get("stats_channel_id") and str(g["stats_channel_id"]),
        ]):
            return g
    return None


def is_managed_output_or_stats_channel(channel_id):
    cid = str(channel_id)
    for g in GROUPS:
        if (g.get("output_channel_id") and cid == str(g["output_channel_id"])) or \
           (g.get("stats_channel_id") and cid == str(g["stats_channel_id"])):
            return True
    return False


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT UNIQUE,
                channel_id TEXT,
                guild_id TEXT,
                user_id TEXT,
                username TEXT,
                group_name TEXT,
                sportsbook TEXT,
                bet_type TEXT,
                description TEXT,
                legs TEXT,
                odds INTEGER,
                stake REAL,
                potential_payout REAL,
                status TEXT DEFAULT 'pending',
                profit REAL,
                created_at TEXT,
                settled_at TEXT
            )
            """
        )
        # migrate older databases that predate the group_name column
        cur = await db.execute("PRAGMA table_info(bets)")
        cols = [row[1] for row in await cur.fetchall()]
        if "group_name" not in cols:
            await db.execute("ALTER TABLE bets ADD COLUMN group_name TEXT DEFAULT 'default'")
        if "sport" not in cols:
            await db.execute("ALTER TABLE bets ADD COLUMN sport TEXT")
        if "league" not in cols:
            await db.execute("ALTER TABLE bets ADD COLUMN league TEXT")
        for col, typ in (("live_odds", "REAL"), ("live_odds_at", "TEXT"),
                         ("closing_odds", "REAL"), ("clv", "REAL"),
                         ("odds_alerted", "INTEGER DEFAULT 0"),
                         ("auto_graded", "INTEGER DEFAULT 0"),
                         ("score_checked", "INTEGER DEFAULT 0"),
                         ("game_time", "TEXT")):
            if col not in cols:
                await db.execute(f"ALTER TABLE bets ADD COLUMN {col} {typ}")

        # live_stats_messages: migrate from old group-level schema to per-user schema if needed
        cur2 = await db.execute("PRAGMA table_info(live_stats_messages)")
        lsm_cols = [row[1] for row in await cur2.fetchall()]
        if "user_id" not in lsm_cols:
            # Old schema had group_name as sole PK — drop and recreate (only stores message IDs, not bet data)
            await db.execute("DROP TABLE IF EXISTS live_stats_messages")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS live_stats_messages (
                group_name TEXT,
                user_id TEXT,
                channel_id TEXT,
                message_id TEXT,
                PRIMARY KEY (group_name, user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_schedule (
                key TEXT PRIMARY KEY,
                last_run TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS monitor_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await db.commit()


def calc_profit(odds, stake, status, potential_payout=None):
    if stake is None:
        return None
    if status == "lost":
        return round(-stake, 2)
    if status == "push":
        return 0.0
    # Won — try odds first, fall back to potential_payout
    if status == "won":
        if odds is not None:
            o = _to_american(odds)
            if o > 0:
                return round(stake * (o / 100), 2)
            else:
                return round(stake * (100 / abs(o)), 2)
        if potential_payout is not None:
            return round(potential_payout - stake, 2)
    return None


def _to_american(odds):
    """Normalize odds to American. Values between -99 and +99 aren't valid
    American odds — treat 1-99 as a percentage (Kalshi/Polymarket style)
    and 0-1 as a decimal probability."""
    o = float(odds)
    if abs(o) >= 100:
        return o
    if 1 <= o < 100:          # percentage, e.g. 75 = 75% chance
        p = o / 100
        return -round(p / (1 - p) * 100) if p >= 0.5 else round((1 - p) / p * 100)
    if 0 < o < 1:             # decimal probability, e.g. 0.75
        return -round(o / (1 - o) * 100) if o >= 0.5 else round((1 - o) / o * 100)
    return o


def detect_media_type(data: bytes, fallback: str) -> str:
    """Sniff the real image type from file bytes. Discord's reported content_type
    can be wrong/mismatched, and the Anthropic API rejects that mismatch outright."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def fmt_odds(odds):
    if odds is None:
        return None
    try:
        odds = int(odds)
        return f"{odds:+d}"
    except (ValueError, TypeError):
        return str(odds)


SPORT_EMOJI = {
    "football": "🏈", "basketball": "🏀", "baseball": "⚾",
    "hockey": "🏒", "soccer": "⚽", "mma": "🥊", "boxing": "🥊",
    "tennis": "🎾", "golf": "⛳", "rugby": "🏉", "volleyball": "🏐",
    "cricket": "🏏",
}

def sport_emoji(sport):
    if not sport:
        return "🎲"
    return SPORT_EMOJI.get(sport.lower(), "🎲")


def build_embed(data, author_name, status="pending", profit=None, record=None):
    color = {
        "pending": 0xF5A623,
        "won":     0x2ECC71,
        "lost":    0xE74C3C,
        "push":    0x95A5A6,
    }[status]

    sport  = data.get("sport") or ""
    league = data.get("league") or ""
    bt     = data.get("bet_type") or ""
    book   = data.get("sportsbook") or ""

    # Bet description is the title — biggest, most readable text
    title = f"{sport_emoji(sport)}  {data.get('description') or 'Bet'}"

    # Single subtitle line: tags
    tag_parts = [t for t in [league.upper(), bt.title(), book] if t]
    subtitle = "  ·  ".join(tag_parts) if tag_parts else ""

    # Numbers line
    num_parts = []
    if data.get("odds") is not None:
        num_parts.append(fmt_odds(data["odds"]))
    if data.get("stake") is not None:
        num_parts.append(f"${data['stake']:.2f} risk")
    if data.get("potential_payout") is not None:
        num_parts.append(f"${data['potential_payout']:.2f} to win")
    numbers = "  ·  ".join(num_parts)

    # Parlay legs
    legs = data.get("legs") or []
    legs_text = ""
    if len(legs) > 1:
        legs_text = "\n" + "\n".join(f"▸ {l}" for l in legs)

    desc = "\n".join(filter(None, [subtitle, numbers, legs_text]))
    embed = discord.Embed(title=title, description=desc or None, color=color)

    # Status line
    if status == "pending":
        embed.add_field(name="​", value="🟡 Pending  ·  ✅ W  ❌ L  ↩️ P", inline=False)
    else:
        icons  = {"won": "🟢", "lost": "🔴", "push": "⚪"}
        labels = {"won": "WIN", "lost": "LOSS", "push": "PUSH"}
        profit_str = f"  {'+' if profit >= 0 else ''}{profit:.2f}" if profit is not None else ""
        record_str = f"  ·  {record}" if record else ""
        embed.add_field(
            name="​",
            value=f"{icons[status]} **{labels[status]}**{profit_str}{record_str}",
            inline=False,
        )

    embed.set_footer(text=author_name)
    return embed


def period_cutoffs():
    now_ct = datetime.now(CENTRAL)
    today = now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start  = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    day30 = today - timedelta(days=30)
    day60 = today - timedelta(days=60)
    day90 = today - timedelta(days=90)
    def _utc(dt): return dt.astimezone(timezone.utc).isoformat()
    return (
        _utc(today),
        _utc(week_start),
        _utc(month_start),
        _utc(day30),
        _utc(day60),
        _utc(day90),
    )


async def get_user_period_stats(db, user_id, group_name, cutoff):
    cur = await db.execute(
        """
        SELECT COALESCE(SUM(profit), 0),
               COALESCE(SUM(CASE WHEN status='won'  THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN status='push' THEN 1 ELSE 0 END), 0)
        FROM bets
        WHERE user_id = ? AND group_name = ? AND status != 'pending'
        AND created_at >= ?
        """,
        (user_id, group_name, cutoff),
    )
    row = await cur.fetchone()
    net, wins, losses, pushes = row
    decided = wins + losses
    win_pct = f"{wins/decided*100:.0f}%" if decided > 0 else "—"
    net_str = f"{'+' if net >= 0 else ''}{net:.2f}"
    return wins, losses, pushes, net_str, win_pct


async def get_user_streak(db, user_id, group_name):
    cur = await db.execute(
        """
        SELECT status FROM bets
        WHERE user_id = ? AND group_name = ? AND status IN ('won','lost')
        ORDER BY created_at DESC LIMIT 20
        """,
        (user_id, group_name),
    )
    rows = await cur.fetchall()
    if not rows:
        return None
    first = rows[0][0]
    count = 0
    for (status,) in rows:
        if status == first:
            count += 1
        else:
            break
    label = "🔥 W" if first == "won" else "🥶 L"
    return f"{label}{count}"


def build_discord_calendar(daily_pnl, year, month):
    """Calendar grid — day numbers on top, P&L below, only on weeks with action."""
    import calendar as cal_lib
    first_weekday, num_days = cal_lib.monthrange(year, month)
    start_offset = (first_weekday + 1) % 7  # Sunday-first

    header = "Su  Mo  Tu  We  Th  Fr  Sa"
    divider = "─" * 27
    lines = [header, divider]
    won_days = lost_days = 0

    day_row = ["   "] * 7
    pnl_row = ["   "] * 7
    col = start_offset
    week_has_bets = False

    for day in range(1, num_days + 1):
        date_str = f"{year}-{month:02d}-{day:02d}"
        net = daily_pnl.get(date_str)
        day_row[col] = f"{day:>3}"

        if net is not None:
            week_has_bets = True
            if net > 0:
                won_days += 1
                pnl_row[col] = f"+{min(int(abs(net)),999):>2}"
            elif net < 0:
                lost_days += 1
                pnl_row[col] = f"-{min(int(abs(net)),999):>2}"
            else:
                pnl_row[col] = " =0"
        else:
            pnl_row[col] = " · "

        col += 1
        if col == 7:
            lines.append(" ".join(day_row))
            lines.append(" ".join(pnl_row))
            day_row = ["   "] * 7
            pnl_row = ["   "] * 7
            week_has_bets = False
            col = 0

    if col > 0:
        for i in range(col, 7):
            day_row[i] = "   "
            pnl_row[i] = "   "
        lines.append(" ".join(day_row))
        lines.append(" ".join(pnl_row))

    return "\n".join(lines), won_days, lost_days


async def build_user_embed(user_id, username, group_name):
    import calendar as cal_lib
    today_cut, week_cut, month_cut, d30_cut, d60_cut, d90_cut = period_cutoffs()
    now_ct = datetime.now(CENTRAL)
    year, month = now_ct.year, now_ct.month

    async with aiosqlite.connect(DB_PATH) as db:
        daily   = await get_user_period_stats(db, user_id, group_name, today_cut)
        weekly  = await get_user_period_stats(db, user_id, group_name, week_cut)
        monthly = await get_user_period_stats(db, user_id, group_name, month_cut)
        alltime = await get_user_period_stats(db, user_id, group_name, "1970-01-01")
        streak  = await get_user_streak(db, user_id, group_name)

        cur = await db.execute(
            "SELECT COUNT(*) FROM bets WHERE user_id=? AND group_name=? AND status='pending'",
            (user_id, group_name),
        )
        (pending,) = await cur.fetchone()

        # Max upside — sum of potential profit on all pending bets
        cur_up = await db.execute(
            """SELECT COALESCE(SUM(potential_payout - stake), 0),
                      COALESCE(SUM(stake), 0)
               FROM bets
               WHERE user_id=? AND group_name=? AND status='pending'
               AND potential_payout IS NOT NULL AND stake IS NOT NULL""",
            (user_id, group_name),
        )
        (max_upside, total_risk) = await cur_up.fetchone()

        # Daily P&L for calendar — group by the day the bet was MADE (created_at),
        # not the day it graded, so Sunday's slate shows on Sunday
        cur_cal = await db.execute(
            """SELECT created_at, profit FROM bets
               WHERE user_id=? AND group_name=? AND status!='pending'
               AND created_at >= ?""",
            (user_id, group_name, month_cut),
        )
        cal_rows = await cur_cal.fetchall()

    # Build daily P&L dict keyed by CT date of bet placement
    daily_pnl = {}
    for created_at_str, profit in cal_rows:
        if not created_at_str or profit is None:
            continue
        try:
            dt = datetime.fromisoformat(created_at_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            date_key = dt.astimezone(CENTRAL).strftime("%Y-%m-%d")
            daily_pnl[date_key] = daily_pnl.get(date_key, 0) + profit
        except Exception:
            pass

    try:
        alltime_net = float(alltime[3].replace("+", ""))
    except ValueError:
        alltime_net = 0
    color = 0x2ECC71 if alltime_net >= 0 else 0xE74C3C

    streak_badge = f"  {streak}" if streak else ""
    title = f"{username}{streak_badge}"

    # Stats table — today / week / month / all time
    def r(label, w, l, p, net, wp):
        return f"{label:<9}{w:>5}{l:>5}{p:>4}  {net:>10}  {wp:>5}"

    div  = "─" * 40
    div2 = "═" * 40
    hdr  = f"{'':9}{'W':>5}{'L':>5}{'P':>4}  {'NET':>10}  {'WIN%':>5}"

    table = "```\n"
    table += hdr + "\n" + div + "\n"
    table += r("Today",    *daily)   + "\n"
    table += r("Week",     *weekly)  + "\n"
    table += r("Month",    *monthly) + "\n"
    table += div2 + "\n"
    table += r("All Time", *alltime) + "\n"
    table += "```"

    # Calendar block — just the grid, no header line
    cal_str, won_days, lost_days = build_discord_calendar(daily_pnl, year, month)
    month_label = now_ct.strftime("%b %Y")
    month_net = sum(daily_pnl.values())
    month_net_str = f"{'+' if month_net >= 0 else ''}{month_net:.0f}"
    calendar_block = f"```\n📅 {month_label}  {won_days}W {lost_days}L  {month_net_str}\n{cal_str}\n```"

    # Pending section
    if pending:
        upside_str = f"+${max_upside:.2f}" if max_upside else "—"
        risk_str   = f"-${total_risk:.2f}" if total_risk else "—"
        pending_block = f"```\n⏳ {pending} pending  ·  risk {risk_str}  ·  upside {upside_str}\n```"
    else:
        pending_block = ""

    description = table
    if pending_block:
        description += "\n" + pending_block
    description += "\n" + calendar_block

    embed = discord.Embed(title=title, description=description, color=color)

    footer_parts = [now_ct.strftime("%b %d  %I:%M %p CT")]
    embed.set_footer(text="  ·  ".join(footer_parts))
    return embed


async def refresh_live_stats(group_name):
    group = next((g for g in GROUPS if g.get("name") == group_name), None)
    stats_id = group.get("stats_channel_id") if group else None
    if not stats_id:
        return
    channel = bot.get_channel(int(stats_id))
    if channel is None:
        return

    # Get all users who have any bets in this group
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT DISTINCT user_id, username FROM bets WHERE group_name = ? ORDER BY created_at ASC",
            (group_name,),
        )
        users = await cur.fetchall()

        cur2 = await db.execute(
            "SELECT user_id, channel_id, message_id FROM live_stats_messages WHERE group_name = ?",
            (group_name,),
        )
        stored = {row[0]: (row[1], row[2]) for row in await cur2.fetchall()}

    for user_id, username in users:
        embed = await build_user_embed(user_id, username, group_name)

        if user_id in stored:
            stored_channel_id, stored_message_id = stored[user_id]
            try:
                msg_channel = bot.get_channel(int(stored_channel_id)) or channel
                msg = await msg_channel.fetch_message(int(stored_message_id))
                await msg.edit(embed=embed)
                continue
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass  # fall through and post fresh

        new_msg = await channel.send(embed=embed)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO live_stats_messages (group_name, user_id, channel_id, message_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_name, user_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id
                """,
                (group_name, user_id, str(channel.id), str(new_msg.id)),
            )
            await db.commit()


@bot.event
async def on_ready():
    await init_db()
    try:
        await bot.tree.sync()
    except Exception:
        log.exception("slash command sync failed")

    # Sheets health check — logs clearly so you can see in Railway whether it connected
    if SPREADSHEET_ID and GOOGLE_CREDENTIALS_JSON:
        def _check_sheets():
            client = _get_sheets_client()
            if client is None:
                return False
            try:
                ss = client.open_by_key(SPREADSHEET_ID)
                log.info(f"✅ Google Sheets connected: '{ss.title}' ({SPREADSHEET_ID})")
                return True
            except Exception as e:
                log.error(f"❌ Google Sheets connected but spreadsheet not found: {e}. "
                          "Check SPREADSHEET_ID and that the sheet is shared with the service account.")
                return False
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _check_sheets)
    elif SPREADSHEET_ID or GOOGLE_CREDENTIALS_JSON:
        log.warning("⚠️  Google Sheets: only one of SPREADSHEET_ID / GOOGLE_CREDENTIALS_JSON is set — both required")
    else:
        log.info("Google Sheets not configured (SPREADSHEET_ID / GOOGLE_CREDENTIALS_JSON not set) — skipping")

    for g in GROUPS:
        if g.get("stats_channel_id"):
            try:
                await refresh_live_stats(g["name"])
            except Exception:
                log.exception(f"failed to refresh live stats for group {g.get('name')}")

    # Startup notification to monitor channel
    sheets_status = "🟢 Connected" if _sheets_client is not None else "🔴 Not configured"
    await post_monitor(
        "Bot Online",
        f"**Groups:** {len(GROUPS)}\n**Sheets:** {sheets_status}\nRunning startup audit...",
        level="ok",
    )

    # Startup audit — run automatically on every deploy to catch any data issues
    log.info("Running startup audit...")
    await run_system_audit(reason="startup")

    if not biweekly_audit.is_running():
        biweekly_audit.start()
    if not cleanup_stale_confirmations.is_running():
        cleanup_stale_confirmations.start()
    if not status_heartbeat.is_running():
        status_heartbeat.start()
    if ODDS_API_KEY and not odds_watch.is_running():
        odds_watch.start()
        log.info(f"odds_watch started (every {ODDS_POLL_MINUTES}m, alerts at {CLV_ALERT_PTS}pts)")
    if ODDS_API_KEY and not score_watch.is_running():
        score_watch.start()
        log.info("score_watch started ("
                 + (f"fixed every {SCORE_POLL_MINUTES}m" if _SCORE_POLL_FIXED
                    else f"adaptive, currently every {_score_poll_minutes()}m")
                 + f", auto-grade={'on' if AUTO_GRADE else 'off'})")

    await update_status_message()

    log.info(f"Logged in as {bot.user} (id={bot.user.id}) — {len(GROUPS)} group(s) configured")


async def run_system_audit(reason="manual"):
    """Run profit audit across all groups, post results to each group's stats channel."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM bets WHERE status != 'pending'")
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        bets = [dict(zip(cols, row)) for row in rows]

        fixed = []
        null_profit = []
        for bet in bets:
            correct = calc_profit(bet["odds"], bet["stake"], bet["status"], bet.get("potential_payout"))
            stored  = bet["profit"]
            if correct is None:
                guild   = bet.get("guild_id") or "@me"
                channel = bet.get("channel_id", "")
                msg_id  = bet.get("message_id", "")
                link    = f"https://discord.com/channels/{guild}/{channel}/{msg_id}"
                null_profit.append(f"{bet['description'] or 'unknown'} — [jump to bet]({link})")
                continue
            if stored is None or abs(float(stored) - float(correct)) > 0.01:
                await db.execute("UPDATE bets SET profit = ? WHERE message_id = ?",
                                 (correct, bet["message_id"]))
                fixed.append(f"{bet['description'] or 'unknown'}: was {stored} → {'+' if correct>=0 else ''}{correct:.2f}")
        if fixed:
            await db.commit()

        # Record this audit run
        await db.execute(
            "INSERT OR REPLACE INTO audit_schedule (key, last_run) VALUES ('last_audit', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await db.commit()

    total_checked = len(bets)
    if not fixed and not null_profit:
        log.info(f"Audit ({reason}): all {total_checked} bets look correct — nothing to fix")
    else:
        log.info(f"Audit ({reason}): fixed {len(fixed)}, {len(null_profit)} uncalculable, {total_checked} checked")

    if fixed or null_profit:
        await post_monitor(
            f"Audit Complete — {reason.title()}",
            f"Fixed **{len(fixed)}** bet(s). **{len(null_profit)}** still uncalculable. {total_checked} total checked.",
            level="warning" if null_profit else "ok",
        )
        for g in GROUPS:
            stats_id = g.get("stats_channel_id")
            if not stats_id:
                continue
            channel = bot.get_channel(int(stats_id))
            if channel is None:
                continue

            embed = discord.Embed(
                title=f"🔧 System Audit — {reason.title()}",
                color=discord.Color.orange(),
            )
            if fixed:
                sample = "\n".join(f"• {f}" for f in fixed[:10])
                if len(fixed) > 10:
                    sample += f"\n... and {len(fixed)-10} more"
                embed.add_field(name=f"Fixed {len(fixed)} bet(s)", value=sample, inline=False)
            if null_profit:
                embed.add_field(
                    name=f"⚠️ {len(null_profit)} still can't be calculated",
                    value="\n".join(f"• {b}" for b in null_profit[:8])
                          + (f"\n...and {len(null_profit)-8} more" if len(null_profit) > 8 else "")
                          + "\nUse `/fixbet` on each one.",
                    inline=False,
                )
            embed.set_footer(text=f"{total_checked} bets checked · {datetime.now(CENTRAL).strftime('%b %d %I:%M %p CT')}")
            try:
                await channel.send(embed=embed)
            except Exception:
                log.exception(f"failed to post audit results to {g['name']}")


@tasks.loop(hours=12)
async def biweekly_audit():
    """Check every 12 hours whether 14 days have passed since the last audit and run if so."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT last_run FROM audit_schedule WHERE key = 'last_audit'")
        row = await cur.fetchone()

    if row:
        last = datetime.fromisoformat(row[0])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        days_since = (datetime.now(timezone.utc) - last).days
        if days_since < 14:
            return

    log.info("Bi-weekly audit triggered")
    await run_system_audit(reason="bi-weekly")


@biweekly_audit.before_loop
async def before_biweekly_audit():
    await bot.wait_until_ready()


@tasks.loop(hours=1)
async def cleanup_stale_confirmations():
    """Prune _awaiting_confirmation entries older than CONFIRMATION_TTL_HOURS."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CONFIRMATION_TTL_HOURS)
    stale = [
        mid for mid, entry in _awaiting_confirmation.items()
        if datetime.fromisoformat(entry.get("created_at", "1970-01-01")).replace(tzinfo=timezone.utc) < cutoff
    ]
    for mid in stale:
        entry = _awaiting_confirmation.pop(mid, None)
        if entry:
            log.info(f"Pruned stale confirmation entry {mid} for {entry.get('username')}")
            await post_monitor(
                "Confirmation Expired",
                f"**{entry.get('username')}** never confirmed/rejected their flagged bet. Card greyed out — use `/logbet` to re-enter.\n[Source]({entry.get('source_url','')})",
                level="warning",
            )
            # Try to edit the expired card so user knows it's dead
            try:
                ch = bot.get_channel(int(entry["channel_id"]))
                if ch:
                    msg = await ch.fetch_message(int(mid))
                    exp_embed = msg.embeds[0] if msg.embeds else None
                    if exp_embed:
                        exp_embed.set_footer(text="⏰ Expired — use /logbet to re-enter this bet manually")
                        exp_embed.color = discord.Color.dark_grey()
                        await msg.edit(embed=exp_embed)
                        await msg.clear_reactions()
            except Exception:
                pass


@cleanup_stale_confirmations.before_loop
async def before_cleanup():
    await bot.wait_until_ready()


@tasks.loop(minutes=30)
async def status_heartbeat():
    """Update the pinned status message every 30 minutes."""
    await update_status_message()


@status_heartbeat.before_loop
async def before_heartbeat():
    await bot.wait_until_ready()


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if is_managed_output_or_stats_channel(message.channel.id):
        return
    # Also ignore the monitor/status channel
    if MONITOR_CHANNEL_ID and str(message.channel.id) == str(MONITOR_CHANNEL_ID):
        return

    group = group_for_bet_channel(message.channel.id)
    if GROUPS and not group:
        # GROUPS is configured but this channel isn't anyone's intake channel
        await bot.process_commands(message)
        return

    image_attachments = [
        a for a in message.attachments if a.content_type and a.content_type.startswith("image/")
    ]
    if not image_attachments:
        await bot.process_commands(message)
        return

    group_name = group["name"] if group else "default"
    output_channel_id = group.get("output_channel_id") if group else None

    attachment = image_attachments[0]
    img_bytes = await attachment.read()
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    media_type = detect_media_type(img_bytes, attachment.content_type)

    processing_msg = None
    if output_channel_id:
        try:
            await message.add_reaction("⏳")
        except discord.HTTPException:
            pass
    else:
        processing_msg = await message.reply("Reading bet slip...")

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        },
                        {"type": "text", "text": PARSE_PROMPT},
                    ],
                }
            ],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text.strip())
    except Exception:
        log.exception("parse failed")
        await post_monitor(
            "Parse Failed",
            f"{message.author.display_name} posted a slip that couldn't be read.\n[View screenshot]({message.jump_url})",
            level="warning",
        )
        if output_channel_id:
            try:
                await message.remove_reaction("⏳", bot.user)
            except discord.HTTPException:
                pass
            await message.add_reaction("⚠️")
        else:
            await processing_msg.edit(
                content="Couldn't read that slip clearly. Try a clearer screenshot, or log it manually with `/logbet`."
            )
        return

    # ── Validate parsed data ────────────────────────────────────────────
    severity, val_msg = validate_parsed_bet(data)

    if severity == "error":
        await post_monitor(
            "Bet Not Saved — Unreadable Slip",
            f"{message.author.mention} · {val_msg}\n[View screenshot]({message.jump_url})",
            level="error",
        )
        if output_channel_id:
            try:
                await message.remove_reaction("⏳", bot.user)
            except discord.HTTPException:
                pass
            await message.add_reaction("⚠️")
            target_channel = bot.get_channel(int(output_channel_id))
            await target_channel.send(
                f"⚠️ {message.author.mention} — {val_msg}\n[View screenshot]({message.jump_url})"
            )
        else:
            await processing_msg.edit(content=f"⚠️ {val_msg}")
        return

    embed = build_embed(data, message.author.display_name, status="pending")

    # Add warning field to embed if validation flagged something
    if severity == "warning" and val_msg:
        embed.add_field(name="⚠️ Review needed", value=val_msg, inline=False)

    if output_channel_id:
        target_channel = bot.get_channel(int(output_channel_id))
        embed.add_field(name="Source", value=f"[jump to screenshot]({message.jump_url})", inline=False)
        try:
            await message.remove_reaction("⏳", bot.user)
        except discord.HTTPException:
            pass
        await message.add_reaction("✅")
        bet_msg = await target_channel.send(embed=embed)
    else:
        await processing_msg.delete()
        bet_msg = await message.reply(embed=embed)

    # ── If flagged: require poster confirmation before saving ───────────
    if severity == "warning":
        await post_monitor(
            "Bet Flagged for Review",
            f"{message.author.display_name} · {val_msg}\n[View card]({bet_msg.jump_url})",
            level="warning",
        )
        await bet_msg.add_reaction(CONFIRM_EMOJI)
        await bet_msg.add_reaction(REJECT_EMOJI)
        _awaiting_confirmation[str(bet_msg.id)] = {
            "data": data,
            "group_name": group_name,
            "user_id": str(message.author.id),
            "username": message.author.display_name,
            "source_url": message.jump_url,
            "channel_id": str(bet_msg.channel.id),
            "guild_id": str(message.guild.id) if message.guild else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        # Edit in a footer note so the user knows what to do if the bot restarts
        try:
            exp_embed = bet_msg.embeds[0]
            exp_embed.set_footer(text=f"React {CONFIRM_EMOJI} to save · {REJECT_EMOJI} to discard · expires in {CONFIRMATION_TTL_HOURS}h (use /logbet if reactions stop working)")
            await bet_msg.edit(embed=exp_embed)
        except Exception:
            pass
        await bot.process_commands(message)
        return   # ← don't save to DB yet; wait for confirmation reaction

    # ── Clean parse — save immediately ─────────────────────────────────
    data, _lg_note = await asyncio.get_event_loop().run_in_executor(
        None, verify_league, data)
    if _lg_note:
        try:
            await bet_msg.reply(f"ℹ️ {_lg_note}", mention_author=False)
        except Exception:
            pass
    for emoji in REACTIONS:
        await bet_msg.add_reaction(emoji)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bets (message_id, channel_id, guild_id, user_id, username, group_name, sportsbook,
                sport, league, bet_type, description, legs, odds, stake, potential_payout, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                str(bet_msg.id),
                str(bet_msg.channel.id),
                str(message.guild.id) if message.guild else None,
                str(message.author.id),
                message.author.display_name,
                group_name,
                data.get("sportsbook"),
                data.get("sport"),
                data.get("league"),
                data.get("bet_type"),
                data.get("description"),
                json.dumps(data.get("legs") or []),
                data.get("odds"),
                data.get("stake"),
                data.get("potential_payout"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()

    await sheets_log_bet(data, message.author.display_name, group_name, bet_msg.id)
    await refresh_live_stats(group_name)
    await bot.process_commands(message)


async def _handle_reaction(reaction, user):
    if user.bot:
        return
    emoji = str(reaction.emoji)
    msg_id = str(reaction.message.id)

    # ── Handle pending confirmation (flagged parse) ─────────────────────
    if msg_id in _awaiting_confirmation:
        pending = _awaiting_confirmation[msg_id]
        if str(user.id) != pending["user_id"]:
            return   # only original poster can confirm/reject

        if emoji == REJECT_EMOJI:
            # Poster rejected — delete card, prompt manual entry
            del _awaiting_confirmation[msg_id]
            try:
                await reaction.message.delete()
            except discord.HTTPException:
                pass
            try:
                poster = await bot.fetch_user(int(pending["user_id"]))
                await poster.send(
                    f"Bet slip discarded. Use `/logbet` in the server to enter it manually.\n"
                    f"[Original screenshot]({pending['source_url']})"
                )
            except Exception:
                pass
            return

        if emoji == CONFIRM_EMOJI:
            # Poster confirmed — save to DB and sheet
            del _awaiting_confirmation[msg_id]
            data = pending["data"]
            group_name = pending["group_name"]

            # League verification runs here too — flagged parses were
            # bypassing it, which is how WNBA bets kept slipping in as NBA
            data, _lg_note = await asyncio.get_event_loop().run_in_executor(
                None, verify_league, data)
            if _lg_note:
                try:
                    await reaction.message.channel.send(f"ℹ️ {_lg_note}")
                except Exception:
                    pass

            # Rebuild embed without the warning field, add grade reactions
            clean_embed = build_embed(data, pending["username"], status="pending")
            clean_embed.add_field(
                name="Source", value=f"[jump to screenshot]({pending['source_url']})", inline=False
            )
            try:
                await reaction.message.edit(embed=clean_embed)
                await reaction.message.clear_reactions()
            except discord.HTTPException:
                pass
            for grade_emoji in REACTIONS:
                await reaction.message.add_reaction(grade_emoji)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """
                    INSERT INTO bets (message_id, channel_id, guild_id, user_id, username, group_name,
                        sportsbook, sport, league, bet_type, description, legs, odds, stake,
                        potential_payout, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        msg_id,
                        pending["channel_id"],
                        pending["guild_id"],
                        pending["user_id"],
                        pending["username"],
                        group_name,
                        data.get("sportsbook"),
                        data.get("sport"),
                        data.get("league"),
                        data.get("bet_type"),
                        data.get("description"),
                        json.dumps(data.get("legs") or []),
                        data.get("odds"),
                        data.get("stake"),
                        data.get("potential_payout"),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                await db.commit()

            await sheets_log_bet(data, pending["username"], group_name, int(msg_id))
            await refresh_live_stats(group_name)
            return

    if emoji not in REACTIONS:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM bets WHERE message_id = ?", (str(reaction.message.id),))
        row = await cur.fetchone()
        if not row:
            return
        cols = [d[0] for d in cur.description]
        bet = dict(zip(cols, row))

        if bet["status"] != "pending":
            if not bet.get("auto_graded"):
                log.info(f"Reaction ignored — bet {reaction.message.id} already settled as {bet['status']}")
                return
            # Human override of an auto-grade — the human always wins
            log.info(f"Human override of auto-grade on {reaction.message.id}: "
                     f"{bet['status']} -> {REACTIONS.get(emoji)}")
        if not ALLOW_ANYONE_TO_SETTLE and str(user.id) != bet["user_id"]:
            log.info(f"Reaction ignored — {user.name} is not the bet owner and ALLOW_ANYONE_TO_SETTLE=false")
            return

        status = REACTIONS[emoji]
        profit = calc_profit(bet["odds"], bet["stake"], status, bet.get("potential_payout"))

        await db.execute(
            "UPDATE bets SET status = ?, profit = ?, settled_at = ?, auto_graded = 0 "
            "WHERE message_id = ?",
            (status, profit, datetime.now(timezone.utc).isoformat(), str(reaction.message.id)),
        )
        await db.commit()

        # ── grade verification: read back and confirm the write stuck ──
        vcur = await db.execute(
            "SELECT status, profit FROM bets WHERE message_id = ?",
            (str(reaction.message.id),))
        vrow = await vcur.fetchone()
        if not vrow or vrow[0] != status or (
            profit is not None and vrow[1] is not None
            and abs(float(vrow[1]) - float(profit)) > 0.01
        ):
            log.error(f"GRADE VERIFY FAILED for {reaction.message.id}: "
                      f"wanted ({status},{profit}) got {vrow}")
            await post_monitor("Grade Verification Failed",
                               f"Bet {reaction.message.id} may not have saved correctly. "
                               f"Run /audit to check.", level="error")
        else:
            log.info(f"grade verified: {reaction.message.id} -> {status} ({profit})")

    data = {
        "sportsbook": bet["sportsbook"],
        "sport":      bet.get("sport"),
        "league":     bet.get("league"),
        "bet_type":   bet["bet_type"],
        "description": bet["description"],
        "legs":        json.loads(bet["legs"] or "[]"),
        "odds":        bet["odds"],
        "stake":       bet["stake"],
        "potential_payout": bet["potential_payout"],
    }

    # Pull updated record for this user to show on the settled card
    group_name = bet.get("group_name") or "default"
    async with aiosqlite.connect(DB_PATH) as db:
        cur2 = await db.execute(
            """SELECT SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),
                      COALESCE(SUM(profit), 0)
               FROM bets WHERE user_id=? AND group_name=? AND status != 'pending'""",
            (bet["user_id"], group_name),
        )
        rec = await cur2.fetchone()
    record_str = f"{rec[0]}-{rec[1]}  ·  {'+' if rec[2] >= 0 else ''}{rec[2]:.2f} all time" if rec else None

    embed = build_embed(data, bet["username"], status=status, profit=profit, record=record_str)
    await reaction.message.edit(embed=embed)

    # Calculate cumulative P&L from DB sorted by settled_at — accurate even for out-of-order grading
    async with aiosqlite.connect(DB_PATH) as db:
        cur_cum = await db.execute(
            """SELECT COALESCE(SUM(profit), 0) FROM bets
               WHERE user_id=? AND group_name=? AND status!='pending'
               AND settled_at <= (SELECT settled_at FROM bets WHERE message_id=?)""",
            (bet["user_id"], group_name, str(reaction.message.id)),
        )
        (cumulative_pnl,) = await cur_cum.fetchone()

    await sheets_update_bet(bet["username"], reaction.message.id, status, profit,
                            cumulative_pnl=round(cumulative_pnl, 2))
    await refresh_live_stats(group_name)



@bot.event
async def on_raw_reaction_add(payload):
    """Cache-proof grading: fires for ALL messages, including bet cards posted
    before the last restart. (on_reaction_add only fires for messages in the
    bot's memory cache, which empties on every deploy — that's why grading
    used to break after updates.)"""
    if bot.user and payload.user_id == bot.user.id:
        return
    emoji = str(payload.emoji)
    if emoji not in REACTIONS and emoji not in (CONFIRM_EMOJI, REJECT_EMOJI):
        return
    msg_id = str(payload.message_id)

    # cheap relevance gate before any Discord API fetches
    if msg_id not in _awaiting_confirmation:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT 1 FROM bets WHERE message_id = ?", (msg_id,))
            if not await cur.fetchone():
                return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception:
            return
    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception:
        return

    user = payload.member
    if user is None:
        try:
            user = await bot.fetch_user(payload.user_id)
        except Exception:
            return
    if getattr(user, "bot", False):
        return

    reaction = types.SimpleNamespace(emoji=payload.emoji, message=message)
    await _handle_reaction(reaction, user)

@bot.tree.command(name="logbet", description="Manually log a bet (use if the screenshot parse fails)")
@app_commands.describe(
    sportsbook="Sportsbook (e.g. DraftKings, FanDuel, Kalshi)",
    description="What's the bet, in plain words",
    odds="American odds, e.g. -110 or 150",
    stake="Dollars risked",
    sport="Sport (e.g. Football, Basketball, Baseball)",
    league="League (e.g. NFL, NBA, MLB, NHL, NCAAF)",
    bet_type="Bet type",
    potential_payout="Total payout if it wins (optional)",
)
@app_commands.choices(bet_type=[
    app_commands.Choice(name="Moneyline", value="moneyline"),
    app_commands.Choice(name="Spread",    value="spread"),
    app_commands.Choice(name="Total",     value="total"),
    app_commands.Choice(name="Parlay",    value="parlay"),
    app_commands.Choice(name="Prop",      value="prop"),
    app_commands.Choice(name="Future",    value="future"),
    app_commands.Choice(name="Other",     value="other"),
])
async def logbet(
    interaction: discord.Interaction,
    sportsbook: str,
    description: str,
    odds: int,
    stake: float,
    sport: str = None,
    league: str = None,
    bet_type: app_commands.Choice[str] = None,
    potential_payout: float = None,
):
    group = group_for_any_channel(interaction.channel.id)
    if GROUPS and not group:
        await interaction.response.send_message(
            "Run this inside one of the bet-tracking channels for a group.", ephemeral=True
        )
        return
    group_name = group["name"] if group else "default"
    output_channel_id = group.get("output_channel_id") if group else None
    bt = bet_type.value if bet_type else "other"

    data = {
        "sportsbook": sportsbook,
        "sport": sport,
        "league": league,
        "bet_type": bt,
        "description": description,
        "legs": [description],
        "odds": odds,
        "stake": stake,
        "potential_payout": potential_payout,
    }
    embed = build_embed(data, interaction.user.display_name, status="pending")

    if output_channel_id:
        target_channel = bot.get_channel(int(output_channel_id))
        msg = await target_channel.send(embed=embed)
        await interaction.response.send_message(f"Logged — see {target_channel.mention}", ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()

    for emoji in REACTIONS:
        await msg.add_reaction(emoji)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bets (message_id, channel_id, guild_id, user_id, username, group_name, sportsbook,
                sport, league, bet_type, description, legs, odds, stake, potential_payout, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                str(msg.id),
                str(msg.channel.id),
                str(interaction.guild.id) if interaction.guild else None,
                str(interaction.user.id),
                interaction.user.display_name,
                group_name,
                sportsbook,
                sport,
                league,
                bt,
                description,
                json.dumps([description]),
                odds,
                stake,
                potential_payout,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()

    await sheets_log_bet(data, interaction.user.display_name, group_name, msg.id)
    await refresh_live_stats(group_name)


@bot.tree.command(name="stats", description="Show betting stats for a user (or yourself)")
async def stats(interaction: discord.Interaction, member: discord.Member = None):
    group = group_for_any_channel(interaction.channel.id)
    if GROUPS and not group:
        await interaction.response.send_message(
            "Run this inside one of the bet-tracking channels for a group.", ephemeral=True
        )
        return
    group_name = group["name"] if group else None
    member = member or interaction.user

    query = (
        "SELECT status, COUNT(*), COALESCE(SUM(profit),0) FROM bets "
        "WHERE user_id = ? AND status != 'pending'"
    )
    params = [str(member.id)]
    if group_name:
        query += " AND group_name = ?"
        params.append(group_name)
    query += " GROUP BY status"

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        rows = await cur.fetchall()

    record = {"won": 0, "lost": 0, "push": 0}
    total_profit = 0.0
    for status, count, profit_sum in rows:
        record[status] = count
        total_profit += profit_sum

    settled = record["won"] + record["lost"] + record["push"]
    decided = record["won"] + record["lost"]
    win_pct = (record["won"] / decided * 100) if decided > 0 else 0.0

    embed = discord.Embed(title=f"{member.display_name}'s bet record", color=discord.Color.blue())
    embed.add_field(name="Record", value=f"{record['won']}-{record['lost']}-{record['push']}")
    embed.add_field(name="Win %", value=f"{win_pct:.1f}%")
    embed.add_field(name="Net Profit", value=f"{'+' if total_profit >= 0 else ''}{total_profit:.2f}")
    embed.set_footer(text=f"{settled} settled bets")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Net profit leaderboard for this group")
async def leaderboard(interaction: discord.Interaction):
    group = group_for_any_channel(interaction.channel.id)
    if GROUPS and not group:
        await interaction.response.send_message(
            "Run this inside one of the bet-tracking channels for a group.", ephemeral=True
        )
        return
    group_name = group["name"] if group else None

    query = """
        SELECT username, COALESCE(SUM(profit),0) as total,
               SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) as losses
        FROM bets WHERE status != 'pending'
    """
    params = []
    if group_name:
        query += " AND group_name = ?"
        params.append(group_name)
    query += " GROUP BY user_id ORDER BY total DESC"

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        rows = await cur.fetchall()

    if not rows:
        await interaction.response.send_message("No settled bets yet.")
        return

    lines = [
        f"{i}. **{username}** — {wins}-{losses} — {'+' if total >= 0 else ''}{total:.2f}"
        for i, (username, total, wins, losses) in enumerate(rows, start=1)
    ]
    embed = discord.Embed(title="Leaderboard", description="\n".join(lines), color=discord.Color.purple())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="refresh", description="Force the live scoreboard to update right now")
async def refresh(interaction: discord.Interaction):
    group = group_for_any_channel(interaction.channel.id)
    if GROUPS and not group:
        await interaction.response.send_message(
            "Run this inside one of the bet-tracking channels for a group.", ephemeral=True
        )
        return
    group_name = group["name"] if group else "default"
    if not (group and group.get("stats_channel_id")):
        await interaction.response.send_message("No stats channel configured for this group.", ephemeral=True)
        return
    await refresh_live_stats(group_name)
    await interaction.response.send_message("Scoreboard refreshed.", ephemeral=True)


@bot.tree.command(name="pending", description="List your pending (unsettled) bets")
async def pending(interaction: discord.Interaction):
    group = group_for_any_channel(interaction.channel.id)
    if GROUPS and not group:
        await interaction.response.send_message(
            "Run this inside one of the bet-tracking channels for a group.", ephemeral=True
        )
        return
    group_name = group["name"] if group else None

    query = "SELECT description, message_id, channel_id FROM bets WHERE user_id = ? AND status = 'pending'"
    params = [str(interaction.user.id)]
    if group_name:
        query += " AND group_name = ?"
        params.append(group_name)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        rows = await cur.fetchall()

    if not rows:
        await interaction.response.send_message("No pending bets.")
        return

    guild_id = interaction.guild.id if interaction.guild else 0
    lines = [
        f"• {desc or '(bet)'} — https://discord.com/channels/{guild_id}/{chan}/{mid}"
        for desc, mid, chan in rows
    ]
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="filter", description="See your stats filtered by sport, league, or bet type")
@app_commands.describe(
    sport="e.g. Football, Basketball, Baseball",
    league="e.g. NFL, NBA, MLB, NHL, NCAAF",
    bet_type="Bet type to filter by",
    member="Whose stats (default: yours)",
)
@app_commands.choices(bet_type=[
    app_commands.Choice(name="Moneyline", value="moneyline"),
    app_commands.Choice(name="Spread",    value="spread"),
    app_commands.Choice(name="Total",     value="total"),
    app_commands.Choice(name="Parlay",    value="parlay"),
    app_commands.Choice(name="Prop",      value="prop"),
    app_commands.Choice(name="Future",    value="future"),
    app_commands.Choice(name="Other",     value="other"),
])
async def filter_stats(
    interaction: discord.Interaction,
    sport: str = None,
    league: str = None,
    bet_type: app_commands.Choice[str] = None,
    member: discord.Member = None,
):
    group = group_for_any_channel(interaction.channel.id)
    if GROUPS and not group:
        await interaction.response.send_message(
            "Run this inside one of the bet-tracking channels for a group.", ephemeral=True
        )
        return
    if not sport and not league and not bet_type:
        await interaction.response.send_message(
            "Provide at least one filter: sport, league, or bet type.", ephemeral=True
        )
        return

    group_name = group["name"] if group else None
    member = member or interaction.user

    query = """
        SELECT COALESCE(SUM(profit), 0),
               SUM(CASE WHEN status='won'  THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='push' THEN 1 ELSE 0 END),
               COUNT(*) as total
        FROM bets
        WHERE user_id = ? AND status != 'pending'
    """
    params = [str(member.id)]
    filter_labels = []

    if group_name:
        query += " AND group_name = ?"
        params.append(group_name)
    if sport:
        query += " AND LOWER(sport) = LOWER(?)"
        params.append(sport)
        filter_labels.append(f"Sport: {sport.title()}")
    if league:
        query += " AND LOWER(league) = LOWER(?)"
        params.append(league)
        filter_labels.append(f"League: {league.upper()}")
    if bet_type:
        query += " AND bet_type = ?"
        params.append(bet_type.value)
        filter_labels.append(f"Type: {bet_type.name}")

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        row = await cur.fetchone()

        # Also pull a breakdown by sport/league for context
        breakdown_query = """
            SELECT COALESCE(sport,'?'), COALESCE(league,'?'),
                   SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),
                   COALESCE(SUM(profit),0)
            FROM bets WHERE user_id = ? AND status != 'pending'
        """
        b_params = [str(member.id)]
        if group_name:
            breakdown_query += " AND group_name = ?"
            b_params.append(group_name)
        if sport:
            breakdown_query += " AND LOWER(sport) = LOWER(?)"
            b_params.append(sport)
        if league:
            breakdown_query += " AND LOWER(league) = LOWER(?)"
            b_params.append(league)
        if bet_type:
            breakdown_query += " AND bet_type = ?"
            b_params.append(bet_type.value)
        breakdown_query += " GROUP BY sport, league ORDER BY SUM(profit) DESC"
        cur2 = await db.execute(breakdown_query, b_params)
        breakdown = await cur2.fetchall()

    net, wins, losses, pushes, total = row
    decided = wins + losses
    win_pct = f"{wins/decided*100:.1f}%" if decided > 0 else "—"
    net_str = f"{'+' if net >= 0 else ''}{net:.2f}"
    color = discord.Color.green() if net >= 0 else discord.Color.red()

    title = f"{member.display_name} — {' / '.join(filter_labels)}"
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Record", value=f"{wins}-{losses}-{pushes}", inline=True)
    embed.add_field(name="Net",    value=net_str,                      inline=True)
    embed.add_field(name="Win %",  value=win_pct,                      inline=True)

    if breakdown and len(breakdown) > 1:
        lines = [
            f"{sp}/{lg} — {w}-{l} — {'+' if p>=0 else ''}{p:.2f}"
            for sp, lg, w, l, p in breakdown
        ]
        embed.add_field(name="Breakdown", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"{total} settled bets matched")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="sheetsync", description="Compare DB vs Google Sheet and remove orphaned rows")
async def sheetsync(interaction: discord.Interaction):
    group = group_for_any_channel(interaction.channel.id)
    if GROUPS and not group:
        await interaction.response.send_message(
            "Run this inside one of the bet-tracking channels.", ephemeral=True
        )
        return
    group_name = group["name"] if group else None

    if not SHEETS_AVAILABLE or not SPREADSHEET_ID:
        await interaction.response.send_message(
            "Google Sheets is not configured.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    # Get ALL message IDs from the DB — no group filter
    # (sheet rows belong to users across groups, filtering by group causes false orphan detection)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT message_id, username, description FROM bets")
        db_rows = await cur.fetchall()

    db_ids = {str(r[0]): {"username": r[1], "desc": r[2] or "unknown"} for r in db_rows}

    deleted = []
    missing = []
    errors = []

    def _sync():
        try:
            client = _get_sheets_client()
            if not client:
                errors.append("Could not connect to Google Sheets")
                return
            ss = client.open_by_key(SPREADSHEET_ID)

            for ws in ss.worksheets():
                title = ws.title
                if title in ("Sheet1",) or title.endswith(" Dashboard") or title.endswith(" Calendar"):
                    continue

                try:
                    msg_ids = ws.col_values(COL_MSG_ID)  # column T
                except Exception as e:
                    errors.append(f"{title}: {e}")
                    continue

                # Find orphaned rows (in sheet but not in DB) — collect in reverse order
                orphan_rows = []
                for idx, mid in enumerate(msg_ids[1:], start=2):
                    if mid and str(mid) not in db_ids:
                        try:
                            desc = ws.cell(idx, 5).value or "unknown"
                        except Exception:
                            desc = "unknown"
                        orphan_rows.append((idx, mid, desc))

                # Delete in reverse row order so indices stay valid
                for row_num, mid, desc in sorted(orphan_rows, reverse=True):
                    try:
                        ws.delete_rows(row_num)
                        deleted.append(f"{desc} (@{title})")
                    except Exception as e:
                        errors.append(f"Could not delete row {row_num} in {title}: {e}")

            # Check DB bets missing from sheet
            for mid, info in db_ids.items():
                uname = info["username"]
                if not uname:
                    continue
                try:
                    ws = ss.worksheet(uname)
                    if _find_row_by_message_id(ws, mid) is None:
                        missing.append(f"{info['desc']} (@{uname})")
                except Exception:
                    pass

        except Exception as e:
            errors.append(f"Sync failed: {e}")
            log.exception("sheetsync error")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: _sync())

    lines = ["**🔍 Sheet Sync**\n"]
    if deleted:
        lines.append(f"🗑️ **Removed {len(deleted)} orphaned row(s):**")
        lines.extend(f"• {d}" for d in deleted[:15])
        if len(deleted) > 15:
            lines.append(f"  ...and {len(deleted)-15} more")
    if missing:
        lines.append(f"\n⚠️ **{len(missing)} DB bet(s) not in sheet** (run backfill to restore):")
        lines.extend(f"• {m}" for m in missing[:10])
        if len(missing) > 10:
            lines.append(f"  ...and {len(missing)-10} more")
    if errors:
        lines.append(f"\n❌ **Errors:** {'; '.join(errors[:3])}")
    if not deleted and not missing and not errors:
        lines.append("✅ DB and sheet are fully in sync.")

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n..."
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="fixbet", description="Manually set the payout on a bet the audit couldn't calculate")
@app_commands.describe(
    message_link="Link to the bet card (shown in /audit results)",
    stake="Amount risked in dollars",
    payout="Total payout if it wins (stake + profit)",
)
async def fixbet(
    interaction: discord.Interaction,
    message_link: str,
    stake: float,
    payout: float,
):
    parts = message_link.strip().rstrip("/").split("/")
    if not parts[-1].isdigit():
        await interaction.response.send_message(
            "That doesn't look like a valid message link.", ephemeral=True
        )
        return

    message_id = parts[-1]

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM bets WHERE message_id = ?", (message_id,))
        row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(
                "Couldn't find that bet in the database.", ephemeral=True
            )
            return
        cols = [d[0] for d in cur.description]
        bet = dict(zip(cols, row))

        profit = calc_profit(None, stake, bet["status"], payout)

        await db.execute(
            "UPDATE bets SET stake = ?, potential_payout = ?, profit = ? WHERE message_id = ?",
            (stake, payout, profit, message_id),
        )
        await db.commit()

    group_name = bet.get("group_name") or "default"

    # Update the bet card embed
    data = {
        "sportsbook":      bet["sportsbook"],
        "sport":           bet.get("sport"),
        "league":          bet.get("league"),
        "bet_type":        bet["bet_type"],
        "description":     bet["description"],
        "legs":            json.loads(bet["legs"] or "[]"),
        "odds":            bet["odds"],
        "stake":           stake,
        "potential_payout": payout,
    }
    async with aiosqlite.connect(DB_PATH) as db:
        cur2 = await db.execute(
            """SELECT SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),
                      COALESCE(SUM(profit), 0)
               FROM bets WHERE user_id=? AND group_name=? AND status != 'pending'""",
            (bet["user_id"], group_name),
        )
        rec = await cur2.fetchone()
    record_str = f"{rec[0]}-{rec[1]}  ·  {'+' if rec[2] >= 0 else ''}{rec[2]:.2f} all time" if rec else None

    embed = build_embed(data, bet["username"], status=bet["status"], profit=profit, record=record_str)
    try:
        ch = bot.get_channel(int(bet["channel_id"]))
        if ch:
            msg = await ch.fetch_message(int(message_id))
            await msg.edit(embed=embed)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass

    profit_str = f"{'+' if profit >= 0 else ''}{profit:.2f}" if profit is not None else "unknown"
    await interaction.response.send_message(
        f"Fixed — **{bet.get('description') or 'bet'}** profit set to **{profit_str}**",
        ephemeral=True,
    )
    await refresh_live_stats(group_name)




# ═══════════════════════════════════════════════════════════════════════
# CLV / LIVE ODDS / LINE-MOVEMENT ENGINE (The Odds API)
# One polling loop feeds all three features from a single cached fetch.
# ═══════════════════════════════════════════════════════════════════════
ODDS_API_KEY      = os.environ.get("ODDS_API_KEY", "")
ODDS_POLL_MINUTES = int(os.environ.get("ODDS_POLL_MINUTES", "30"))
CLV_ALERT_PTS     = float(os.environ.get("CLV_ALERT_PTS", "3.5"))  # implied-prob pts

# (sport, league) -> The Odds API sport key. League wins over sport.
_LEAGUE_KEYS = {
    "MLB": "baseball_mlb", "NBA": "basketball_nba", "WNBA": "basketball_wnba",
    "NFL": "americanfootball_nfl", "NCAAF": "americanfootball_ncaaf",
    "NCAAB": "basketball_ncaab", "NHL": "icehockey_nhl",
    "EPL": "soccer_epl", "MLS": "soccer_usa_mls",
}
_SPORT_KEYS = {
    "baseball": "baseball_mlb", "basketball": "basketball_nba",
    "football": "americanfootball_nfl", "hockey": "icehockey_nhl",
}


def _odds_sport_key(sport, league):
    if league and str(league).upper().strip() in _LEAGUE_KEYS:
        return _LEAGUE_KEYS[str(league).upper().strip()]
    if sport and str(sport).lower().strip() in _SPORT_KEYS:
        return _SPORT_KEYS[str(sport).lower().strip()]
    return None


def _implied(american):
    a = float(american)
    return (-a / (-a + 100)) if a < 0 else (100 / (a + 100))


def _prob_to_american(p):
    if p <= 0 or p >= 1:
        return None
    return round(-p / (1 - p) * 100) if p >= 0.5 else round((1 - p) / p * 100)


def _fetch_sport_odds(sport_key):
    """One HTTP call per sport per poll: h2h + totals + spreads, US books."""
    import requests as _rq
    try:
        r = _rq.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
            params={"apiKey": ODDS_API_KEY, "regions": "us",
                    "markets": "h2h,totals,spreads", "oddsFormat": "american"},
            timeout=15)
        if r.status_code != 200:
            log.warning(f"odds api {sport_key} -> {r.status_code}: {r.text[:150]}")
            return []
        return r.json()
    except Exception:
        log.exception("odds api fetch failed")
        return []


def _team_frag(team_name):
    """'New York Yankees' -> 'yankees' (distinctive last word)."""
    return (team_name or "").strip().split(" ")[-1].lower()


def _consensus_price(game, market_key, pick):
    """Average implied prob across books for one outcome -> American odds.
    pick: for h2h a team name; for totals ('Over'/'Under', point);
    for spreads (team name, point)."""
    probs = []
    for bk in game.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != market_key:
                continue
            for oc in mkt.get("outcomes", []):
                if market_key == "h2h" and oc.get("name") == pick:
                    probs.append(_implied(oc["price"]))
                elif market_key == "totals" and oc.get("name") == pick[0] \
                        and oc.get("point") == pick[1]:
                    probs.append(_implied(oc["price"]))
                elif market_key == "spreads" and oc.get("name") == pick[0] \
                        and oc.get("point") == pick[1]:
                    probs.append(_implied(oc["price"]))
    if len(probs) < 2:   # strict: need 2+ books or it doesn't count
        return None
    return _prob_to_american(sum(probs) / len(probs))


_TOTAL_RE = re.compile(r"\b(over|under)\s*([0-9]+(?:\.5)?)", re.I)
_SPREAD_RE = re.compile(r"([+-][0-9]+(?:\.5)?)")


def _match_bet_price(bet, games):
    """Find the bet's game+market in the odds feed. Returns (american, commence_iso) or (None, None)."""
    desc = (bet.get("description") or "").lower()
    desc_raw = bet.get("description") or ""
    btype = (bet.get("bet_type") or "").lower()
    # strict: exactly one candidate game or we don't track this bet
    # (nickname OR abbreviation match — "LA -1.5" and "Dodgers -1.5" both work)
    cands = [g for g in games
             if _team_matches(g.get("home_team"), desc, desc_raw)
             or _team_matches(g.get("away_team"), desc, desc_raw)]
    if len(cands) != 1:
        return None, None
    for g in cands:
        home, away = g.get("home_team", ""), g.get("away_team", "")
        hf_match = _team_matches(home, desc, desc_raw)
        af_match = _team_matches(away, desc, desc_raw)
        both_named = hf_match and af_match
        commence = g.get("commence_time")
        if btype == "moneyline":
            if both_named:   # ambiguous side -> don't track
                return None, None
            team = home if hf_match else away
            return _consensus_price(g, "h2h", team), commence
        if btype == "total":
            m = _TOTAL_RE.search(desc)
            if m:
                side = m.group(1).capitalize()
                point = float(m.group(2))
                return _consensus_price(g, "totals", (side, point)), commence
        if btype == "spread":
            m = _SPREAD_RE.search(bet.get("description") or "")
            if m and not both_named:   # ambiguous side -> don't track
                team = home if hf_match else away
                point = float(m.group(1))
                return _consensus_price(g, "spreads", (team, point)), commence
    return None, None


async def sheets_update_odds(username, message_id, live=None, closing=None, clv=None, game_time=None):
    """Write Live Odds (U/21), Closing Odds (V/22), CLV pts (W/23), Game Time (X/24)."""
    client = _get_sheets_client()
    if client is None:
        return
    loop = asyncio.get_event_loop()

    def _upd():
        ss = client.open_by_key(SPREADSHEET_ID)
        ws = _get_or_create_data_sheet(ss, username)
        row = _find_row_by_message_id(ws, str(message_id))
        if row is None:
            return
        cells = []
        if live is not None:
            cells.append({"range": gspread.utils.rowcol_to_a1(row, 21), "values": [[live]]})
        if closing is not None:
            cells.append({"range": gspread.utils.rowcol_to_a1(row, 22), "values": [[closing]]})
        if clv is not None:
            cells.append({"range": gspread.utils.rowcol_to_a1(row, 23), "values": [[clv]]})
        if game_time is not None:
            cells.append({"range": gspread.utils.rowcol_to_a1(row, 24), "values": [[game_time]]})
        if cells:
            ws.batch_update(cells, value_input_option="USER_ENTERED")

    try:
        await loop.run_in_executor(None, lambda: _sheets_call(_upd))
    except Exception:
        log.exception("sheets_update_odds failed")





# ── NBA vs WNBA disambiguation ──────────────────────────────────────────
# Word-boundary matching keeps Suns(NBA)/Sun(WNBA) etc. separate.
_NBA_TEAMS = ["hawks","celtics","nets","hornets","bulls","cavaliers","cavs",
    "mavericks","mavs","nuggets","pistons","warriors","rockets","pacers",
    "clippers","lakers","grizzlies","heat","bucks","timberwolves","wolves",
    "pelicans","knicks","thunder","magic","76ers","sixers","suns",
    "blazers","kings","spurs","raptors","jazz","wizards"]
_WNBA_TEAMS = ["aces","liberty","lynx","storm","sun","mercury","sparks",
    "fever","sky","wings","mystics","dream","valkyries","tempo"]
# NOTE: "Fire" (Portland's 2026 expansion team) is deliberately excluded from
# this deterministic list -- Chicago Fire is an MLS soccer team, and a bare
# word-boundary match on "fire" would misfire in the opposite direction.
# The parse prompt still mentions it since Claude has full slip context to
# disambiguate; this regex-based fallback doesn't, so it stays conservative.

# Distinctive abbreviations: these codes can only mean one league
_NBA_ABBR = {"OKC","SAS","MEM","DEN","MIL","BKN","CLE","DET","HOU","MIA",
             "NOP","ORL","PHI","SAC","UTA","CHA","BOS","LAL","LAC","GSW","NYK"}
_WNBA_ABBR = {"LV","LVA","CONN","CON","GSV","NYL","SEA"}
# Shared-city codes: can't disambiguate alone — the day's schedules decide
_CITY_ABBR = {"MIN":"minnesota","PHX":"phoenix","PHO":"phoenix","CHI":"chicago",
              "ATL":"atlanta","IND":"indiana","DAL":"dallas","WAS":"washington",
              "WSH":"washington","LA":"los angeles","NY":"new york",
              "GS":"golden state","LV":"las vegas","SEA":"seattle",
              "CONN":"connecticut","CON":"connecticut","TOR":"toronto","POR":"portland"}


def _basketball_league_from_desc(desc):
    """Return 'NBA', 'WNBA', or None (no/ambiguous team evidence).
    Checks nicknames AND league-distinctive abbreviations (LAL, CONN...)."""
    text = desc or ""
    d = text.lower()
    toks = set(re.findall(r"\b[A-Z]{2,4}\b", text))   # case-sensitive codes
    nba = any(re.search(rf"\b{t}\b", d) for t in _NBA_TEAMS) or bool(toks & _NBA_ABBR)
    wnba = any(re.search(rf"\b{t}\b", d) for t in _WNBA_TEAMS) or bool(toks & _WNBA_ABBR)
    if nba and not wnba:
        return "NBA"
    if wnba and not nba:
        return "WNBA"
    return None


def _basketball_season_guess(created_iso):
    """July–September: WNBA only. Nov–April: NBA only. Else ambiguous."""
    try:
        m = int((created_iso or "")[5:7])
    except (ValueError, TypeError):
        return None
    if m in (7, 8, 9):
        return "WNBA"
    if m in (11, 12, 1, 2, 3, 4):
        return "NBA"
    return None



# ═══════════════════════════════════════════════════════════════════════
# AUTO-GRADER + MISGRADE DETECTOR (The Odds API /scores)
# Strict rules: one matched game, unambiguous side, completed final only.
# Anything questionable is left alone for manual grading.
# ═══════════════════════════════════════════════════════════════════════
# Adaptive polling: hammer the windows when games actually end, coast when
# they don't. All times US/Eastern. Setting SCORE_POLL_MINUTES in the env
# overrides adaptation with a flat interval.
SCORE_POLL_MINUTES = int(os.environ.get("SCORE_POLL_MINUTES", "10"))
_SCORE_POLL_FIXED = "SCORE_POLL_MINUTES" in os.environ


def _score_poll_minutes():
    """Poll cadence by when finals actually land (ET):
    5pm–midnight: every 5 min (night slates ending)
    weekend noon–5pm: every 7 min (afternoon slates)
    weekday noon–5pm: every 15 min (occasional day games)
    midnight–noon: every 60 min (dead — safety sweep only)"""
    if _SCORE_POLL_FIXED:
        return SCORE_POLL_MINUTES
    now = datetime.now(ZoneInfo("America/New_York"))
    h, weekend = now.hour, now.weekday() >= 5
    if h >= 17:
        return 5
    if h < 12:
        return 60
    return 7 if weekend else 15
AUTO_GRADE = os.environ.get("AUTO_GRADE", "1") == "1"
# sports where a tie/draw is a normal result -> never auto-grade moneylines
_DRAW_SPORTS = {"soccer_epl", "soccer_usa_mls"}


def _fetch_sport_scores(sport_key):
    import requests as _rq
    try:
        r = _rq.get(f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores",
                    params={"apiKey": ODDS_API_KEY, "daysFrom": 2}, timeout=15)
        if r.status_code != 200:
            log.warning(f"scores api {sport_key} -> {r.status_code}")
            return []
        return [g for g in r.json() if g.get("completed")]
    except Exception:
        log.exception("scores fetch failed")
        return []


def _game_scores(g):
    """Return (home_score, away_score) as ints or (None, None)."""
    try:
        m = {s.get("name"): int(s.get("score")) for s in (g.get("scores") or [])}
        return m.get(g.get("home_team")), m.get(g.get("away_team"))
    except (TypeError, ValueError):
        return None, None


def _score_result(bet, games, sport_key):
    """Determine won/lost/push from final scores. STRICT — returns
    (status, final_string) or (None, None) when anything is ambiguous."""
    desc = (bet.get("description") or "").lower()
    btype = (bet.get("bet_type") or "").lower()

    cands = [g for g in games
             if (_team_frag(g.get("home_team")) in desc)
             or (_team_frag(g.get("away_team")) in desc)]
    if len(cands) != 1:
        return None, None
    g = cands[0]
    hs, aw = _game_scores(g)
    if hs is None or aw is None:
        return None, None
    home, away = g.get("home_team", ""), g.get("away_team", "")
    hf, af = _team_frag(home), _team_frag(away)
    both = (hf in desc) and (af in desc)
    final = f"{home} {hs} – {away} {aw}"

    if btype == "moneyline":
        if both or sport_key in _DRAW_SPORTS:
            return None, None
        picked_home = hf in desc
        my, other = (hs, aw) if picked_home else (aw, hs)
        if my == other:
            return None, None          # tie — leave for a human
        return ("won" if my > other else "lost"), final

    if btype == "total":
        m = _TOTAL_RE.search(desc)
        if not m:
            return None, None
        side, point = m.group(1).lower(), float(m.group(2))
        tot = hs + aw
        if tot == point:
            return "push", final
        over = tot > point
        return ("won" if (side == "over") == over else "lost"), final

    if btype == "spread":
        m = _SPREAD_RE.search(bet.get("description") or "")
        if not m or both:
            return None, None
        point = float(m.group(1))
        picked_home = hf in desc
        my, other = (hs, aw) if picked_home else (aw, hs)
        margin = my + point - other
        if margin == 0:
            return "push", final
        return ("won" if margin > 0 else "lost"), final

    return None, None


async def _apply_auto_grade(bet, status, final):
    """Grade like the reaction flow: DB + sheet + verify + notice."""
    profit = calc_profit(bet.get("odds"), bet.get("stake"), status,
                         bet.get("potential_payout"))
    mid = str(bet["message_id"])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bets SET status=?, profit=?, settled_at=?, auto_graded=1, "
            "score_checked=1 WHERE message_id=?",
            (status, profit, datetime.now(timezone.utc).isoformat(), mid))
        await db.commit()
    await sheets_update_bet(bet.get("username", ""), mid, status, profit)

    emoji = {"won": "✅", "lost": "❌", "push": "↩️"}.get(status, "🤖")
    msg = (f"🤖 **Auto-graded {emoji} {status.upper()}** — "
           f"{(bet.get('description') or '?')[:70]}\n"
           f"Final: {final}"
           + (f"  ·  {'+' if (profit or 0) >= 0 else ''}${profit:.2f}" if profit is not None else "")
           + "\nWrong? Re-react on the original bet card to override.")
    try:
        ch = bot.get_channel(int(bet.get("channel_id") or 0))
        if ch:
            try:
                orig = await ch.fetch_message(int(mid))
                await orig.reply(msg, mention_author=False)
            except Exception:
                await ch.send(msg)
    except Exception:
        log.exception("auto-grade notice failed")


@tasks.loop(minutes=SCORE_POLL_MINUTES)
async def score_watch():
    if not ODDS_API_KEY:
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            pend = [dict(r) for r in await (await db.execute(
                "SELECT * FROM bets WHERE status='pending' "
                "AND lower(bet_type) IN ('moneyline','total','spread')")).fetchall()]
            recent = [dict(r) for r in await (await db.execute(
                "SELECT * FROM bets WHERE status IN ('won','lost','push') "
                "AND score_checked=0 AND settled_at >= datetime('now','-2 days') "
                "AND lower(bet_type) IN ('moneyline','total','spread')")).fetchall()]
        if not pend and not recent:
            return

        feeds = {}
        for b in pend + recent:
            sk = _odds_sport_key(b.get("sport"), b.get("league"))
            if sk and sk not in feeds:
                feeds[sk] = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_sport_scores, sk)

        # ── 1. auto-grade pending bets whose games went final ──────────
        for b in pend:
            sk = _odds_sport_key(b.get("sport"), b.get("league"))
            if not sk or not feeds.get(sk):
                continue
            status, final = _score_result(b, feeds[sk], sk)
            if not status:
                continue
            if AUTO_GRADE:
                await _apply_auto_grade(b, status, final)
                log.info(f"auto-graded {b['message_id']} -> {status} ({final})")
                await asyncio.sleep(2)   # pace Google Sheets writes on busy slates
            else:
                # shadow mode: report without touching anything
                await post_monitor(
                    "Auto-Grade (shadow mode)",
                    f"Would grade **{status.upper()}** — "
                    f"{(b.get('description') or '?')[:70]}\nFinal: {final}",
                    level="info")
                log.info(f"SHADOW auto-grade {b['message_id']} -> {status} ({final})")

        # ── 2. misgrade detection on recently settled bets ─────────────
        for b in recent:
            sk = _odds_sport_key(b.get("sport"), b.get("league"))
            verdict, final = (None, None)
            if sk and feeds.get(sk):
                verdict, final = _score_result(b, feeds[sk], sk)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE bets SET score_checked=1 WHERE message_id=?",
                    (b["message_id"],))
                await db.commit()
            if verdict and verdict != b.get("status"):
                await post_monitor(
                    "Possible Misgrade",
                    f"**{(b.get('description') or '?')[:70]}** was graded "
                    f"**{b.get('status')}** but the final ({final}) says "
                    f"**{verdict}**. Re-react on the bet card if it needs fixing.",
                    level="warn")
                log.warning(f"misgrade flag {b['message_id']}: "
                            f"stored {b.get('status')} vs score {verdict}")
    except Exception:
        log.exception("score_watch loop failed")
    finally:
        # adapt cadence to the clock: fast when finals land, slow when idle
        try:
            m = _score_poll_minutes()
            if m != score_watch.minutes:
                score_watch.change_interval(minutes=m)
                log.info(f"score_watch cadence -> every {m}m")
        except Exception:
            pass


@score_watch.before_loop
async def before_score_watch():
    await bot.wait_until_ready()



# ── MLB abbreviation awareness for CLV matching ─────────────────────────
# Bet descriptions sometimes use city/team codes ("LA -1.5", "NYY ML")
# instead of the full nickname the API returns ("Los Angeles Dodgers").
# Map each code to the nickname fragment _team_frag() would produce, so
# abbreviation-only descriptions can still match a scheduled game.
_MLB_ABBR_TO_FRAG = {
    "ARI":"diamondbacks","AZ":"diamondbacks","ATL":"braves","BAL":"orioles",
    "BOS":"sox","CHC":"cubs","CWS":"sox","CHW":"sox","CIN":"reds","CLE":"guardians",
    "COL":"rockies","DET":"tigers","HOU":"astros","KC":"royals","KCR":"royals",
    "LAA":"angels","ANA":"angels","LAD":"dodgers","LA":"dodgers","MIA":"marlins",
    "MIL":"brewers","MIN":"twins","NYM":"mets","NYY":"yankees","OAK":"athletics",
    "ATH":"athletics","PHI":"phillies","PIT":"pirates","SD":"padres","SDP":"padres",
    "SEA":"mariners","SF":"giants","SFG":"giants","STL":"cardinals","TB":"rays",
    "TBR":"rays","TEX":"rangers","TOR":"jays","WSH":"nationals","WAS":"nationals",
}


def _team_matches(team_name, desc_lower, desc_raw):
    """True if the description references this team by nickname OR by a
    known city/franchise abbreviation (word-boundary, case-sensitive)."""
    frag = _team_frag(team_name)
    if frag and frag in desc_lower:
        return True
    for abbr, af in _MLB_ABBR_TO_FRAG.items():
        if af == frag and re.search(rf"\b{abbr}\b", desc_raw):
            return True
    return False


# ── League verification: catch NBA/WNBA-style misclassifications ───────
# Uses The Odds API /events (quota-free) to check the parsed league against
# real schedules. Strict: only corrects when exactly ONE sibling league has
# a matching game. Ambiguity = leave as parsed.
_SIBLING_KEYS = {
    "basketball": ["basketball_nba", "basketball_wnba", "basketball_ncaab"],
    "football":   ["americanfootball_nfl", "americanfootball_ncaaf"],
    "baseball":   ["baseball_mlb"],
    "hockey":     ["icehockey_nhl"],
}
_KEY_TO_LEAGUE = {
    "basketball_nba": ("Basketball", "NBA"), "basketball_wnba": ("Basketball", "WNBA"),
    "basketball_ncaab": ("Basketball", "NCAAB"),
    "americanfootball_nfl": ("Football", "NFL"), "americanfootball_ncaaf": ("Football", "NCAAF"),
    "baseball_mlb": ("Baseball", "MLB"), "icehockey_nhl": ("Hockey", "NHL"),
}
_events_cache = {}   # sport_key -> (ts, events)


def _get_events(sport_key):
    """Quota-free events list, cached 10 min."""
    import requests as _rq, time as _t
    now = _t.time()
    hit = _events_cache.get(sport_key)
    if hit and now - hit[0] < 600:
        return hit[1]
    try:
        r = _rq.get(f"https://api.the-odds-api.com/v4/sports/{sport_key}/events",
                    params={"apiKey": ODDS_API_KEY}, timeout=10)
        ev = r.json() if r.status_code == 200 else []
    except Exception:
        ev = []
    _events_cache[sport_key] = (now, ev)
    return ev


def _desc_matches_events(desc, events):
    """Match by nickname, full city name, or shared city abbreviation —
    so 'MIN vs LV o158.5' matches a Minnesota Lynx game on the schedule."""
    text = desc or ""
    d = text.lower()
    toks = set(re.findall(r"\b[A-Z]{2,4}\b", text))
    cities = {_CITY_ABBR[t] for t in toks if t in _CITY_ABBR}
    for e in events:
        for name in (e.get("home_team"), e.get("away_team")):
            if not name:
                continue
            if _team_frag(name) in d:
                return True
            city = " ".join(name.split(" ")[:-1]).lower()
            if city and (city in d or city in cities):
                return True
    return False


def verify_league(data):
    """Cross-check parsed sport/league against real schedules; fix if the
    evidence is unambiguous. Returns (data, correction_note_or_None)."""
    if not ODDS_API_KEY:
        return data, None
    try:
        sport = (data.get("sport") or "").lower().strip()
        # scan description AND legs — props often name the team only in a leg
        legs = data.get("legs") or []
        if isinstance(legs, str):
            legs = [legs]
        desc = " ".join([data.get("description") or ""] + [str(l) for l in legs])

        # Cross-sport catch: unfamiliar WNBA/NBA team names (new expansion
        # teams, unusual slip layouts) can get the SPORT itself misparsed
        # (e.g. labeled "Soccer"). If the description unambiguously names a
        # basketball team but the sport isn't basketball, fix both fields.
        if sport != "basketball":
            by_team = _basketball_league_from_desc(desc)
            if by_team:
                old_sport = data.get("sport") or "?"
                old_league = data.get("league") or "?"
                data["sport"], data["league"] = "Basketball", by_team
                note = (f"sport corrected: {old_sport} → Basketball, "
                       f"{old_league} → {by_team} (team name match)")
                log.info(note + f" | {desc[:60]}")
                return data, note

        # Deterministic: basketball team names settle NBA vs WNBA instantly
        if sport == "basketball":
            by_team = _basketball_league_from_desc(desc)
            claimed = (data.get("league") or "").upper().strip()
            if by_team and claimed in ("NBA", "WNBA") and by_team != claimed:
                data["league"] = by_team
                note = f"league corrected: {claimed} → {by_team} (team name match)"
                log.info(note + f" | {desc[:60]}")
                return data, note
            if by_team:
                return data, None   # team evidence agrees — done
            # No team evidence (player props): season window backstop
            season = _basketball_season_guess(datetime.now(timezone.utc).isoformat())
            if season and claimed in ("NBA", "WNBA") and season != claimed:
                data["league"] = season
                note = f"league corrected: {claimed} → {season} (off-season for {claimed})"
                log.info(note + f" | {desc[:60]}")
                return data, note

        siblings = _SIBLING_KEYS.get(sport)
        if not siblings or len(siblings) < 2:
            return data, None      # nothing to confuse
        claimed_key = _odds_sport_key(data.get("sport"), data.get("league"))

        matches = [k for k in siblings if _desc_matches_events(desc, _get_events(k))]
        if not matches:
            return data, None      # no schedule evidence either way
        if claimed_key in matches:
            return data, None      # parse agrees with a real game — good
        if len(matches) == 1:      # strict: exactly one alternative fits
            new_sport, new_league = _KEY_TO_LEAGUE[matches[0]]
            old = data.get("league") or data.get("sport") or "?"
            data["sport"], data["league"] = new_sport, new_league
            note = f"league auto-corrected: {old} → {new_league} (verified against schedule)"
            log.info(note + f" | {desc[:60]}")
            return data, note
        return data, None          # multiple fit — ambiguous, leave it
    except Exception:
        log.exception("verify_league failed")
        return data, None


@tasks.loop(minutes=ODDS_POLL_MINUTES)
async def odds_watch():
    """CLV snapshots + live odds + movement alerts for pending game bets."""
    if not ODDS_API_KEY:
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM bets WHERE status='pending' AND closing_odds IS NULL "
                "AND lower(bet_type) IN ('moneyline','total','spread')")
            bets = [dict(r) for r in await cur.fetchall()]
        if not bets:
            return

        # one fetch per distinct sport
        feeds = {}
        for b in bets:
            sk = _odds_sport_key(b.get("sport"), b.get("league"))
            if sk and sk not in feeds:
                feeds[sk] = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_sport_odds, sk)

        now = datetime.now(timezone.utc)
        for b in bets:
            sk = _odds_sport_key(b.get("sport"), b.get("league"))
            games = feeds.get(sk) or []
            if not games or b.get("odds") is None:
                continue
            price, commence = _match_bet_price(b, games)
            if price is None or not commence:
                continue
            try:
                start = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            except Exception:
                continue
            mins_out = (start - now).total_seconds() / 60
            if mins_out > 48 * 60:
                continue

            entry = _to_american(b["odds"])
            uname, mid = b.get("username", ""), b["message_id"]

            # persist the game start time once — powers the Live section
            if commence and b.get("game_time") != commence:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE bets SET game_time=? WHERE message_id=?",
                                     (commence, mid))
                    await db.commit()
                await sheets_update_odds(uname, mid, game_time=commence)

            if mins_out <= -5:
                # Game already started — a "closing" price now would be a live
                # in-play price, which poisons CLV. Mark missed (closing=0
                # sentinel stops retries; clv stays NULL so the historical
                # backfill can still rescue it with the true close).
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE bets SET closing_odds=0 WHERE message_id=?", (mid,))
                    await db.commit()
                log.info(f"CLV missed (game started) for {mid} — backfill can recover it")
                continue

            if mins_out <= ODDS_POLL_MINUTES:
                # ── closing snapshot + CLV (true pre-game window) ───
                clv_pts = round((_implied(price) - _implied(entry)) * 100, 1)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE bets SET closing_odds=?, clv=? WHERE message_id=?",
                        (price, clv_pts, mid))
                    await db.commit()
                await sheets_update_odds(uname, mid, closing=price, clv=clv_pts)
                log.info(f"CLV captured {mid}: entry {entry} close {price} -> {clv_pts:+.1f} pts")
            else:
                # ── live odds refresh ───────────────────────────────
                if b.get("live_odds") != price:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE bets SET live_odds=?, live_odds_at=? WHERE message_id=?",
                            (price, now.isoformat(), mid))
                        await db.commit()
                    await sheets_update_odds(uname, mid, live=price)

                # ── movement alert (once per bet) ───────────────────
                move = (_implied(price) - _implied(entry)) * 100
                if abs(move) >= CLV_ALERT_PTS and not b.get("odds_alerted"):
                    good = move > 0  # market moved toward the bet -> entry beat market
                    emoji = "📈" if good else "📉"
                    verdict = "line moved TOWARD you (you beat the move)" if good \
                              else "line moved AGAINST you"
                    grp = next((g for g in GROUPS if g.get("name") == b.get("group_name")), None)
                    ch = bot.get_channel(int(grp["stats_channel_id"])) if grp and grp.get("stats_channel_id") else None
                    if ch:
                        try:
                            await ch.send(
                                f"{emoji} **Line move** — {b.get('description','?')[:80]}\n"
                                f"Your odds: {'+' if entry>0 else ''}{entry} → now "
                                f"{'+' if price>0 else ''}{price} ({move:+.1f} pts) — {verdict}")
                        except Exception:
                            pass
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE bets SET odds_alerted=1 WHERE message_id=?", (mid,))
                        await db.commit()
    except Exception:
        log.exception("odds_watch loop failed")


@odds_watch.before_loop
async def before_odds_watch():
    await bot.wait_until_ready()




@bot.tree.command(name="clv", description="CLV system health: tracked pending bets, captures, and average CLV")
async def clv_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        pend = await (await db.execute(
            "SELECT COUNT(*) c, SUM(CASE WHEN live_odds IS NOT NULL THEN 1 ELSE 0 END) t "
            "FROM bets WHERE status='pending' AND closing_odds IS NULL "
            "AND lower(bet_type) IN ('moneyline','total','spread')")).fetchone()
        caps = await (await db.execute(
            "SELECT odds, closing_odds, description FROM bets "
            "WHERE closing_odds IS NOT NULL AND closing_odds != 0 AND odds IS NOT NULL "
            "ORDER BY settled_at DESC")).fetchall()
        missed = (await (await db.execute(
            "SELECT COUNT(*) c FROM bets WHERE closing_odds = 0")).fetchone())["c"]

    def dec(a):
        a = _to_american(a)
        return a / 100 + 1 if a > 0 else 100 / abs(a) + 1

    pcts = []
    for r in caps:
        try:
            pcts.append((dec(r["odds"]) / dec(r["closing_odds"]) - 1) * 100)
        except (ZeroDivisionError, TypeError, ValueError):
            pass

    lines = ["**📊 CLV System Status**\n"]
    lines.append(f"Pending game bets eligible: **{pend['c'] or 0}** "
                 f"(live odds found for {pend['t'] or 0})")
    lines.append(f"Closing lines captured: **{len(pcts)}**  ·  missed (game started): {missed}")
    if pcts:
        avg = sum(pcts) / len(pcts)
        beat = sum(1 for p in pcts if p > 0)
        lines.append(f"Average CLV: **{avg:+.1f}%**  ·  beat close on {beat}/{len(pcts)} "
                     f"({beat/len(pcts)*100:.0f}%)")
        lines.append("\n**Last 5 captures:**")
        for r in caps[:5]:
            try:
                p = (dec(r["odds"]) / dec(r["closing_odds"]) - 1) * 100
                lines.append(f"• {(r['description'] or '?')[:45]} — {p:+.1f}%")
            except (ZeroDivisionError, TypeError, ValueError):
                pass
    else:
        lines.append("\nNo closing lines captured yet — they land in the 30 minutes "
                     "before each game starts.")
    await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)




def _single_book_price(game, market_key, pick,
                       books=("draftkings", "fanduel", "betmgm", "caesars",
                              "pointsbetus", "bovada", "betrivers")):
    """Fallback when 2+ book consensus isn't available: take the first
    matching price from a preferred trusted book, in priority order.
    Costs no extra credits — reuses the snapshot already fetched."""
    by_book = {bk.get("key"): bk for bk in game.get("bookmakers", [])}
    for key in books:
        bk = by_book.get(key)
        if not bk:
            continue
        for mkt in bk.get("markets", []):
            if mkt.get("key") != market_key:
                continue
            for oc in mkt.get("outcomes", []):
                if market_key == "h2h" and oc.get("name") == pick:
                    return _prob_to_american(_implied(oc["price"])), key
                elif market_key in ("totals", "spreads") and \
                        oc.get("name") == pick[0] and oc.get("point") == pick[1]:
                    return _prob_to_american(_implied(oc["price"])), key
    return None, None


@bot.tree.command(name="clvbackfill", description="Fetch historical closing lines for all past game bets missing CLV")
@app_commands.describe(max_credits="Odds API credit cap for this run (default 8000)")
async def clvbackfill(interaction: discord.Interaction, max_credits: int = 8000):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
        return
    if not ODDS_API_KEY:
        await interaction.response.send_message("ODDS_API_KEY not configured.", ephemeral=True)
        return
    await interaction.response.send_message(
        "🕰️ CLV backfill started — runs in the background, posts results to the stats "
        "channel when done. Historical calls are billed 10x, capped at "
        f"{max_credits} credits.", ephemeral=True)

    async def _run():
        import requests as _rq
        credits = {"used": 0}
        reasons = {"no_candidate": 0, "ambiguous": 0, "no_odds_snapshot": 0,
                   "insufficient_books": 0, "implausible_clv": 0, "no_events_for_day": 0}
        single_book_used = {"count": 0}
        samples = {"no_candidate": [], "ambiguous": []}  # up to 6 real examples each

        def _sample(bucket, b, sk, ev_list):
            if len(samples.get(bucket, [])) < 6:
                available = sorted({e.get("home_team","?")+" vs "+e.get("away_team","?")
                                    for e in ev_list[:20]})
                samples[bucket].append(
                    f"\"{(b.get('description') or '?')[:45]}\" "
                    f"[{sk}, {(b.get('created_at') or '?')[:10]}] "
                    f"— window had: {'; '.join(available[:3]) or 'no games'}")

        def _hget(url, params):
            params["apiKey"] = ODDS_API_KEY
            try:
                r = _rq.get(url, params=params, timeout=20)
                try:
                    credits["used"] += int(float(r.headers.get("x-requests-last", 0)))
                except (ValueError, TypeError):
                    pass
                if r.status_code != 200:
                    return None, r.status_code
                return r.json(), 200
            except Exception:
                return None, None

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            bets = [dict(r) for r in await (await db.execute(
                "SELECT * FROM bets WHERE status IN ('won','lost','push') "
                "AND clv IS NULL AND odds IS NOT NULL "
                "AND lower(bet_type) IN ('moneyline','total','spread')")).fetchall()]

        by_day = {}
        for b in bets:
            sk = _odds_sport_key(b.get("sport"), b.get("league"))
            d = (b.get("created_at") or "")[:10]
            if sk and d:
                by_day.setdefault((sk, d), []).append(b)

        # only request the ONE market a bet actually needs — cuts quota ~3x
        # vs always pulling h2h+totals+spreads together
        MARKET_FOR = {"moneyline": "h2h", "total": "totals", "spread": "spreads"}

        done = failed = 0
        loop = asyncio.get_event_loop()
        for (sk, day), day_bets in sorted(by_day.items()):
            if credits["used"] >= max_credits:
                break
            # created_at is stored in UTC, but games are scheduled in US time —
            # a bet placed at 9:30pm ET is already past midnight UTC. Querying
            # only that exact calendar day misses any game that crossed the
            # UTC boundary either direction, which is most MLB night games.
            # Pull a 3-day UTC window and merge, so the actual game date is
            # covered regardless of which side of midnight it landed on.
            try:
                day_dt = datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                failed += len(day_bets)
                reasons["no_events_for_day"] += len(day_bets)
                continue
            ev_list, seen_ids = [], set()
            for offset in (0, -1, 1):
                d = (day_dt + timedelta(days=offset)).strftime("%Y-%m-%d")
                resp, _ = await loop.run_in_executor(None, _hget,
                    f"https://api.the-odds-api.com/v4/historical/sports/{sk}/events",
                    {"date": f"{d}T12:00:00Z"})
                for e in ((resp or {}).get("data", []) if isinstance(resp, dict) else (resp or [])):
                    eid = e.get("id")
                    if eid and eid not in seen_ids:
                        seen_ids.add(eid)
                        ev_list.append(e)
            if not ev_list:
                failed += len(day_bets)
                reasons["no_events_for_day"] += len(day_bets)
                continue

            odds_cache = {}
            for b in day_bets:
                if credits["used"] >= max_credits:
                    break
                desc = (b.get("description") or "").lower()
                btype = (b.get("bet_type") or "").lower()
                desc_raw = b.get("description") or ""
                cands = [e for e in ev_list
                         if _team_matches(e.get("home_team"), desc, desc_raw)
                         or _team_matches(e.get("away_team"), desc, desc_raw)]
                if len(cands) == 0:
                    failed += 1; reasons["no_candidate"] += 1
                    _sample("no_candidate", b, sk, ev_list)
                    continue
                if len(cands) == 1:
                    ev = cands[0]
                else:
                    # Multiple matches, almost always the SAME two teams
                    # playing on consecutive days within the widened 3-day
                    # window (a back-to-back series). Disambiguate by
                    # picking whichever game started closest to when the
                    # bet was actually placed -- but only if one candidate
                    # is clearly closer than the rest (6h+ margin), so a
                    # genuine coin-flip still gets skipped rather than guessed.
                    ev = None
                    try:
                        created = datetime.fromisoformat(
                            (b.get("created_at") or "").replace("Z", "+00:00"))
                    except ValueError:
                        created = None
                    if created:
                        def _gap(e):
                            try:
                                ct = datetime.fromisoformat(
                                    e["commence_time"].replace("Z", "+00:00"))
                                return abs((ct - created).total_seconds())
                            except Exception:
                                return float("inf")
                        ranked = sorted(cands, key=_gap)
                        if _gap(ranked[1]) - _gap(ranked[0]) >= 6 * 3600:
                            ev = ranked[0]
                    if ev is None:
                        failed += 1; reasons["ambiguous"] += 1
                        _sample("ambiguous", b, sk, cands)
                        continue
                eid = ev["id"]
                mkt = MARKET_FOR.get(btype, "h2h")
                cache_key = (eid, mkt)
                if cache_key not in odds_cache:
                    resp, status = await loop.run_in_executor(None, _hget,
                        f"https://api.the-odds-api.com/v4/historical/sports/{sk}/events/{eid}/odds",
                        {"date": ev["commence_time"], "regions": "us,us2",
                         "markets": mkt, "oddsFormat": "american"})
                    odds_cache[cache_key] = resp
                    await asyncio.sleep(0.4)
                snap = odds_cache[cache_key]
                data = snap.get("data") if isinstance(snap, dict) else None
                if not data or not data.get("bookmakers"):
                    failed += 1; reasons["no_odds_snapshot"] += 1; continue

                hf_match = _team_matches(ev.get("home_team"), desc, desc_raw)
                af_match = _team_matches(ev.get("away_team"), desc, desc_raw)
                hf = _team_frag(ev.get("home_team"))
                af = _team_frag(ev.get("away_team"))
                both = hf_match and af_match
                price = None
                pick = None
                if btype == "moneyline" and not both:
                    pick = ("h2h", ev["home_team"] if hf_match else ev["away_team"])
                elif btype == "total":
                    m = _TOTAL_RE.search(desc)
                    if m:
                        pick = ("totals", (m.group(1).capitalize(), float(m.group(2))))
                elif btype == "spread" and not both:
                    m = _SPREAD_RE.search(b.get("description") or "")
                    if m:
                        pick = ("spreads", (ev["home_team"] if hf_match else ev["away_team"],
                                            float(m.group(1))))
                if pick is None:
                    failed += 1; reasons["insufficient_books"] += 1; continue
                price = _consensus_price(data, pick[0], pick[1])
                book_used = None
                if price is None:
                    # fallback: no 2-book consensus at this snapshot — take a
                    # single trusted book's line instead of losing the bet
                    price, book_used = _single_book_price(data, pick[0], pick[1])
                if price is None:
                    failed += 1; reasons["insufficient_books"] += 1; continue
                if book_used:
                    single_book_used["count"] += 1

                entry = _to_american(b["odds"])
                clv_pts = round((_implied(price) - _implied(entry)) * 100, 1)
                if abs(clv_pts) > 20:      # implausible -> likely bad match, skip
                    failed += 1; reasons["implausible_clv"] += 1; continue
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE bets SET closing_odds=?, clv=? WHERE message_id=?",
                        (price, clv_pts, b["message_id"]))
                    await db.commit()
                done += 1

        breakdown = "  ·  ".join(f"{v} {k.replace('_',' ')}" for k, v in reasons.items() if v)
        sb_note = (f"  ({single_book_used['count']} used a single-book fallback line)"
                  if single_book_used["count"] else "")
        sample_txt = ""
        if samples["no_candidate"]:
            sample_txt += "\n**Sample 'no candidate' misses:**\n" + "\n".join(
                f"• {s}" for s in samples["no_candidate"])
        summary = (f"**🕰️ CLV Backfill Complete**\n"
                   f"✅ {done} closing lines captured{sb_note}  ·  ⏭️ {failed} skipped\n"
                   f"💳 ~{credits['used']} credits used\n"
                   + (f"📋 Skip reasons: {breakdown}\n" if breakdown else "")
                   + sample_txt
                   + "\n\nRun `/resync` to push CLV to the sheet, then check the dashboard.")
        for grp in GROUPS:
            try:
                ch = bot.get_channel(int(grp.get("stats_channel_id", 0)))
                if ch:
                    await ch.send(summary)
                    break
            except Exception:
                pass

    bot.loop.create_task(_run())


@bot.tree.command(name="fixleagues", description="Re-check every basketball bet's NBA/WNBA label and fix mismatches")
async def fixleagues(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # scan every bet, not just ones already labeled basketball -- a bet
        # mislabeled to a DIFFERENT sport entirely (e.g. "Soccer") needs to
        # be found and corrected too, which the old sport-restricted query
        # could never see in the first place
        cur = await db.execute(
            "SELECT message_id, description, sport, league, created_at FROM bets")
        rows = [dict(r) for r in await cur.fetchall()]

        changes = []
        for b in rows:
            claimed_sport = (b.get("sport") or "").lower().strip()
            claimed = (b.get("league") or "").upper().strip()
            correct = _basketball_league_from_desc(b.get("description"))
            wrong_sport = claimed_sport != "basketball" and bool(correct)
            if not correct and claimed_sport == "basketball":
                correct = _basketball_season_guess(b.get("created_at"))
            if correct and (claimed != correct or wrong_sport):
                await db.execute(
                    "UPDATE bets SET league=?, sport='Basketball' WHERE message_id=?",
                    (correct, b["message_id"]))
                tag = f"[{claimed_sport or '?'}→Basketball] " if wrong_sport else ""
                changes.append(f"{tag}{claimed or '?'} → {correct}: {(b.get('description') or '')[:55]}")
        await db.commit()

    if changes:
        msg = f"**🏀 Fixed {len(changes)} league label(s):**\n" + "\n".join(
            f"• {c}" for c in changes[:20])
        if len(changes) > 20:
            msg += f"\n  ...and {len(changes)-20} more"
        msg += "\n\nRun `/resync` to push corrections to the sheet."
    else:
        msg = "✅ All basketball bets already labeled correctly."
    await interaction.followup.send(msg[:1900], ephemeral=True)


@bot.tree.command(name="resync", description="Recalculate all profits and rewrite every user's sheet from the DB")
async def resync(interaction: discord.Interaction):
    """Full resync: recalculates profits/cumulative P&L in DB and rewrites the entire sheet."""
    # Only server admins or users with manage_guild permission can run this
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server permission to run this.", ephemeral=True)
        return

    await interaction.response.send_message(
        "🔄 Full resync started — runs in the background and posts results to the stats channel when done.",
        ephemeral=True,
    )

    async def _do_resync():
        results = []
        try:
            # ── Step 1: Recalculate profits in DB ──────────────────────
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT * FROM bets WHERE status != 'pending' ORDER BY created_at ASC"
                )
                all_bets = [dict(r) for r in await cur.fetchall()]
                fixed = 0
                for bet in all_bets:
                    correct = calc_profit(
                        bet["odds"], bet["stake"], bet["status"], bet.get("potential_payout")
                    )
                    if correct is not None and (
                        bet["profit"] is None or abs(float(bet["profit"] or 0) - float(correct)) > 0.01
                    ):
                        await db.execute(
                            "UPDATE bets SET profit = ? WHERE message_id = ?",
                            (round(correct, 2), bet["message_id"]),
                        )
                        fixed += 1
                await db.commit()
                results.append(f"✅ Recalculated {fixed} profit value(s) in DB")

            # ── Step 2: Reload all bets with corrected values ──────────
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM bets ORDER BY created_at ASC")
                all_bets = [dict(r) for r in await cur.fetchall()]

            by_user = {}
            for bet in all_bets:
                uname = bet["username"] or "Unknown"
                by_user.setdefault(uname, []).append(bet)

            # ── Step 3: Rewrite each user's sheet tab ──────────────────
            if not SHEETS_AVAILABLE or not SPREADSHEET_ID:
                results.append("⚠️ Google Sheets not configured — skipping sheet rewrite")
            else:
                loop = asyncio.get_event_loop()

                def _rewrite_sheets():
                    import time
                    client = _get_sheets_client()
                    if not client:
                        results.append("❌ Could not connect to Google Sheets")
                        return
                    ss = client.open_by_key(SPREADSHEET_ID)

                    for username, bets in by_user.items():
                        try:
                            ws = _get_or_create_data_sheet(ss, username)
                            ws.clear()
                            ws.append_row(SHEET_HEADERS, value_input_option="RAW")
                            time.sleep(1)

                            # Calculate streak and cumulative per user
                            settled = sorted(
                                [b for b in bets if b["status"] != "pending"],
                                key=lambda b: b.get("created_at") or ""
                            )
                            streaks = {}
                            streak_count = 0
                            streak_type = None
                            for bet in settled:
                                if bet["status"] in ("won", "lost"):
                                    if bet["status"] == streak_type:
                                        streak_count += 1
                                    else:
                                        streak_type = bet["status"]
                                        streak_count = 1
                                    letter = "W" if streak_type == "won" else "L"
                                    streaks[str(bet["message_id"])] = f"{letter}{streak_count}"

                            cum = 0.0
                            rows_to_write = []
                            for bet in sorted(bets, key=lambda b: b.get("created_at") or ""):
                                profit = None
                                if bet["status"] != "pending" and bet["profit"] is not None:
                                    profit = float(bet["profit"])
                                    cum = round(cum + profit, 2)
                                roi = round((profit / float(bet["stake"])) * 100, 1) if (
                                    profit is not None and bet.get("stake") and float(bet["stake"]) > 0
                                ) else None
                                to_win = None
                                if bet.get("potential_payout") and bet.get("stake"):
                                    to_win = round(float(bet["potential_payout"]) - float(bet["stake"]), 2)
                                rows_to_write.append([
                                    bet.get("created_at", ""),
                                    bet.get("settled_at", "") or "",
                                    bet.get("username", ""),
                                    bet.get("group_name", ""),
                                    bet.get("description", ""),
                                    bet.get("sport", ""),
                                    bet.get("league", ""),
                                    bet.get("sportsbook", ""),
                                    bet.get("bet_type", ""),
                                    bet.get("prop_category", ""),
                                    bet.get("legs", ""),
                                    bet.get("odds", ""),
                                    bet.get("stake", ""),
                                    to_win or "",
                                    bet.get("status", ""),
                                    profit if profit is not None else "",
                                    roi if roi is not None else "",
                                    cum if bet["status"] != "pending" else "",
                                    streaks.get(str(bet.get("message_id", "")), ""),
                                    str(bet.get("message_id", "")),
                                    bet.get("live_odds") or "",
                                    bet.get("closing_odds") or "",
                                    bet.get("clv") if bet.get("clv") is not None else "",
                                    bet.get("game_time") or "",
                                ])

                            # Write in chunks of 50 to avoid quota errors
                            for i in range(0, len(rows_to_write), 50):
                                ws.append_rows(rows_to_write[i:i+50], value_input_option="USER_ENTERED")
                                time.sleep(2)
                            # Reapply color formatting (ws.clear() wiped it)
                            try:
                                _apply_data_sheet_formatting(ss, ws)
                            except Exception:
                                log.exception(f"resync: formatting reapply failed for {username}")
                            results.append(f"✅ {username}: {len(rows_to_write)} bets rewritten")
                        except Exception as e:
                            results.append(f"❌ {username}: {e}")
                            log.exception(f"resync failed for {username}")

                await loop.run_in_executor(None, lambda: _sheets_call(_rewrite_sheets))

        except Exception as e:
            results.append(f"❌ Resync error: {e}")
            log.exception("resync command failed")

        summary = "**🔄 Full Resync Complete**\n" + "\n".join(results)
        if len(summary) > 1900:
            summary = summary[:1900] + "\n..."
        for grp in GROUPS:
            try:
                ch = bot.get_channel(int(grp.get("stats_channel_id", 0)))
                if ch:
                    await ch.send(summary)
                    break
            except Exception:
                pass

    bot.loop.create_task(_do_resync())


@bot.tree.command(name="audit", description="Check all settled bets for profit calculation errors and fix them")
async def audit(interaction: discord.Interaction):
    group = group_for_any_channel(interaction.channel.id)
    if GROUPS and not group:
        await interaction.response.send_message(
            "Run this inside one of the bet-tracking channels for a group.", ephemeral=True
        )
        return
    group_name = group["name"] if group else None

    await interaction.response.defer(ephemeral=True)

    query = "SELECT * FROM bets WHERE status != 'pending'"
    params = []
    if group_name:
        query += " AND group_name = ?"
        params.append(group_name)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        bets = [dict(zip(cols, row)) for row in rows]

        fixed = []
        null_profit = []

        for bet in bets:
            correct = calc_profit(
                bet["odds"], bet["stake"], bet["status"], bet.get("potential_payout")
            )
            stored = bet["profit"]

            if correct is None:
                null_profit.append({
                    "desc":    bet["description"] or "unknown",
                    "guild":   bet.get("guild_id") or "@me",
                    "channel": bet["channel_id"],
                    "message": bet["message_id"],
                })
                continue

            if stored is None or abs(float(stored) - float(correct)) > 0.01:
                # Mismatch — fix it
                await db.execute(
                    "UPDATE bets SET profit = ? WHERE message_id = ?",
                    (correct, bet["message_id"]),
                )
                fixed.append(
                    f"{bet['description'] or 'unknown'} — was {stored}, now {'+' if correct >= 0 else ''}{correct:.2f}"
                )

        if fixed:
            await db.commit()

    # Refresh stats so cards update immediately
    if fixed:
        if group_name:
            await refresh_live_stats(group_name)
        else:
            for g in GROUPS:
                if g.get("stats_channel_id"):
                    await refresh_live_stats(g["name"])

    lines = []
    if fixed:
        lines.append(f"**Fixed {len(fixed)} bet(s):**")
        lines.extend(f"• {f}" for f in fixed)
    if null_profit:
        lines.append(f"\n**{len(null_profit)} bet(s) still can't be calculated** — use `/fixbet` on each one:")
        lines.extend(
            f"• {d['desc']} — https://discord.com/channels/{d['guild']}/{d['channel']}/{d['message']}"
            for d in null_profit
        )
    if not fixed and not null_profit:
        lines.append("✅ All settled bets look correct — nothing to fix.")

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + f"\n... {len(fixed)} total fixed, {len(null_profit)} still need manual input."

    await interaction.followup.send(msg, ephemeral=True)

    # Post a visible audit result + updated stats to the stats channel
    if fixed and group and group.get("stats_channel_id"):
        stats_channel = bot.get_channel(int(group["stats_channel_id"]))
        if stats_channel:
            audit_embed = discord.Embed(
                title="🔧 Audit Complete",
                description=f"Recalculated profits on {len(fixed)} bet(s). Stats updated below.",
                color=0x3498DB,
            )
            if null_profit:
                audit_embed.add_field(
                    name="⚠️ Still uncalculable",
                    value=f"{len(null_profit)} bet(s) need manual input — run `/fixbet` on each one.",
                    inline=False,
                )
            await stats_channel.send(embed=audit_embed)


@bot.tree.command(name="grade", description="Manually grade a bet if the reaction isn't working")
@app_commands.describe(
    message_link="Right-click the bet card in the output channel → Copy Message Link",
    result="Won, lost, or push",
)
@app_commands.choices(result=[
    app_commands.Choice(name="Won",  value="won"),
    app_commands.Choice(name="Lost", value="lost"),
    app_commands.Choice(name="Push", value="push"),
])
async def grade(interaction: discord.Interaction, message_link: str, result: app_commands.Choice[str]):
    parts = message_link.strip().rstrip("/").split("/")
    if not parts[-1].isdigit():
        await interaction.response.send_message(
            "That doesn't look like a valid message link. Right-click the bet card → Copy Message Link.",
            ephemeral=True,
        )
        return

    message_id = parts[-1]

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM bets WHERE message_id = ?", (message_id,))
        row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(
                "Couldn't find that bet in the database. It may have been posted before a bug fix — delete the card and re-post the screenshot.",
                ephemeral=True,
            )
            return
        cols = [d[0] for d in cur.description]
        bet = dict(zip(cols, row))

        if bet["status"] != "pending":
            await interaction.response.send_message(
                f"That bet is already graded as **{bet['status']}**. Use `/deletebet` first if you need to change it.",
                ephemeral=True,
            )
            return

        is_admin = interaction.user.guild_permissions.manage_messages if interaction.guild else False
        if str(interaction.user.id) != bet["user_id"] and not is_admin and not ALLOW_ANYONE_TO_SETTLE:
            await interaction.response.send_message("You can only grade your own bets.", ephemeral=True)
            return

        status = result.value
        profit = calc_profit(bet["odds"], bet["stake"], status, bet.get("potential_payout"))

        await db.execute(
            "UPDATE bets SET status = ?, profit = ?, settled_at = ? WHERE message_id = ?",
            (status, profit, datetime.now(timezone.utc).isoformat(), message_id),
        )
        await db.commit()

    # Update the Discord embed
    group_name = bet.get("group_name") or "default"
    async with aiosqlite.connect(DB_PATH) as db:
        cur2 = await db.execute(
            """SELECT SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),
                      COALESCE(SUM(profit), 0)
               FROM bets WHERE user_id=? AND group_name=? AND status != 'pending'""",
            (bet["user_id"], group_name),
        )
        rec = await cur2.fetchone()
    record_str = f"{rec[0]}-{rec[1]}  ·  {'+' if rec[2] >= 0 else ''}{rec[2]:.2f} all time" if rec else None

    data = {
        "sportsbook": bet["sportsbook"],
        "sport":      bet.get("sport"),
        "league":     bet.get("league"),
        "bet_type":   bet["bet_type"],
        "description": bet["description"],
        "legs":        json.loads(bet["legs"] or "[]"),
        "odds":        bet["odds"],
        "stake":       bet["stake"],
        "potential_payout": bet["potential_payout"],
    }
    embed = build_embed(data, bet["username"], status=status, profit=profit, record=record_str)

    try:
        ch = bot.get_channel(int(bet["channel_id"]))
        if ch:
            msg = await ch.fetch_message(int(message_id))
            await msg.edit(embed=embed)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass

    await interaction.response.send_message(
        f"Graded **{bet.get('description') or 'bet'}** as **{status}** — {'+' if profit and profit >= 0 else ''}{profit:.2f}" if profit is not None else f"Graded as **{status}**",
        ephemeral=True,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        cur_cum = await db.execute(
            """SELECT COALESCE(SUM(profit), 0) FROM bets
               WHERE user_id=? AND group_name=? AND status!='pending'
               AND settled_at <= (SELECT settled_at FROM bets WHERE message_id=?)""",
            (bet["user_id"], group_name, message_id),
        )
        (cumulative_pnl,) = await cur_cum.fetchone()

    await sheets_update_bet(bet["username"], message_id, status, profit,
                            cumulative_pnl=round(cumulative_pnl, 2))
    await refresh_live_stats(group_name)


@bot.tree.command(name="deletebet", description="Delete a bet by pasting a link to the bet card message")
@app_commands.describe(message_link="Right-click the bet card in the output channel → Copy Message Link")
async def deletebet(interaction: discord.Interaction, message_link: str):
    # Extract message ID from the link (format: .../channels/guild_id/channel_id/message_id)
    parts = message_link.strip().rstrip("/").split("/")
    if len(parts) < 3 or not parts[-1].isdigit():
        await interaction.response.send_message(
            "That doesn't look like a valid message link. Right-click the bet card → Copy Message Link.",
            ephemeral=True,
        )
        return

    message_id = parts[-1]
    channel_id = parts[-2] if len(parts) >= 2 else None

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM bets WHERE message_id = ?", (message_id,))
        row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(
                "Couldn't find that bet in the database. Make sure you're linking the bot's bet card, not the original screenshot.",
                ephemeral=True,
            )
            return
        cols = [d[0] for d in cur.description]
        bet = dict(zip(cols, row))

    # Only the original poster or an admin can delete
    is_admin = interaction.user.guild_permissions.manage_messages if interaction.guild else False
    if str(interaction.user.id) != bet["user_id"] and not is_admin:
        await interaction.response.send_message(
            "You can only delete your own bets.", ephemeral=True
        )
        return

    group_name = bet.get("group_name") or "default"

    # Try to delete the actual Discord message too
    try:
        ch = bot.get_channel(int(bet["channel_id"]))
        if ch:
            msg = await ch.fetch_message(int(message_id))
            await msg.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass  # message already gone or no permission — still remove from DB

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bets WHERE message_id = ?", (message_id,))
        await db.commit()

    # Remove from Google Sheet too
    await sheets_delete_bet(bet.get("username", ""), message_id)

    await interaction.response.send_message(
        f"Bet deleted — **{bet.get('description') or 'unknown'}**", ephemeral=True
    )
    await refresh_live_stats(group_name)


@bot.tree.command(name="status", description="Show bot health and activity report right now")
async def status(interaction: discord.Interaction):
    await update_status_message()
    if MONITOR_CHANNEL_ID:
        ch = bot.get_channel(int(MONITOR_CHANNEL_ID))
        await interaction.response.send_message(
            f"Status updated in {ch.mention if ch else '#bot-status'}.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "MONITOR_CHANNEL_ID not set in Railway — add it to enable the status channel.", ephemeral=True
        )


@bot.tree.command(name="summary", description="Post a group recap for a time period")
@app_commands.describe(period="Time period to summarize")
@app_commands.choices(period=[
    app_commands.Choice(name="Today",     value="today"),
    app_commands.Choice(name="This Week", value="week"),
    app_commands.Choice(name="This Month",value="month"),
    app_commands.Choice(name="All Time",  value="alltime"),
])
async def summary(interaction: discord.Interaction, period: app_commands.Choice[str]):
    group = group_for_any_channel(interaction.channel.id)
    if GROUPS and not group:
        await interaction.response.send_message(
            "Run this inside one of the bet-tracking channels for a group.", ephemeral=True
        )
        return
    group_name = group["name"] if group else None

    today_cut, week_cut, month_cut, *_ = period_cutoffs()
    cutoff_map = {
        "today":   today_cut,
        "week":    week_cut,
        "month":   month_cut,
        "alltime": "1970-01-01",
    }
    label_map = {
        "today":   "Today",
        "week":    "This Week",
        "month":   "This Month",
        "alltime": "All Time",
    }
    cutoff    = cutoff_map[period.value]
    label     = label_map[period.value]

    async with aiosqlite.connect(DB_PATH) as db:
        query = """
            SELECT username,
                   COALESCE(SUM(profit), 0) as net,
                   COALESCE(SUM(CASE WHEN status='won'  THEN 1 ELSE 0 END), 0) as wins,
                   COALESCE(SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END), 0) as losses,
                   COALESCE(SUM(CASE WHEN status='push' THEN 1 ELSE 0 END), 0) as pushes
            FROM bets WHERE status != 'pending' AND settled_at >= ?
        """
        params = [cutoff]
        if group_name:
            query += " AND group_name = ?"
            params.append(group_name)
        query += " GROUP BY user_id ORDER BY net DESC"
        cur = await db.execute(query, params)
        rows = await cur.fetchall()

        # Streak per user — look up by user_id, respecting group filter
        streaks = {}
        for username, *_ in rows:
            uid_query = "SELECT user_id FROM bets WHERE username = ? LIMIT 1"
            uid_params = [username]
            if group_name:
                uid_query = "SELECT user_id FROM bets WHERE username = ? AND group_name = ? LIMIT 1"
                uid_params = [username, group_name]
            cur2 = await db.execute(uid_query, uid_params)
            uid_row = await cur2.fetchone()
            if uid_row:
                streak_query = """SELECT status FROM bets WHERE user_id = ?
                                  AND status IN ('won','lost')
                                  ORDER BY settled_at DESC LIMIT 20"""
                streak_params = [uid_row[0]]
                if group_name:
                    streak_query = """SELECT status FROM bets WHERE user_id = ?
                                      AND group_name = ?
                                      AND status IN ('won','lost')
                                      ORDER BY settled_at DESC LIMIT 20"""
                    streak_params = [uid_row[0], group_name]
                cur3 = await db.execute(streak_query, streak_params)
                s_rows = await cur3.fetchall()
                if s_rows:
                    first = s_rows[0][0]
                    count = 0
                    for (s,) in s_rows:
                        if s == first:
                            count += 1
                        else:
                            break
                    streaks[username] = f"{'🔥W' if first=='won' else '🥶L'}{count}"

    if not rows:
        await interaction.response.send_message(f"No settled bets for {label}.", ephemeral=True)
        return

    group_net = sum(r[1] for r in rows)
    lines = []
    for i, (username, net, wins, losses, pushes) in enumerate(rows, 1):
        decided = wins + losses
        win_pct = f"{wins/decided*100:.0f}%" if decided > 0 else "—"
        net_str = f"{'+' if net >= 0 else ''}{net:.2f}"
        streak  = streaks.get(username, "")
        medal   = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} **{username}** — {wins}-{losses}-{pushes} — {net_str} — {win_pct}  {streak}")

    group_net_str = f"{'+' if group_net >= 0 else ''}{group_net:.2f}"
    embed = discord.Embed(
        title=f"📊 {label} Recap — {group_name or 'Group'}",
        description="\n".join(lines),
        color=discord.Color.green() if group_net >= 0 else discord.Color.red(),
    )
    embed.set_footer(text=f"Group net: {group_net_str}  ·  {datetime.now(CENTRAL).strftime('%b %d  %I:%M %p CT')}")
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
