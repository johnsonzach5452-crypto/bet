"""
backfill.py — run this ONCE to populate your Google Sheet with all existing bets from the database.

Usage (in Railway console):
    python3 backfill.py

Requires GOOGLE_CREDENTIALS_JSON, SPREADSHEET_ID, and DB_PATH to be set as environment variables
(they're already set in Railway since the bot uses them).
"""

import os
import json
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from itertools import takewhile

CENTRAL = ZoneInfo("America/Chicago")

DB_PATH           = os.environ.get("DB_PATH", "bets.db")
SPREADSHEET_ID    = os.environ.get("SPREADSHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

if not SPREADSHEET_ID or not GOOGLE_CREDS_JSON:
    print("ERROR: SPREADSHEET_ID and GOOGLE_CREDENTIALS_JSON must be set as environment variables.")
    exit(1)

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    print("ERROR: gspread not installed. Run: pip install gspread google-auth")
    exit(1)

SHEET_HEADERS = [
    "Date Posted (CT)",    # A
    "Date Settled (CT)",   # B
    "Username",            # C
    "Group",               # D
    "Description",         # E
    "Sport",               # F
    "League",              # G
    "Sportsbook",          # H
    "Bet Type",            # I
    "Prop Category",       # J
    "Legs (#)",            # K
    "Odds",                # L
    "Stake ($)",           # M
    "To Win ($)",          # N
    "Status",              # O
    "Profit ($)",          # P
    "ROI %",               # Q
    "Cumulative P&L ($)",  # R
    "Streak",              # S
    "Message ID",          # T
]


def _c(r, g, b):
    return {"red": r/255, "green": g/255, "blue": b/255}


def fmt_dt(iso_str, tz=CENTRAL):
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%Y-%m-%d %I:%M %p")
    except Exception:
        return iso_str or ""


def calc_profit(odds, stake, status, potential_payout=None):
    if stake is None:
        return None
    if status == "lost":
        return round(-stake, 2)
    if status == "push":
        return 0.0
    if status == "won":
        if odds is not None:
            if odds > 0:
                return round(stake * (odds / 100), 2)
            else:
                return round(stake * (100 / abs(odds)), 2)
        if potential_payout is not None:
            return round(potential_payout - stake, 2)
    return None


import time

CHECKPOINT_FILE = "backfill_checkpoint.json"


def load_checkpoint():
    try:
        with open(CHECKPOINT_FILE) as f:
            return set(json.load(f).get("completed", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_checkpoint(completed_users):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump({"completed": list(completed_users)}, f)


def sheets_retry(fn, retries=3, delay=5):
    """Call fn(), retrying up to `retries` times on failure with exponential backoff."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = delay * (2 ** attempt)
            print(f"  ⚠️  Attempt {attempt + 1} failed: {e}. Retrying in {wait}s...")
            time.sleep(wait)
    import calendar as cal_lib
    from zoneinfo import ZoneInfo
    CENTRAL = ZoneInfo("America/Chicago")
    now = datetime.now(CENTRAL)
    year, month = now.year, now.month
    month_name = now.strftime("%B %Y")
    tab_title = f"{username} Calendar"

    def _c(r, g, b):
        return {"red": r/255, "green": g/255, "blue": b/255}

    try:
        cal_ws = ss.worksheet(tab_title)
        cal_ws.clear()
    except gspread.WorksheetNotFound:
        cal_ws = ss.add_worksheet(title=tab_title, rows=12, cols=7)

    q = f"'{data_sheet_title}'"
    p_col = f"{q}!P:P"
    a_col = f"{q}!A:A"   # Date Posted (CT) — group by placement date
    o_col = f"{q}!O:O"

    rows = [[f"📅  {username.upper()} — {month_name}", "", "", "", "", "", ""]]
    rows.append(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])

    first_weekday, num_days = cal_lib.monthrange(year, month)
    start_offset = (first_weekday + 1) % 7
    week = [""] * 7
    day = 1
    calendar_rows = []
    col = start_offset
    while day <= num_days:
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

    sid = cal_ws.id
    reqs = []
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 7},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _c(26, 26, 46),
            "textFormat": {"foregroundColor": _c(255,255,255), "bold": True, "fontSize": 13},
            "horizontalAlignment": "CENTER",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
    }})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2,
                  "startColumnIndex": 0, "endColumnIndex": 7},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _c(52, 73, 94),
            "textFormat": {"foregroundColor": _c(255,255,255), "bold": True},
            "horizontalAlignment": "CENTER",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
    }})
    cal_range = {"sheetId": sid, "startRowIndex": 2,
                 "endRowIndex": 2 + len(calendar_rows), "startColumnIndex": 0, "endColumnIndex": 7}
    reqs.append({"repeatCell": {
        "range": cal_range,
        "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "CURRENCY", "pattern": '"$"#,##0.00;"-$"#,##0.00'},
            "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
            "textFormat": {"fontSize": 11, "bold": True},
        }},
        "fields": "userEnteredFormat(numberFormat,horizontalAlignment,verticalAlignment,textFormat)"
    }})
    for op, bg, fg in [("NUMBER_GREATER", _c(198,239,206), _c(0,97,0)),
                        ("NUMBER_LESS",    _c(255,199,206), _c(156,0,6))]:
        reqs.append({"addConditionalFormatRule": {"rule": {
            "ranges": [cal_range],
            "booleanRule": {
                "condition": {"type": op, "values": [{"userEnteredValue": "0"}]},
                "format": {"backgroundColor": bg, "textFormat": {"foregroundColor": fg, "bold": True}}
            }
        }, "index": 0}})
    for i in range(7):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i+1},
            "properties": {"pixelSize": 130}, "fields": "pixelSize"
        }})
    for i in range(2, 2 + len(calendar_rows)):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": i, "endIndex": i+1},
            "properties": {"pixelSize": 60}, "fields": "pixelSize"
        }})
    ss.batch_update({"requests": reqs})


def apply_formatting(ss, ws):
    sid = ws.id
    reqs = []
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 20},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _c(26, 26, 46),
            "textFormat": {"foregroundColor": _c(255, 255, 255), "bold": True, "fontSize": 10},
            "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
    }})
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount"
    }})
    widths = [140,140,100,80,300,100,70,110,90,130,55,70,80,80,75,85,70,120,70,160]
    for i, w in enumerate(widths):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i+1},
            "properties": {"pixelSize": w}, "fields": "pixelSize"
        }})
    for start, end in [(12,14),(15,16),(17,18)]:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2000,
                      "startColumnIndex": start, "endColumnIndex": end},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": '"$"#,##0.00'}}},
            "fields": "userEnteredFormat.numberFormat"
        }})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2000,
                  "startColumnIndex": 16, "endColumnIndex": 17},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": '#,##0.0"%"'}}},
        "fields": "userEnteredFormat.numberFormat"
    }})
    row_range = {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2000,
                 "startColumnIndex": 0, "endColumnIndex": 20}
    for status, bg, fg in [
        ("won",     _c(198,239,206), _c(0,97,0)),
        ("lost",    _c(255,199,206), _c(156,0,6)),
        ("push",    _c(220,220,220), _c(80,80,80)),
        ("pending", _c(255,242,204), _c(100,80,0)),
    ]:
        reqs.append({"addConditionalFormatRule": {"rule": {
            "ranges": [row_range],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue": f'=$O2="{status}"'}]},
                "format": {"backgroundColor": bg, "textFormat": {"foregroundColor": fg}}
            }
        }, "index": 0}})
    for col_start, col_end in [(15,16),(17,18)]:
        pr = {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2000,
              "startColumnIndex": col_start, "endColumnIndex": col_end}
        for op, color in [("NUMBER_GREATER", _c(0,97,0)), ("NUMBER_LESS", _c(156,0,6))]:
            reqs.append({"addConditionalFormatRule": {"rule": {
                "ranges": [pr],
                "booleanRule": {
                    "condition": {"type": op, "values": [{"userEnteredValue": "0"}]},
                    "format": {"textFormat": {"foregroundColor": color, "bold": True}}
                }
            }, "index": 0}})
    ss.batch_update({"requests": reqs})


def _q(username):
    return f"'{username}'"


def _create_calendar_tab(ss, username, data_sheet_title):
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
    q = f"'{data_sheet_title}'"
    p_col = f"{q}!P:P"
    a_col = f"{q}!A:A"
    o_col = f"{q}!O:O"
    rows = [[f"📅  {username.upper()} — {month_name}", "", "", "", "", "", ""]]
    rows.append(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])
    first_weekday, num_days = cal_lib.monthrange(year, month)
    start_offset = (first_weekday + 1) % 7
    week = [""] * 7
    day = 1
    calendar_rows = []
    col = start_offset
    while day <= num_days:
        formula = (f'=IFERROR(SUMIFS({p_col},{a_col},">="&DATE({year},{month},{day}),'
                   f'{a_col},"<"&DATE({year},{month},{day})+1,{o_col},"<>pending"),"")')
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
    sid = cal_ws.id
    reqs = []
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 7},
        "cell": {"userEnteredFormat": {"backgroundColor": _c(26,26,46), "textFormat": {"foregroundColor": _c(255,255,255), "bold": True, "fontSize": 13}, "horizontalAlignment": "CENTER"}},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
    }})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": 7},
        "cell": {"userEnteredFormat": {"backgroundColor": _c(52,73,94), "textFormat": {"foregroundColor": _c(255,255,255), "bold": True}, "horizontalAlignment": "CENTER"}},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
    }})
    cal_range = {"sheetId": sid, "startRowIndex": 2, "endRowIndex": 2+len(calendar_rows), "startColumnIndex": 0, "endColumnIndex": 7}
    reqs.append({"repeatCell": {
        "range": cal_range,
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": '"$"#,##0.00;"-$"#,##0.00'}, "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "textFormat": {"fontSize": 11, "bold": True}}},
        "fields": "userEnteredFormat(numberFormat,horizontalAlignment,verticalAlignment,textFormat)"
    }})
    for op, bg, fg in [("NUMBER_GREATER", _c(198,239,206), _c(0,97,0)), ("NUMBER_LESS", _c(255,199,206), _c(156,0,6))]:
        reqs.append({"addConditionalFormatRule": {"rule": {"ranges": [cal_range], "booleanRule": {"condition": {"type": op, "values": [{"userEnteredValue": "0"}]}, "format": {"backgroundColor": bg, "textFormat": {"foregroundColor": fg, "bold": True}}}}, "index": 0}})
    for i in range(7):
        reqs.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i+1}, "properties": {"pixelSize": 130}, "fields": "pixelSize"}})
    for i in range(2, 2+len(calendar_rows)):
        reqs.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": i, "endIndex": i+1}, "properties": {"pixelSize": 60}, "fields": "pixelSize"}})
    ss.batch_update({"requests": reqs})


def _setup_dashboard(ss, username):
    # Delete duplicate dashboard tabs from previous backfill runs
    existing = ss.worksheets()
    dash_title = f"{username} Dashboard"
    dash_tabs = [w for w in existing if w.title == dash_title]
    for extra in dash_tabs[1:]:
        try:
            ss.del_worksheet(extra)
        except Exception:
            pass

    if dash_tabs:
        dash = dash_tabs[0]
        dash.clear()
    else:
        dash = ss.add_worksheet(title=dash_title, rows=80, cols=4)

    q = _q(username)
    o = f"{q}!O:O"
    p = f"{q}!P:P"
    m = f"{q}!M:M"
    f_ = f"{q}!F:F"
    i_ = f"{q}!I:I"
    h_ = f"{q}!H:H"

    def w(col, val): return f'COUNTIFS({o},"<>pending",{col},"{val}",{o},"won")'
    def l(col, val): return f'COUNTIFS({o},"<>pending",{col},"{val}",{o},"lost")'
    def tot(col, val): return f'COUNTIFS({o},"<>pending",{col},"{val}")'
    def net(col, val): return f'SUMIFS({p},{o},"<>pending",{col},"{val}")'
    def wp(col, val): return f'IFERROR({w(col,val)}/({w(col,val)}+{l(col,val)})*100,0)'

    # ── Summary block (rows 1-19) ──────────────────────────────────────────
    # Column A = label, Column B = value
    # Won=B5, Lost=B6 → Win% formula uses B5/B6
    # Net=B9, Staked=B10 → ROI uses B9/B10
    summary = [
        [f"📊  {username.upper()}  —  BET TRACKER", ""],  # 1
        ["", ""],                                           # 2
        ["SUMMARY", "VALUE"],                              # 3
        ["Total Bets",   f"=COUNTA({q}!A2:A)"],           # 4
        ["Won",          f'=COUNTIF({o},"won")'],          # 5  ← B5
        ["Lost",         f'=COUNTIF({o},"lost")'],         # 6  ← B6
        ["Push",         f'=COUNTIF({o},"push")'],         # 7
        ["Win %",        "=IFERROR(B5/(B5+B6)*100,0)"],   # 8  ← FIXED
        ["Net Profit",   f"=SUM({p})"],                    # 9  ← B9
        ["Total Staked", f'=SUMIF({o},"<>pending",{m})'],  # 10 ← B10
        ["ROI %",        "=IFERROR(B9/B10*100,0)"],        # 11 ← FIXED
        ["Pending",      f'=COUNTIF({o},"pending")'],      # 12
        ["Streak",       f'=IFERROR(INDEX({q}!S:S,MATCH(2,1/({q}!S:S<>""),1)),"—")'],  # 13
        ["", ""],                                           # 14
        ["ROLLING WINDOWS", "NET  |  ROI %"],              # 15
        ["Last 30 Days", f'=IFERROR("$"&TEXT(SUMIFS({p},{q}!B:B,">="&TODAY()-30,{o},"<>pending"),"#,##0.00")&"  |  "&TEXT(IFERROR(SUMIFS({p},{q}!B:B,">="&TODAY()-30,{o},"<>pending")/SUMIFS({m},{q}!B:B,">="&TODAY()-30,{o},"<>pending")*100,0),"0.0")&"%","—")'],  # 16
        ["Last 60 Days", f'=IFERROR("$"&TEXT(SUMIFS({p},{q}!B:B,">="&TODAY()-60,{o},"<>pending"),"#,##0.00")&"  |  "&TEXT(IFERROR(SUMIFS({p},{q}!B:B,">="&TODAY()-60,{o},"<>pending")/SUMIFS({m},{q}!B:B,">="&TODAY()-60,{o},"<>pending")*100,0),"0.0")&"%","—")'],  # 17
        ["Last 90 Days", f'=IFERROR("$"&TEXT(SUMIFS({p},{q}!B:B,">="&TODAY()-90,{o},"<>pending"),"#,##0.00")&"  |  "&TEXT(IFERROR(SUMIFS({p},{q}!B:B,">="&TODAY()-90,{o},"<>pending")/SUMIFS({m},{q}!B:B,">="&TODAY()-90,{o},"<>pending")*100,0),"0.0")&"%","—")'],  # 18
        ["", ""],                                           # 19
    ]

    # ── By Sport (rows 20+) — single value column with all stats combined ──
    sports = ["Football","Basketball","Baseball","Hockey","Soccer","MMA","Tennis","Golf","Other"]
    sport_block = [["BY SPORT", ""], ["Sport", "Bets  |  W-L  |  Net ($)  |  Win%"]]
    for s in sports:
        formula = (f'=IF({tot(f_,s)}=0,"—",'
                   f'"{s}  "&{tot(f_,s)}&"  |  "&{w(f_,s)}&"-"&{l(f_,s)}&'
                   f'"  |  $"&TEXT({net(f_,s)},"#,##0.00")&'
                   f'"  |  "&TEXT({wp(f_,s)},"0.0")&"%")')
        sport_block.append([s, f"={formula[1:]}"])  # strip leading =
    sport_block.append(["", ""])

    bet_types = ["moneyline","spread","total","parlay","prop","future","other"]
    type_block = [["BY BET TYPE", ""], ["Type", "Bets  |  W-L  |  Net ($)  |  Win%"]]
    for bt in bet_types:
        formula = (f'=IF({tot(i_,bt)}=0,"—",'
                   f'"{bt.title()}  "&{tot(i_,bt)}&"  |  "&{w(i_,bt)}&"-"&{l(i_,bt)}&'
                   f'"  |  $"&TEXT({net(i_,bt)},"#,##0.00")&'
                   f'"  |  "&TEXT({wp(i_,bt)},"0.0")&"%")')
        type_block.append([bt.title(), f"={formula[1:]}"])
    type_block.append(["", ""])

    books = ["DraftKings","FanDuel","BetMGM","Caesars","Novig","Kalshi","ESPN BET","PrizePicks","Underdog"]
    book_block = [["BY SPORTSBOOK", ""], ["Book", "Bets  |  W-L  |  Net ($)  |  Win%"]]
    for b in books:
        formula = (f'=IF({tot(h_,b)}=0,"—",'
                   f'"{b}  "&{tot(h_,b)}&"  |  "&{w(h_,b)}&"-"&{l(h_,b)}&'
                   f'"  |  $"&TEXT({net(h_,b)},"#,##0.00")&'
                   f'"  |  "&TEXT({wp(h_,b)},"0.0")&"%")')
        book_block.append([b, f"={formula[1:]}"])
    book_block.append(["", ""])

    row = 1
    for block in [summary, sport_block, type_block, book_block]:
        end_row = row + len(block) - 1
        dash.update(range_name=f"A{row}:B{end_row}", values=block, value_input_option="USER_ENTERED")
        row = end_row + 1

    _format_dashboard_visual(ss, dash, len(summary), len(sport_block), len(bet_types), len(books))
    print(f"  Dashboard created for {username}")


def _format_dashboard_visual(ss, dash, summary_len, sport_block_len, num_bet_types, num_books):
    """Apply full visual formatting to the 2-column mobile dashboard."""
    sid = dash.id
    reqs = []

    # Row positions (0-indexed)
    title_row   = 0
    summary_hdr = 2
    rolling_hdr = 14
    sport_hdr   = summary_len           # e.g. 19
    sport_col_h = sport_hdr + 1
    sport_data  = sport_col_h + 1
    sport_end   = sport_data + 9
    type_hdr    = sport_hdr + sport_block_len
    type_col_h  = type_hdr + 1
    type_data   = type_col_h + 1
    type_end    = type_data + num_bet_types
    book_hdr    = type_hdr + (num_bet_types + 3)
    book_col_h  = book_hdr + 1
    book_data   = book_col_h + 1
    book_end    = book_data + num_books

    def rng(r0, r1, c0=0, c1=2):
        return {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
                "startColumnIndex": c0, "endColumnIndex": c1}

    # ── Title ──────────────────────────────────────────────────────────────
    reqs.append({"repeatCell": {
        "range": rng(0, 1),
        "cell": {"userEnteredFormat": {
            "backgroundColor": _c(15, 20, 40),
            "textFormat": {"foregroundColor": _c(255,255,255), "bold": True, "fontSize": 15},
            "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
    }})
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 48}, "fields": "pixelSize"
    }})

    # ── Section headers ────────────────────────────────────────────────────
    for row_idx in [summary_hdr, rolling_hdr, sport_hdr, type_hdr, book_hdr]:
        reqs.append({"repeatCell": {
            "range": rng(row_idx, row_idx+1),
            "cell": {"userEnteredFormat": {
                "backgroundColor": _c(30, 50, 80),
                "textFormat": {"foregroundColor": _c(255,255,255), "bold": True, "fontSize": 11},
                "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }})
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": row_idx, "endIndex": row_idx+1},
            "properties": {"pixelSize": 30}, "fields": "pixelSize"
        }})

    # ── Column header rows ─────────────────────────────────────────────────
    for row_idx in [sport_col_h, type_col_h, book_col_h]:
        reqs.append({"repeatCell": {
            "range": rng(row_idx, row_idx+1),
            "cell": {"userEnteredFormat": {
                "backgroundColor": _c(180, 195, 215),
                "textFormat": {"bold": True, "fontSize": 10},
                "horizontalAlignment": "LEFT",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
        }})

    # ── Summary value cells: bold + number formats ─────────────────────────
    reqs.append({"repeatCell": {
        "range": rng(3, 14, 1, 2),  # B4:B14
        "cell": {"userEnteredFormat": {
            "textFormat": {"bold": True, "fontSize": 12},
            "horizontalAlignment": "RIGHT",
        }},
        "fields": "userEnteredFormat(textFormat,horizontalAlignment)"
    }})

    # Net Profit (B9) and Total Staked (B10) — currency
    for row_idx in [8, 9]:  # 0-indexed rows 8 and 9 = sheet rows 9 and 10
        reqs.append({"repeatCell": {
            "range": rng(row_idx, row_idx+1, 1, 2),
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": '"$"#,##0.00'}}},
            "fields": "userEnteredFormat.numberFormat"
        }})

    # Win % (B8) and ROI % (B11) — percentage
    for row_idx in [7, 10]:
        reqs.append({"repeatCell": {
            "range": rng(row_idx, row_idx+1, 1, 2),
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": '#,##0.0"%"'}}},
            "fields": "userEnteredFormat.numberFormat"
        }})

    # ── Conditional: green/red on summary values ───────────────────────────
    for row_idx in [7, 8, 10]:  # Win%, Net Profit, ROI%
        vrange = rng(row_idx, row_idx+1, 1, 2)
        reqs.append({"addConditionalFormatRule": {"rule": {
            "ranges": [vrange],
            "booleanRule": {
                "condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
                "format": {"textFormat": {"foregroundColor": _c(0,130,0), "bold": True}}
            }
        }, "index": 0}})
        reqs.append({"addConditionalFormatRule": {"rule": {
            "ranges": [vrange],
            "booleanRule": {
                "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]},
                "format": {"textFormat": {"foregroundColor": _c(180,0,0), "bold": True}}
            }
        }, "index": 0}})

    # ── Alternating rows for data sections ────────────────────────────────
    for section_start, section_end in [(sport_data, sport_end), (type_data, type_end), (book_data, book_end)]:
        for i, row_idx in enumerate(range(section_start, section_end)):
            bg = _c(240, 245, 252) if i % 2 == 0 else _c(255, 255, 255)
            reqs.append({"repeatCell": {
                "range": rng(row_idx, row_idx+1),
                "cell": {"userEnteredFormat": {"backgroundColor": bg}},
                "fields": "userEnteredFormat.backgroundColor"
            }})

    # ── Column widths — optimized for mobile ──────────────────────────────
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 150}, "fields": "pixelSize"
    }})
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
        "properties": {"pixelSize": 280}, "fields": "pixelSize"
    }})

    # ── Borders ────────────────────────────────────────────────────────────
    for s_start, s_end in [(sport_col_h, sport_end), (type_col_h, type_end), (book_col_h, book_end)]:
        reqs.append({"updateBorders": {
            "range": rng(s_start, s_end),
            "top":    {"style": "SOLID_MEDIUM", "color": _c(30,50,80)},
            "bottom": {"style": "SOLID_MEDIUM", "color": _c(30,50,80)},
            "innerHorizontal": {"style": "SOLID", "color": _c(200,210,220)},
        }})

    # ── Freeze title ──────────────────────────────────────────────────────
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount"
    }})

    ss.batch_update({"requests": reqs})


import time
import time

CHECKPOINT_FILE = "backfill_checkpoint.json"


def load_checkpoint():
    try:
        with open(CHECKPOINT_FILE) as f:
            return set(json.load(f).get("completed", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_checkpoint(completed_users):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump({"completed": list(completed_users)}, f)


def sheets_retry(fn, retries=3, delay=5):
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = delay * (2 ** attempt)
            print(f"  ⚠️  Attempt {attempt + 1} failed: {e}. Retrying in {wait}s...")
            time.sleep(wait)


def main():
    import sys
    dashboards_only = "--dashboards-only" in sys.argv

    completed = load_checkpoint()
    if completed and not dashboards_only:
        print(f"Resuming — skipping already-completed: {', '.join(completed)}")

    print("Connecting to Google Sheets...")
    raw = GOOGLE_CREDS_JSON.strip().replace("\n", "").replace("\r", "")
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    try:
        creds_dict = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}")
        print("Make sure you copied the entire .json file as one block — no line breaks.")
        exit(1)

    required = {"type", "project_id", "private_key", "client_email"}
    missing = required - set(creds_dict.keys())
    if missing:
        print(f"ERROR: credentials JSON is missing fields: {missing}")
        exit(1)

    creds = Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    try:
        ss = gc.open_by_key(SPREADSHEET_ID)
        print(f"✅ Connected to: '{ss.title}'")
    except Exception as e:
        print(f"ERROR: Could not open spreadsheet {SPREADSHEET_ID}: {e}")
        print("Make sure the sheet is shared with the service account email.")
        exit(1)

    print(f"Reading bets from {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM bets ORDER BY username, COALESCE(settled_at, created_at) ASC")
    all_bets = [dict(row) for row in cur.fetchall()]
    conn.close()
    print(f"Found {len(all_bets)} bets total.")

    by_user = {}
    for bet in all_bets:
        by_user.setdefault(bet["username"] or "Unknown", []).append(bet)

    # Dashboards-only mode — just create/refresh dashboard + calendar tabs
    if dashboards_only:
        print("Dashboards-only mode — creating dashboard and calendar tabs...")
        for username in by_user:
            print(f"\n  {username}...")
            try:
                sheets_retry(lambda u=username: _setup_dashboard(ss, u))
            except Exception as e:
                print(f"  Dashboard warning: {e}")
            try:
                ws = sheets_retry(lambda u=username: ss.worksheet(u))
                sheets_retry(lambda u=username, t=ws.title: _create_calendar_tab(ss, u, t))
                print(f"  Calendar created for {username}")
            except Exception as e:
                print(f"  Calendar warning: {e}")
        print("\n✅ Dashboards and calendars created.")
        return

    for username, bets in by_user.items():
        if username in completed:
            print(f"Skipping {username} (checkpoint)")
            continue

        print(f"\nWriting {len(bets)} bets for {username}...")
        try:
            ws = sheets_retry(lambda u=username: ss.worksheet(u))
            sheets_retry(ws.clear)
            print(f"  Cleared existing sheet")
        except gspread.WorksheetNotFound:
            ws = sheets_retry(lambda: ss.add_worksheet(title=username, rows=2000, cols=len(SHEET_HEADERS)))
            print(f"  Created new sheet")

        sheets_retry(lambda: ws.append_row(SHEET_HEADERS, value_input_option="RAW"))

        rows = []
        cumulative = 0.0
        settled_statuses = []

        for bet in bets:
            status = bet.get("status") or "pending"
            odds   = bet.get("odds")
            stake  = bet.get("stake")
            payout = bet.get("potential_payout")
            profit = bet.get("profit")
            if profit is None:
                profit = calc_profit(odds, stake, status, payout)
            roi = ""
            if profit is not None and stake:
                try:
                    roi = round(float(profit) / float(stake) * 100, 1)
                except (ValueError, TypeError):
                    pass
            cumulative_val = ""
            if status in ("won", "lost", "push") and profit is not None:
                cumulative += profit
                cumulative_val = round(cumulative, 2)
            streak_str = ""
            if status in ("won", "lost"):
                settled_statuses.append(status)
                last = settled_statuses[-1]
                count = sum(1 for _ in takewhile(lambda s: s == last, reversed(settled_statuses)))
                streak_str = f"{'W' if last == 'won' else 'L'}{count}"
            legs_raw = bet.get("legs") or "[]"
            try:
                legs_count = len(json.loads(legs_raw))
            except Exception:
                legs_count = 0
            rows.append([
                fmt_dt(bet.get("created_at")),
                fmt_dt(bet.get("settled_at")) if status != "pending" else "",
                username,
                bet.get("group_name") or "",
                bet.get("description") or "",
                (bet.get("sport") or "").title(),
                (bet.get("league") or "").upper(),
                bet.get("sportsbook") or "",
                (bet.get("bet_type") or "").title(),
                "",
                legs_count if legs_count > 1 else "",
                odds or "",
                stake or "",
                payout or "",
                status,
                round(profit, 2) if profit is not None else "",
                roi,
                cumulative_val,
                streak_str,
                str(bet.get("message_id") or ""),
            ])

        if rows:
            for i in range(0, len(rows), 100):
                chunk = rows[i:i+100]
                sheets_retry(lambda c=chunk: ws.append_rows(c, value_input_option="USER_ENTERED"))
                print(f"  Rows {i+1}–{min(i+100,len(rows))}/{len(rows)}")
                time.sleep(1)

        print(f"  Applying formatting...")
        try:
            sheets_retry(lambda: apply_formatting(ss, ws))
        except Exception as e:
            print(f"  Formatting warning (non-fatal): {e}")

        print(f"  Creating dashboard...")
        try:
            sheets_retry(lambda u=username: _setup_dashboard(ss, u))
        except Exception as e:
            print(f"  Dashboard warning (non-fatal): {e}")

        print(f"  Creating calendar tab...")
        try:
            sheets_retry(lambda u=username, t=ws.title: _create_calendar_tab(ss, u, t))
        except Exception as e:
            print(f"  Calendar warning (non-fatal): {e}")

        completed.add(username)
        save_checkpoint(completed)
        print(f"  ✅ {username} done — checkpoint saved")

    # Clean up checkpoint on full success
    try:
        import os
        os.remove(CHECKPOINT_FILE)
    except FileNotFoundError:
        pass

    print(f"\n✅ Backfill complete — {len(by_user)} user sheet(s) written.")
    print("Open your Google Sheet to verify, then delete backfill.py from Railway.")


if __name__ == "__main__":
    main()
