import os
import json
import base64
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BET_CHANNEL_ID = os.environ.get("BET_CHANNEL_ID")  # optional: restrict to one channel
DB_PATH = os.environ.get("DB_PATH", "bets.db")
# If True, anyone can settle a bet by reacting, not just the original poster
ALLOW_ANYONE_TO_SETTLE = os.environ.get("ALLOW_ANYONE_TO_SETTLE", "false").lower() == "true"

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
  "bet_type": one of "moneyline", "spread", "total", "parlay", "prop", "future", "other",
  "description": string,            // short human-readable summary of the bet, e.g. "Packers -3.5 vs Bears"
  "legs": [string],                 // one entry per leg; a straight (non-parlay) bet still has exactly 1 entry
  "odds": number or null,           // American odds for the bet as a whole, e.g. -110 or 150
  "stake": number or null,          // dollars risked, no $ sign
  "potential_payout": number or null // total payout if it wins (stake + profit), no $ sign
}

If a field isn't visible or determinable from the image, use null for it. Do not guess wildly -
only fill in values you can actually read from the screenshot."""


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


def fmt_odds(odds):
    if odds is None:
        return None
    try:
        odds = int(odds)
        return f"{odds:+d}"
    except (ValueError, TypeError):
        return str(odds)


def build_embed(data, author_name, status="pending", profit=None):
    color = {
        "pending": discord.Color.gold(),
        "won": discord.Color.green(),
        "lost": discord.Color.red(),
        "push": discord.Color.light_grey(),
    }[status]

    title = f"{data.get('sportsbook') or 'Bet'} — {(data.get('bet_type') or 'bet').title()}"
    embed = discord.Embed(title=title, description=data.get("description") or "—", color=color)

    legs = data.get("legs") or []
    if len(legs) > 1:
        embed.add_field(name="Legs", value="\n".join(f"• {l}" for l in legs), inline=False)

    if data.get("odds") is not None:
        embed.add_field(name="Odds", value=fmt_odds(data["odds"]))
    if data.get("stake") is not None:
        embed.add_field(name="Stake", value=f"${data['stake']:.2f}")
    if data.get("potential_payout") is not None:
        embed.add_field(name="To Win", value=f"${data['potential_payout']:.2f}")

    status_label = {
        "pending": "⏳ Pending — react ✅ won, ❌ lost, ↩️ push",
        "won": "✅ Won",
        "lost": "❌ Lost",
        "push": "↩️ Push",
    }[status]
    if profit is not None and status != "pending":
        status_label += f"  ({'+' if profit >= 0 else ''}{profit:.2f})"
    embed.add_field(name="Status", value=status_label, inline=False)

    embed.set_footer(text=f"Logged by {author_name}")
    return embed


@bot.event
async def on_ready():
    await init_db()
    try:
        await bot.tree.sync()
    except Exception:
        log.exception("slash command sync failed")
    log.info(f"Logged in as {bot.user} (id={bot.user.id})")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if BET_CHANNEL_ID and str(message.channel.id) != str(BET_CHANNEL_ID):
        await bot.process_commands(message)
        return

    image_attachments = [
        a for a in message.attachments if a.content_type and a.content_type.startswith("image/")
    ]
    if not image_attachments:
        await bot.process_commands(message)
        return

    attachment = image_attachments[0]
    img_bytes = await attachment.read()
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    media_type = attachment.content_type

    thinking = await message.reply("Reading bet slip...")

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
    except Exception as e:
        log.exception("parse failed")
        await thinking.edit(
            content=f"Couldn't read that slip clearly. Try a clearer screenshot, or log it manually with `/logbet`."
        )
        return

    embed = build_embed(data, message.author.display_name, status="pending")
    await thinking.delete()
    bet_msg = await message.reply(embed=embed)
    for emoji in REACTIONS:
        await bet_msg.add_reaction(emoji)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bets (message_id, channel_id, guild_id, user_id, username, sportsbook, bet_type,
                description, legs, odds, stake, potential_payout, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                str(bet_msg.id),
                str(message.channel.id),
                str(message.guild.id) if message.guild else None,
                str(message.author.id),
                message.author.display_name,
                data.get("sportsbook"),
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
        "bet_type": bet["bet_type"],
        "description": bet["description"],
        "legs": json.loads(bet["legs"] or "[]"),
        "odds": bet["odds"],
        "stake": bet["stake"],
        "potential_payout": bet["potential_payout"],
    }
    embed = build_embed(data, bet["username"], status=status, profit=profit)
    await reaction.message.edit(embed=embed)


@bot.tree.command(name="logbet", description="Manually log a bet (use if the screenshot parse fails)")
@app_commands.describe(
    sportsbook="Sportsbook (e.g. DraftKings, FanDuel, Kalshi)",
    description="What's the bet, in plain words",
    odds="American odds, e.g. -110 or 150",
    stake="Dollars risked",
    potential_payout="Total payout if it wins (optional)",
)
async def logbet(
    interaction: discord.Interaction,
    sportsbook: str,
    description: str,
    odds: int,
    stake: float,
    potential_payout: float = None,
):
    data = {
        "sportsbook": sportsbook,
        "bet_type": "manual",
        "description": description,
        "legs": [description],
        "odds": odds,
        "stake": stake,
        "potential_payout": potential_payout,
    }
    embed = build_embed(data, interaction.user.display_name, status="pending")
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    for emoji in REACTIONS:
        await msg.add_reaction(emoji)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bets (message_id, channel_id, guild_id, user_id, username, sportsbook, bet_type,
                description, legs, odds, stake, potential_payout, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                str(msg.id),
                str(interaction.channel.id),
                str(interaction.guild.id) if interaction.guild else None,
                str(interaction.user.id),
                interaction.user.display_name,
                sportsbook,
                "manual",
                description,
                json.dumps([description]),
                odds,
                stake,
                potential_payout,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()


@bot.tree.command(name="stats", description="Show betting stats for a user (or yourself)")
async def stats(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT status, COUNT(*), COALESCE(SUM(profit),0) FROM bets "
            "WHERE user_id = ? AND status != 'pending' GROUP BY status",
            (str(member.id),),
        )
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


@bot.tree.command(name="leaderboard", description="Net profit leaderboard for everyone tracked here")
async def leaderboard(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT username, COALESCE(SUM(profit),0) as total,
                   SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) as losses
            FROM bets WHERE status != 'pending' GROUP BY user_id ORDER BY total DESC
            """
        )
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


@bot.tree.command(name="pending", description="List your pending (unsettled) bets")
async def pending(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT description, message_id, channel_id FROM bets WHERE user_id = ? AND status = 'pending'",
            (str(interaction.user.id),),
        )
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


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
