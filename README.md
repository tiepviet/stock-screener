# TSE Stock Screener

Algorithmic trading & analysis tool for Tokyo Stock Exchange (TSE). Built with Python, Streamlit, and yfinance.

## Features

| Module | Description |
|---|---|
| `data_loader.py` | OHLCV & fundamentals ingestion with Parquet caching. Pluggable interface for J-Quants / Rakuten APIs. |
| `technical_engine.py` | SMA, RSI, ATR, Bollinger Bands, Volume SMA. Two built-in strategies: VolumeBreakout & PullbackMA. |
| `fundamental_screener.py` | Filter stocks by ROE, P/E, P/B, EPS, Dividend Yield with flexible conditions. |
| `risk_management.py` | Position sizing (1% rule), hard stop-loss (7%). |
| `backtest.py` | Historical simulation with win rate, Sharpe, max drawdown, equity curve. |
| `alert.py` | Telegram signal alerts. Daemon mode for daily 15:30 JST scans. |
| `portfolio.py` | Track positions, unrealized P/L, sector exposure. Persists to JSON. |
| `earnings_calendar.py` | Flag tickers with upcoming earnings (avoid pre-earnings risk). |
| `multi_timeframe.py` | Confirm signals across daily + weekly timeframes. |
| `screen_chain.py` | Multi-pass screener: fundamental → technical → weighted scoring. |
| `app.py` | Streamlit dashboard with 8 tabs. |

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
│       ├── __init__.py
│       ├── data_loader.py
│       ├── technical_engine.py
│       ├── fundamental_screener.py
│       ├── risk_management.py
│       ├── backtest.py
│       ├── alert.py
│       ├── portfolio.py
│       ├── earnings_calendar.py
│       ├── multi_timeframe.py
│       └── screen_chain.py
├── app.py                    # Streamlit dashboard entry point
├── requirements.txt
├── pyproject.toml
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
