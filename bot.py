import os
import json
import base64
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from anthropic import Anthropic
from dotenv import load_dotenv

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

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

intents = discord.Intents.default()
intents.message_content = True  # required (privileged) - enable in Discord Dev Portal too

bot = commands.Bot(command_prefix="!", intents=intents)

REACTIONS = {"✅": "won", "❌": "lost", "↩️": "push"}

PARSE_PROMPT = """You are extracting structured data from a sports betting slip screenshot.
Respond with ONLY valid JSON (no markdown fences, no extra text) matching exactly this schema:

{
  "sportsbook": string or null,
  "sport": string or null,           // e.g. "Football", "Basketball", "Baseball", "Soccer", "Hockey", "MMA", "Tennis", "Golf"
  "league": string or null,          // e.g. "NFL", "NBA", "MLB", "NHL", "NCAAF", "NCAAB", "EPL", "UFC", "PGA"
  "bet_type": one of "moneyline", "spread", "total", "parlay", "prop", "future", "other",
  "description": string,             // short human-readable summary of the bet, e.g. "Packers -3.5 vs Bears"
  "legs": [string],                  // one entry per leg; a straight (non-parlay) bet still has exactly 1 entry
  "odds": number or null,            // American odds for the bet as a whole, e.g. -110 or 150
  "stake": number or null,           // dollars risked, no $ sign
  "potential_payout": number or null // total payout if it wins (stake + profit), no $ sign
}

If a field isn't visible or determinable from the image, use null for it. Do not guess wildly -
only fill in values you can actually read or confidently infer from the screenshot."""


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
        await db.commit()


def calc_profit(odds, stake, status):
    if stake is None or odds is None:
        return None
    if status == "won":
        if odds > 0:
            return round(stake * (odds / 100), 2)
        else:
            return round(stake * (100 / abs(odds)), 2)
    elif status == "lost":
        return round(-stake, 2)
    else:  # push / void
        return 0.0


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
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    # Convert back to UTC ISO strings for DB comparison (settled_at is stored in UTC)
    return (
        today.astimezone(timezone.utc).isoformat(),
        week_start.astimezone(timezone.utc).isoformat(),
        month_start.astimezone(timezone.utc).isoformat(),
    )


async def get_user_period_stats(db, user_id, group_name, cutoff):
    cur = await db.execute(
        """
        SELECT COALESCE(SUM(profit), 0),
               SUM(CASE WHEN status='won'  THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='push' THEN 1 ELSE 0 END)
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


async def build_user_embed(user_id, username, group_name):
    today_cut, week_cut, month_cut = period_cutoffs()

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

    try:
        alltime_net = float(alltime[3].replace("+", ""))
    except ValueError:
        alltime_net = 0
    color = 0x2ECC71 if alltime_net >= 0 else 0xE74C3C

    # Clean header: name + streak badge
    streak_badge = f"  {streak}" if streak else ""
    title = f"{username}{streak_badge}"

    # Monospace scoreboard table
    def r(label, w, l, p, net, wp):
        return f"{label:<9}{w:>3}{l:>3}{p:>3}  {net:>9}  {wp:>5}"

    table = "```\n"
    table += f"{'':9}{'W':>3}{'L':>3}{'P':>3}  {'NET':>9}  {'WIN%':>5}\n"
    table += "─" * 34 + "\n"
    table += r("Today",    *daily)   + "\n"
    table += r("Week",     *weekly)  + "\n"
    table += r("Month",    *monthly) + "\n"
    table += "─" * 34 + "\n"
    table += r("All Time", *alltime) + "\n"
    table += "```"

    # Footer line
    footer_parts = []
    if pending:
        footer_parts.append(f"⏳ {pending} pending")
    footer_parts.append(datetime.now(CENTRAL).strftime("%b %d  %I:%M %p CT"))

    embed = discord.Embed(title=title, description=table, color=color)
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
    for g in GROUPS:
        if g.get("stats_channel_id"):
            try:
                await refresh_live_stats(g["name"])
            except Exception:
                log.exception(f"failed to refresh live stats for group {g.get('name')}")
    log.info(f"Logged in as {bot.user} (id={bot.user.id}) — {len(GROUPS)} group(s) configured")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if is_managed_output_or_stats_channel(message.channel.id):
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

    embed = build_embed(data, message.author.display_name, status="pending")

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

    await refresh_live_stats(group_name)
    await bot.process_commands(message)


@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    emoji = str(reaction.emoji)
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
            return  # already settled
        if not ALLOW_ANYONE_TO_SETTLE and str(user.id) != bet["user_id"]:
            return  # only the original poster can settle by default

        status = REACTIONS[emoji]
        profit = calc_profit(bet["odds"], bet["stake"], status)

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


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
