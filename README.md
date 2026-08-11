# Futures Breakout Trading Bot

Automated futures trading bot for TastyTrade that monitors any futures contract for hourly breakout signals and places bracket orders with configurable target/stop levels.

## Strategy

1. At the start of each configured session, the bot fetches the high/low of a reference hourly candle
2. During the active window, it monitors 1-minute candles for closes above the high (long) or below the low (short)
3. On breakout, it places a market entry with an OCO bracket (limit target + stop loss)
4. Each session allows up to 2 trades, one per direction

By default the bot runs in **dry-run mode**: signals are detected against live market data and simulated as if filled at the breakout price, but no broker orders are ever placed. Set `LIVE_TRADING=1` to place real orders.

## Architecture

```
main.py                  TradingBot orchestrator + entry point
breakout_bot/
  config.py              Settings dataclass, env loading, session definitions
  api_client.py          TastyTrade auth, bracket orders, quotes
  strategy.py            Pure signal logic: breakout entry/exit detection
  risk_management.py     Trade eligibility, session window checks
  data_handler.py        Historical candle fetching, streaming helpers
  notifications.py       Telegram integration
  state.py               JSON state persistence (BotState, ActiveTrade)
```

## Setup

1. Clone the repository
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your credentials:
   ```
   cp .env.example .env
   ```
4. Run:
   ```
   python main.py
   ```

## Configuration

All credentials are loaded from environment variables (`.env` file):

| Variable | Description |
|---|---|
| `TT_USERNAME` | TastyTrade username or token |
| `TT_PASSWORD` | TastyTrade password or session token |
| `TELEGRAM_TOKEN` | Telegram bot API token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for notifications |
| `SYMBOL_BASE` | Futures symbol (e.g., `/MNQ`, `/MES`, `/MHGH6`) |
| `SESSIONS_JSON` | Optional. JSON object defining one or more trading sessions (see `.env.example`). Defaults to a single session if unset. |
| `LIVE_TRADING` | Optional. `1`/`true`/`yes`/`on` to place real orders. Defaults to off (dry-run). |

Each session needs: `name`, `ref_hour` (hour whose candle sets the breakout levels), `start_hour`/`end_hour` (active trading window), `target_points`, `stop_points`. Multiple sessions can run per day, each independently tracking its own reference levels and trade count.

## State Persistence

The bot saves its state to `bot_state.json` on trade events. On restart, it restores same-day state to avoid duplicate trades. State resets automatically at midnight PT.

## Disclaimer

This software is for educational and research purposes. Trading futures involves substantial risk of loss. Use at your own risk.
