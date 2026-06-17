# Bet Tracker Discord Bot

Post a screenshot of a bet slip in Discord → the bot reads it (sportsbook, bet, odds,
stake, payout), posts a clean summary, and lets you mark it won/lost/push with a
reaction. Everything gets logged so `/stats` and `/leaderboard` work across your group.

## 1. Create the Discord bot

1. Go to https://discord.com/developers/applications → **New Application**.
2. Go to the **Bot** tab → **Reset Token** → copy the token (this is `DISCORD_BOT_TOKEN`).
3. On the same Bot tab, scroll to **Privileged Gateway Intents** and turn ON
   **Message Content Intent**. This is required — the bot can't read attachments
   without it.
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `View Channels`, `Send Messages`, `Embed Links`,
     `Add Reactions`, `Read Message History`
   - Open the generated URL and invite the bot to your server.

## 2. Get an Anthropic API key

This bot calls the Claude API directly to read the screenshots (separate from
claude.ai — it needs its own key and is billed per use).

1. Go to https://console.anthropic.com → API Keys → create one. This is
   `ANTHROPIC_API_KEY`.
2. Image requests are cheap (a small fraction of a cent to a couple cents each
   depending on image size), but it is metered usage — check
   https://docs.claude.com/en/docs/about-claude/pricing for current rates.

## 3. Deploy to Railway

1. Push this folder to a GitHub repo.
2. https://railway.app → **New Project** → **Deploy from GitHub repo** → pick the repo.
3. In the service **Variables** tab, add:
   - `DISCORD_BOT_TOKEN`
   - `ANTHROPIC_API_KEY`
   - `DB_PATH` = `/data/bets.db`
   - `BET_CHANNEL_ID` (optional — right-click a channel in Discord with Developer
     Mode on to copy its ID; leave unset to watch every channel the bot can see)
   - `ALLOW_ANYONE_TO_SETTLE` = `true` if you want any friend to be able to settle
     any bet, not just the person who posted it (default: only the poster can settle)
4. Add a **Volume**: service → Settings → Volumes → mount path `/data`. Without this,
   the database resets every time Railway redeploys the service.
5. Settings → set the **Start Command** to `python bot.py` if it isn't auto-detected.
6. Deploy. Check the Deployments → Logs tab for `Logged in as ...` to confirm it's
   live.

This will run continuously on Railway's Hobby plan (~$5/month, which is also the
plan's included usage credit, so a small bot like this should normally stay within it).

## 4. Using it

- Drop a screenshot of a bet slip into the channel. The bot replies with a parsed
  summary and three reactions: ✅ won, ❌ lost, ↩️ push/void.
- React to settle it. The embed updates with the result and profit/loss.
- If the parse comes out wrong or blank, use `/logbet` to enter it manually.
- `/stats [user]` — record, win %, net profit for you or someone else.
- `/leaderboard` — net profit ranking across everyone who's logged a bet.
- `/pending` — your bets still waiting to be settled.

## Notes / things worth knowing

- Profit is computed from American odds + stake using standard payout math — it
  assumes the odds/stake the model read off the screenshot are correct, so it's
  worth a glance before trusting it for anything that matters.
- Multi-leg parlays get parsed into a `legs` list and displayed under the summary,
  but settlement is still all-or-nothing per the single ✅/❌/↩️ reaction (no partial
  leg tracking).
- By default only the original poster can react to settle a bet, to stop someone
  marking a friend's bet as a win. Flip `ALLOW_ANYONE_TO_SETTLE=true` if your group
  doesn't care about that.
