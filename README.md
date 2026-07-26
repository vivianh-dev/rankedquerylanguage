# Ranked Query Language Discord bot

This is DesktopFolder’s Discord interface for Ranked Query Language. The bot
keeps the existing slash commands and legacy query syntax, while query
compilation and execution are provided by the hosted RQL service.

## Commands

The existing commands remain available:

- `/average_completion`
- `/qb_info`
- `/qb_quicklook`
- `/qb_quicksplits`
- `/qb_leaderboard`
- `/qb_top_activity`
- `/qb_matchup`
- `/qb_faq`
- `/query`

`/query` waits for the hosted query result and replies in the channel, matching
the existing behavior. `/query-async` queues longer work, acknowledges
ephemerally, and DMs the result when it is ready.

Async delivery is durable across bot restarts. SQLite stores only the RQL job
ID, Discord user ID, and delivery timing metadata; query text is not stored by
the bot.

## Setup

Python 3.10 or newer is required.

```sh
python -m venv .env
source .env/bin/activate
python -m pip install -r requirements.txt
```

Set the following environment variables:

```sh
export RQL_API_KEY='dedicated-standard-tier-key'
export DISCORD_TOKEN='discord-bot-token'
```

Configuration:

| Variable | Default | Purpose |
| --- | --- | --- |
| `RQL_API_BASE_URL` | `https://rql.vivianh.dev/api/v1/legacy` | Hosted legacy-compatible RQL API |
| `RQL_API_KEY` | required | Dedicated standard-tier API key |
| `RQL_API_TIMEOUT_SECONDS` | `130` | Synchronous HTTP deadline |
| `RQL_ASYNC_POLL_SECONDS` | `2` | Async job polling interval |
| `RQL_STATE_PATH` | `.state/querybot.sqlite3` | Durable DM-delivery state |
| `DISCORD_TOKEN` | falls back to `token.txt` | Discord bot token |
| `LOG_LEVEL` | `INFO` | Python logging level |

Except for loopback development, the RQL base URL must use HTTPS. API keys and
query text are not written to logs.

Run the Discord bot:

```sh
python bot.py
```

Run the hosted-API test CLI:

```sh
python bot.py --fake
```

## Tests

The test suite uses an in-process fake RQL server and does not require a real
API key or Discord token:

```sh
python -m unittest discover -s tests -v
```

It covers synchronous responses and attachments, safe errors and timeouts,
async submission/polling/results, SQLite restart recovery, DM retry retention,
and the slash-command registry.
