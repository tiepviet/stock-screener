# TSE Stock Screener

Algorithmic trading & analysis tool for Tokyo Stock Exchange (TSE). Built with Python, Streamlit, and yfinance.

## Features

| Module | Description |
|---|---|
| `data_loader.py` | OHLCV & fundamentals ingestion with Parquet caching (JST-aware freshness). Pluggable interface for J-Quants / Rakuten APIs. |
| `technical_engine.py` | SMA, RSI, ATR, Bollinger Bands, Volume SMA. Four strategies: VolumeBreakout, PullbackMA + 2 sell-side. |
| `fundamental_screener.py` | Filter stocks by ROE, P/E, P/B, EPS, Dividend Yield with flexible conditions (fail-closed). |
| `risk_management.py` | Position sizing (1% rule), hard stop-loss (7%), trailing stop, batch position plans. |
| `backtest.py` | Historical simulation with commission + slippage (defaults 0.1%), win rate, Sharpe, max drawdown, equity curve. |
| `alert.py` | Telegram & Slack signal alerts. Daemon mode for daily 15:30 JST scans. |
| `portfolio.py` | Track positions, unrealized P/L, sector exposure. Persists to JSON. |
| `earnings_calendar.py` | Flag tickers with upcoming earnings (avoid pre-earnings risk). |
| `multi_timeframe.py` | Confirm signals across daily + weekly timeframes with confidence scoring. |
| `screen_chain.py` | Multi-pass screener: fundamental → technical → weighted scoring (missing data penalized). |
| `price_target.py` / `profit_target.py` | Cluster-based price targets and per-position profit calculators. |
| `auth.py` / `jwt_auth.py` / `db.py` / `user_store.py` | bcrypt + JWT auth (30-day tokens, rate-limited login, server-side revocation), SQLite persistence. |
| `watchlist.py` | Single source of truth for default / user / AI ticker lists. |
| `app.py` | Streamlit dashboard with 12 tabs (Chart, Screener, Signals, Backtest, Portfolio, Earnings, Smart Screen, MTF, Profit Target, Price Target, Alerts, Guide). |

## Quick Start

```bash
# Clone
git clone <repo-url>
cd stock-screener

# Virtual environment
python -m venv venv
source venv/bin/activate  # macOS/Linux
# venv\Scripts\activate   # Windows

# Install
pip install -r requirements.txt

# Run dashboard
streamlit run app.py
```

## Telegram Alerts

```bash
# 1. Create bot via @BotFather, get token
# 2. Get chat ID via @userinfobot

export TELEGRAM_BOT_TOKEN="123456:ABC-..."
export TELEGRAM_CHAT_ID="987654321"

# Run once
python -m src.stock_screener.alert --tickers 7203 6758 9984

# Run daily at 15:30 JST
python -m src.stock_screener.alert --daemon
```

Or copy `.env.example` to `.env` and fill in values.

## Project Structure

```
stock-screener/
├── src/
│   └── stock_screener/
│       ├── alert.py               # Telegram/Slack alerts + daemon
│       ├── auth.py                # bcrypt users, login rate limiting
│       ├── backtest.py            # backtesting with realistic costs
│       ├── data_loader.py         # yfinance + caching (JST-aware)
│       ├── db.py                  # SQLite schema + migrations
│       ├── earnings_calendar.py
│       ├── fundamental_screener.py
│       ├── jwt_auth.py            # JWT tokens with revocation
│       ├── multi_timeframe.py
│       ├── portfolio.py
│       ├── price_target.py        # cluster price-target analysis
│       ├── profit_target.py       # per-position target calculator
│       ├── risk_management.py
│       ├── screen_chain.py
│       ├── technical_engine.py
│       ├── user_store.py          # per-user settings/watchlist
│       ├── watchlist.py           # default ticker lists
│       └── assets/                # CSS
├── tests/                         # 160+ pytest tests
├── app.py                         # Streamlit dashboard (12 tabs)
├── requirements.txt
├── pyproject.toml
├── render.yaml                    # Render deployment (Python 3.12)
├── .env.example
├── .gitignore
└── README.md
```

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Data Loader │────▶│ Technical Engine  │────▶│    Strategies    │
│  (yfinance)  │     │  (pandas_ta)     │     │  Breakout / MA   │
└─────────────┘     └──────────────────┘     └────────┬────────┘
       │                                               │
       ▼                                               ▼
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Screener    │     │  Risk Management │     │     Backtest     │
│ (fundamental)│     │ (position size)  │     │   (simulate)     │
└─────────────┘     └──────────────────┘     └─────────────────┘
       │                       │                       │
       └───────────────────────┼───────────────────────┘
                               ▼
                    ┌──────────────────┐
                    │   Streamlit App   │
                    │  (dashboard)      │
                    └──────────────────┘
```

## Data Sources

| Source | Status | Notes |
|---|---|---|
| yfinance | Default | Free, auto `.T` suffix for JP tickers |
| J-Quants API | Planned | JPX official, paid (free tier available) |
| Rakuten Securities | Planned | Paid API |

## Deployment (Render)

- **Python 3.12** (see `render.yaml` — `requires-python >=3.12`, do not downgrade).
- Free tier: the SQLite DB (users, watchlist), JWT secret and caches live in an
  **ephemeral** filesystem and are **lost on every redeploy**. Set
  `TSE_ADMIN_USER` / `TSE_ADMIN_PASSWORD` in the Render dashboard (or .env) to
  re-create the admin after each deploy.
- Paid tier (Starter+): uncomment the disk block in `render.yaml`
  (`mountPath: /data`) and set `TSE_DATA_DIR=/data` to persist data across
  redeploys. Do **not** reference a disk on the free tier — deployment fails.

## Strategies

### VolumeBreakout
- Price breaks above 20-day high
- Volume > 1.5x average (20-day)
- Close > SMA20 (trend filter)
- Stop-loss: 2x ATR or 7% hard stop

### PullbackMA
- Uptrend: Close > SMA200
- Pullback recovery: Crosses back above SMA20/SMA50
- RSI < 60 (not overbought)
- Stop-loss: 2x ATR or 7% hard stop

## License

MIT
