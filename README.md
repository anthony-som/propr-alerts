# propr-alerts

A Discord bot that announces **your Hyperliquid trades** — ticker, direction,
entry, stop and target — and keeps each alert up to date as the setup fills,
cancels or closes.

**No sizes, ever.** No quantity, no notional, no balance, no PnL figure. Beyond
the price levels, the only figures an alert keeps are ratios: the reward-to-risk
of the levels, and how much of a position has been taken off as a share. `tests/test_render.py` pushes a book full of sizes through the
whole pipeline and fails if any of them reach the output.

## How it gets your trades

It never touches Hyperliquid or your keys. It polls the copier's read-only API:

```
propr-copy  /api/accounts/snapshot  →  leader block  →  tracker  →  Discord
```

The copier's *leader* is your Hyperliquid account, already normalised into one
order shape by its venue adapter. This bot holds no exchange credentials and
cannot place, cancel or modify anything.

### MEXC

Set `MEXC_API_KEY` / `MEXC_API_SECRET` (a futures key with **read-only**
permission) and a second feed polls your MEXC futures account directly, since
the copier has no MEXC adapter. Its alerts go to the same channels, titled
`LONG BTC · MEXC`. Limit/market
entries, TP/SL on orders or positions, and trigger (plan) orders are all read.

## What gets posted

One embed per idea, edited in place as it progresses, with a short follow-up
line on each transition so an edit is never missed. Each follow-up replaces the
previous one, so a trade leaves at most one update under its alert rather than
a stack of them. The title is the direction and ticker; the body is the levels:

```
LONG BTC
**Entry** 64,250.5 (limit)
**Stop** 63,100
**Target** 66,800
**RR** 1:2.22

⏳ Working

[Save fees on Hyperliquid](https://app.hyperliquid.xyz/join/QIKO)
[5% off challenges on Propr](https://app.propr.xyz/r/RkWBtYVD)

Follow @qikoCrypto on X • Affiliated with Nefarious.Trading
```

| Event | What you see |
|---|---|
| A new entry rests on the book | the embed above, colour blurple |
| Entry, stop or target moves | the same message is updated + a reply naming every moved order |
| A stop or target is pulled | the level goes to `—` + `⚠️ Stop removed` |
| Entry fills | *Filled* + `✅ BTC LONG filled at 64,251` |
| Part of the position comes off | *Filled · 50% closed* + `✂️ Trimmed 50%` |
| More is added to the position | the same message + `➕ Added · avg entry 63,800` |
| Entry cancelled unfilled | *Cancelled* + `🚫 Cancelled BTC LONG` |
| Position closes | *Closed* + `🏁 BTC LONG closed in profit` |

**RR** is the distance to the target over the distance to the stop, measured
from the fill once there is one. It stays `—` until entry, stop and target are
all known, and goes back to `—` once the stop is at or past the entry.

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
4. **Invite it** to a server with *Send Messages*, *Embed Links*,
   *Attach Files* (for `/announce` images) and *Read Message History*,
   then run `/subscribe #channel` there.

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
| `/announce <message> [image] [heading] [mention] [plain] [preview]` | Post your own message or chart to every subscribed channel |
| `/preview` | Post a sample alert, only you can see it |
| `/invite` | Invite link with the right permissions |
| `/ping` | Latency |

### Posting something yourself

`/announce` is the one way anything reaches a channel that the tracker did not
write. Use it for a monthly PnL card, an equity curve, or a note:

- `message` is the text — type `\n` where you want a line break, since a
  slash-command box cannot hold a real one.
- `image` attaches a chart or screenshot. An image is shown inside the embed;
  anything else (a PDF, say) rides along as a plain file.
- `preview: true` renders it back to you and sends it nowhere, so a chart can
  be checked before it goes out.
- `mention: true` pings the subscribed role. Off by default, and an
  `@everyone` typed into the message is never allowed through.
- `plain: true` drops the embed and posts the text as-is.

It fans out to every subscribed server, like an alert, but nothing is
remembered afterwards — there is no setup for it to belong to, so it is never
edited or replied to later. **The no-money rule does not apply here**: an alert
renders a `Setup`, which has no figures in it to leak, while an announcement
carries whatever you typed or drew. That is the point of it, and the reason it
is a separate command rather than a flag on an alert.

## Behaviour worth knowing

- **A cold start is silent.** Orders already resting are adopted, not
  announced. `SEED_ALERTS=true` announces them once instead — useful the first
  time you wire a channel up.
- **An unreachable copier alerts nothing.** A failed read is not an empty book,
  so a copier that is down or stopped never produces a wall of "cancelled".
- **State survives restarts** in `data/alerts.db`, so a redeploy mid-trade does
  not re-announce anything.
- **A partial close is a percentage, not a size.** Taking half off reports
  `Trimmed 50%`, and a later trim adds how much of the position is off in
  total, counted from the largest it ever was. A move under 3% is treated as a
  rounding remainder and ignored; on Hyperliquid fees and funding come out of
  margin rather than out of the position, so a real trim is never that small.
- **A size change is announced once it settles.** A limit entry, and a
  take-profit worked in pieces, both walk the position across several polls.
  The tracker waits for the size to hold still for a tick, so one decision
  produces one alert rather than a stream — at the cost of one poll interval.
- **A pulled stop stops being advertised.** Cancelling a stop or target blanks
  it in the embed and replies `⚠️ Stop removed`, so the alert never claims
  protection that is no longer on the book. Cancel-and-replace is not that: a
  removal is judged a tick later, so replacing an order — even across a poll
  boundary — reads as the move it is. A conditional that vanished because it
  *filled* is told apart by the position size, which shrinks in that case and
  holds when the order was pulled; the trim alert reports it instead. A close
  is silent about the survivor the exchange cancels on the way out.
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
