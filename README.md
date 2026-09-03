# propr-alerts

A Discord bot that announces **your Hyperliquid trades** — ticker, direction,
entry, stop and target — and keeps each alert up to date as the setup fills,
cancels or closes.

**No sizes, ever.** No quantity, no notional, no balance, no PnL figure. Alerts
only retain the configured public risk percentage (the copier's follower side
of the risk remap), and `tests/test_render.py` pushes a book full of sizes
through the whole pipeline and fails if any of them reach the output.

## How it gets your trades

It never touches Hyperliquid or your keys. It polls the copier's read-only API:

```
propr-copy  /api/accounts/snapshot  →  leader block  →  tracker  →  Discord
```

The copier's *leader* is your Hyperliquid account, already normalised into one
order shape by its venue adapter. This bot holds no exchange credentials and
cannot place, cancel or modify anything.

## What gets posted

One embed per idea, edited in place as it progresses, with a short follow-up
line on each transition so an edit is never missed. The title is the direction
and ticker; the body is the levels:

```
LONG BTC
**Entry** 64,250.5 (limit)
**Stop** 63,100
**Target** 66,800
**Risk** 1%

⏳ Working

[Save fees on Hyperliquid](https://app.hyperliquid.xyz/join/QIKO)
[5% off challenges on Propr](https://app.propr.xyz/r/RkWBtYVD)

Follow @qikoCrypto on X • Affiliated with Nefarious.Trading
```

| Event | What you see |
|---|---|
| A new entry rests on the book | the embed above, colour blurple |
| Entry, stop or target moves | the same message is updated + a reply naming every moved order |
| Entry fills | *Filled* + `✅ BTC LONG filled at 64,251` |
| Entry cancelled unfilled | *Cancelled* + `🚫 Cancelled BTC LONG` |
| Position closes | *Closed* + `🏁 BTC LONG closed in profit` |

`SHOW_OUTCOME=false` drops the "in profit" / "at a loss" wording if you would
rather say nothing at all about how it went.

## Setup

1. **Create the bot** at <https://discord.com/developers/applications> → *New
   Application* → *Bot* → *Reset Token*. Copy the token. No privileged intents
   are needed — it only posts.
2. **Configure**: `cp .env.example .env`, then fill in `DISCORD_TOKEN`,
   `OWNER_IDS` (your Discord user ID — enable Developer Mode, right-click
   yourself → Copy User ID) and the copier's `COPIER_USER` / `COPIER_PASSWORD`.
3. **Run**: `docker compose up -d --build`
4. **Invite it** to a server with *Send Messages*, *Embed Links* and
   *Read Message History*, then run `/subscribe #channel` there.

## Commands

Every command is restricted to `OWNER_IDS`. Keep only your Discord user ID in
that setting if nobody else should be able to run them.

| Command | Purpose |
|---|---|
| `/subscribe <channel> [role]` | Set the alert channel and optional mention role |
| `/subscribe_id <guild> <channel>` | Subscribe by ID, from anywhere |
| `/unsubscribe` | Stop alerts here (owner or server admin) |
| `/admin_unsubscribe <guild_id>` | Remove a server by ID |
| `/servers` | List every subscribed server |
| `/status` | Copier reachability, last read, live setups |
| `/preview` | Post a sample alert, only you can see it |
| `/invite` | Invite link with the right permissions |
| `/ping` | Latency |

## Behaviour worth knowing

- **A cold start is silent.** Orders already resting are adopted, not
  announced. `SEED_ALERTS=true` announces them once instead — useful the first
  time you wire a channel up.
- **An unreachable copier alerts nothing.** A failed read is not an empty book,
  so a copier that is down or stopped never produces a wall of "cancelled".
- **State survives restarts** in `data/alerts.db`, so a redeploy mid-trade does
  not re-announce anything.
- **A close does not say *why*.** The snapshot shows working orders, not fills,
  so "stop hit" versus "target hit" cannot be told apart honestly. It reports
  the close and, optionally, the direction. Distinguishing them would need a
  fills endpoint on the copier.
- **Poll interval is 3s** (`POLL_SECONDS`). A trade that opens and closes
  entirely inside one interval can be missed.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest -q
```

`tests/test_hyperliquid_shapes.py` runs on the output of the copier's own
Hyperliquid normaliser, so the fixtures match what the exchange really sends.
