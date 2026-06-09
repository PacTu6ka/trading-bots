# Bothost / BotHost Pro deploy

This project is prepared for a single Python process on BotHost Pro:

- 4 vCPU
- 2 GB RAM
- 15 GB SSD
- unlimited env variables

Run command:

```bash
python run_bot_manager.py
```

Docker and Telegram webhook are not required for the first deployment. Telegram works through polling.

## What To Upload

Upload the project source code through Git or the Bothost panel.

Do not upload local runtime folders:

- `.venv/`
- `.idea/`
- `__pycache__/`
- `logs/`
- `data/`
- `state/`
- `trades/`

These folders are already covered by `.gitignore`.

## Env Variables

Set secrets only in the Bothost env panel:

```env
ARENA_TOKEN=your_arenago_token
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
TELEGRAM_ADMIN_IDS=your_telegram_user_id
```

Bot selection:

```env
ENABLED_BOTS=btc_bollinger
# or:
ENABLED_BOTS=btc_bollinger,btc_trend
# or:
ENABLED_BOTS=all
```

RAM controls:

```env
MARKET_DATA_MAX_BARS=600
LOG_MAX_BYTES=1048576
LOG_BACKUP_COUNT=5
```

The default `MARKET_DATA_MAX_BARS=600` is enough for the current Bollinger and trend bots while keeping DataFrames small. If a future strategy needs more history, increase this value deliberately.

## Current Managed Bots

`btc_bollinger`

- Strategy: Bollinger mean reversion
- Default portfolio: `btc`
- Enabled by default

`btc_trend`

- Strategy: multi-timeframe trend/divergence
- Default portfolio: `btc trend`
- Disabled by default to avoid two BTC strategies trading at once accidentally

Enable it explicitly:

```env
BTC_TREND_ENABLED=true
ENABLED_BOTS=btc_bollinger,btc_trend
```

## Telegram Commands

The bot shows a persistent Telegram keyboard with the same actions, so commands do not have to be typed manually.

- `/start`
- `/status`
- `/bots`
- `/balance`
- `/positions`
- `/trades`
- `/pause [bot|all]`
- `/resume [bot|all]`

Telegram does not expose direct buy/sell commands. Pause/resume only controls the manager loop.

## Preflight Check

Run locally or in the Bothost console:

```bash
python run_bot_manager.py --check-config
```

This prints sanitized settings and never prints real tokens.

Status without starting the live loop:

```bash
python run_bot_manager.py --status
```

## RAM Notes

The manager runs all enabled bots in one Python process. This avoids loading `pandas`, `numpy`, and `pyarrow` repeatedly for each bot.

Memory-saving choices:

- shared in-memory market-data cache by ticker and interval;
- DataFrames are trimmed to `MARKET_DATA_MAX_BARS`;
- Telegram polling uses existing `aiohttp`, no heavy Telegram framework;
- logs rotate by size;
- local caches/logs/state/trades are not deployed from the laptop.

On 2 GB RAM, start conservatively:

- safe baseline: 1-2 enabled bots;
- expected practical range: 3-5 similar bots;
- more strategies are possible if they share ticker/interval data and keep history windows small.

Watch Bothost memory graphs after enabling new bots.
