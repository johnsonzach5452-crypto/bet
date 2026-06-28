import os
import json
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
                  "startColumnIndex": 0, "endColumnIndex": 20},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _c(26, 26, 46),
            "textFormat": {"foregroundColor": _c(255, 255, 255), "bold": True, "fontSize": 10},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
    }})

    # ── Column widths ───────────────────────────────────────────────────
    widths = [140,140,100,80,300,100,70,110,90,130,55,70,80,80,75,85,70,120,70,160]
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
        # Strip whitespace/newlines that get introduced when pasting into Railway
        raw = GOOGLE_CREDENTIALS_JSON.strip().replace("\n", "").replace("\r", "")
        # Strip BOM if present
        if raw.startswith("\ufeff"):
            raw = raw[1:]
        creds_dict = json.loads(raw)
        # Validate required fields are present
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
        await loop.run_in_executor(None, _write)
    except Exception:
        log.exception("sheets_log_bet failed")
        await post_monitor("Sheets Write Failed", "Could not log a new bet to the spreadsheet. Check Railway logs.", level="error")


async def sheets_update_bet(username, message_id, status, profit, cumulative_pnl=None):
    """Update status, profit, ROI, cumulative P&L, streak, and settled date."""
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
        await loop.run_in_executor(None, _update)
    except Exception:
        log.exception("sheets_update_bet failed")
        await post_monitor("Sheets Update Failed", "Could not update a graded bet in the spreadsheet. Check Railway logs.", level="error")

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
  "sport": string or null,           // e.g. "Football", "Basketball", "Baseball", "Soccer", "Hockey", "MMA", "Tennis", "Golf"
  "league": string or null,          // e.g. "NFL", "NBA", "MLB", "NHL", "NCAAF", "NCAAB", "EPL", "UFC", "PGA"
  "bet_type": one of "moneyline", "spread", "total", "parlay", "prop", "future", "other",
  "prop_category": string or null,   // ONLY for props: e.g. "Points", "Assists", "Rebounds", "Strikeouts", "Passing Yards", "Rushing Yards", "Receiving Yards", "Home Runs", "Saves", "First TD Scorer", "Anytime TD", "Goals"
  "description": string,             // short human-readable summary of the bet
  "legs": [string],                  // one entry per leg; straight bets still have exactly 1 entry
  "odds": number or null,            // American odds, e.g. -110 or +150
  "stake": number or null,           // dollars risked, no $ sign
  "potential_payout": number or null // total payout if it wins (stake + profit), no $ sign
}

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
            if odds > 0:
                return round(stake * (odds / 100), 2)
            else:
                return round(stake * (100 / abs(odds)), 2)
        if potential_payout is not None:
            return round(potential_payout - stake, 2)
    return None


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
        AND settled_at >= ?
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
        ORDER BY settled_at DESC LIMIT 20
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
    """Build a compact calendar showing daily net P&L amounts, mobile-friendly."""
    import calendar as cal_lib
    first_weekday, num_days = cal_lib.monthrange(year, month)
    start_offset = (first_weekday + 1) % 7  # Sunday-first

    header = "Su   Mo   Tu   We   Th   Fr   Sa"
    week = ["    "] * 7
    col = start_offset
    lines = [header]
    won_days = lost_days = 0

    for day in range(1, num_days + 1):
        date_str = f"{year}-{month:02d}-{day:02d}"
        net = daily_pnl.get(date_str)
        if net is None:
            cell = " ·  "
        elif net > 0:
            # Cap display at 4 chars: +999
            val = f"+{min(int(abs(net)), 999)}"
            cell = val.rjust(4)
            won_days += 1
        elif net < 0:
            val = f"-{min(int(abs(net)), 999)}"
            cell = val.rjust(4)
            lost_days += 1
        else:
            cell = " ±0 "
        week[col] = cell
        col += 1
        if col == 7:
            lines.append(" ".join(week))
            week = ["    "] * 7
            col = 0

    if col > 0:
        for i in range(col, 7):
            week[i] = "    "
        lines.append(" ".join(week))

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
        return f"{label:<9}{w:>3}{l:>3}{p:>3}  {net:>9}  {wp:>5}"

    div  = "─" * 34
    div2 = "═" * 34
    hdr  = f"{'':9}{'W':>3}{'L':>3}{'P':>3}  {'NET':>9}  {'WIN%':>5}"

    table = "```\n"
    table += hdr + "\n" + div + "\n"
    table += r("Today",    *daily)   + "\n"
    table += r("Week",     *weekly)  + "\n"
    table += r("Month",    *monthly) + "\n"
    table += div2 + "\n"
    table += r("All Time", *alltime) + "\n"
    table += "```"

    # Calendar block
    cal_str, won_days, lost_days = build_discord_calendar(daily_pnl, year, month)
    month_label = now_ct.strftime("%B %Y")
    month_net = sum(daily_pnl.values())
    month_net_str = f"{'+' if month_net >= 0 else ''}{month_net:.0f}"
    calendar_block = (
        f"```\n"
        f"📅 {month_label}  ·  {won_days}W {lost_days}L  ·  {month_net_str}\n"
        f"{cal_str}\n"
        f"```"
    )

    embed = discord.Embed(title=title, description=table + "\n" + calendar_block, color=color)

    footer_parts = []
    if pending:
        footer_parts.append(f"⏳ {pending} pending")
    footer_parts.append(now_ct.strftime("%b %d  %I:%M %p CT"))
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
                null_profit.append(bet["description"] or "unknown")
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
                    value="Use `/fixbet` on each — run `/audit` for the full list with links",
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


@bot.event
async def on_reaction_add(reaction, user):
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
            log.info(f"Reaction ignored — bet {reaction.message.id} already settled as {bet['status']}")
            return
        if not ALLOW_ANYONE_TO_SETTLE and str(user.id) != bet["user_id"]:
            log.info(f"Reaction ignored — {user.name} is not the bet owner and ALLOW_ANYONE_TO_SETTLE=false")
            return

        status = REACTIONS[emoji]
        profit = calc_profit(bet["odds"], bet["stake"], status, bet.get("potential_payout"))

        await db.execute(
            "UPDATE bets SET status = ?, profit = ?, settled_at = ? WHERE message_id = ?",
            (status, profit, datetime.now(timezone.utc).isoformat(), str(reaction.message.id)),
        )
        await db.commit()

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
