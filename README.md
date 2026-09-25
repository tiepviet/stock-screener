# TSE Stock Screener

Algorithmic trading and analysis dashboard for the Tokyo Stock Exchange (TSE).

The application is split into two applications:

- **FastAPI backend** — authentication, market data, screening, signals, backtests, portfolio, and alerts.
- **React frontend** — responsive dashboard built with Vite and React.

The analysis engine remains in `src/stock_screener/`; the UI no longer depends on Streamlit.

## Features

| Module | Description |
|---|---|
| `data_loader.py` | OHLCV and fundamentals ingestion with JST-aware Parquet/JSON caching. |
| `technical_engine.py` | SMA, RSI, ATR, Bollinger Bands, volume SMA, and four strategies. |
| `fundamental_screener.py` | Fail-closed ROE, P/E, P/B, EPS, and dividend filters. |
| `risk_management.py` | 1% risk rule, hard stop-loss, position sizing, and batch plans. |
| `backtest.py` | Historical simulation with commission, slippage, Sharpe, and drawdown metrics. |
| `alert.py` | Telegram and Slack signal delivery. |
| `portfolio.py` | Positions, unrealized P/L, sector exposure, trailing stops, and targets. |
| `earnings_calendar.py` | Upcoming earnings risk checks. |
| `multi_timeframe.py` | Daily + weekly signal confirmation. |
| `screen_chain.py` | Fundamental → technical → weighted ranking pipeline. |
| `price_target.py` / `profit_target.py` | Fibonacci zones, support/resistance, and profit calculators. |
| `auth.py` / `jwt_auth.py` / `db.py` / `user_store.py` | bcrypt credentials, JWT sessions, rate-limited login, and SQLite persistence. |

## Project structure

```text
stock-screener/
├── backend/                    # FastAPI application
│   ├── main.py                 # routes, lifespan, error handlers
│   ├── models.py               # Pydantic request/response contracts
│   ├── dependencies.py         # bearer auth and client identity
│   ├── security.py             # proxy trust, throttling, security headers
│   ├── services.py             # reusable analysis orchestration
│   └── serializers.py          # strict JSON-safe response conversion
├── frontend/                   # Vite + React application
│   ├── src/
│   │   ├── api.js              # typed API client and auth handling
│   │   ├── main.jsx            # dashboard views and components
│   │   └── styles.css          # dark responsive design system
│   └── package.json
├── src/stock_screener/         # reusable Python analysis engine
├── legacy/                     # unsupported Streamlit rollback reference only
├── tests/                      # Python unit and API tests
├── requirements.txt
├── pyproject.toml
├── render.yaml
└── README.md
```

## Quick start

### 1. Backend

```bash
python3 -m venv .venv
source .venv/bin/activate          # macOS/Linux
# .venv\\Scripts\\activate         # Windows
pip install -r requirements.txt
cp .env.example .env
uvicorn backend.main:app --reload --port 8000 --no-proxy-headers
```

API documentation:

- Swagger UI (development): <http://localhost:8000/docs>
- ReDoc (development): <http://localhost:8000/redoc>
- Health check: <http://localhost:8000/api/v1/health>

The first startup creates `data/screener.db`. Set `TSE_ADMIN_USER` and
`TSE_ADMIN_PASSWORD` in `.env` to create the initial account.

### 2. Frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api` requests to the FastAPI
server on port 8000. The old Streamlit UI is archived under `legacy/` only
as a rollback reference; it is not imported, built, or served by the FastAPI
application. For a production build:

```bash
npm run build
npm run preview
```

Set `VITE_API_URL` when the frontend is hosted separately, for example
`VITE_API_URL=https://api.example.com/api/v1`.

## API overview

All application routes are under `/api/v1` and authenticated routes use a
Bearer JWT returned by `POST /api/v1/auth/login`.

| Area | Examples |
|---|---|
| Auth | `POST /auth/login`, `GET /auth/me`, `POST /auth/logout` |
| User state | `GET/PUT /me/settings/sidebar`, `GET/PUT /me/watchlist`, `PUT /me/profit-target-rows` |
| Market data | `GET /markets/defaults`, `GET /markets/{ticker}/ohlcv`, `GET /markets/{ticker}/fundamentals` |
| Analysis | `POST /screeners/fundamental`, `POST /screeners/smart`, `POST /signals/scans`, `POST /backtests` |
| Risk and targets | `POST /earnings/checks`, `POST /price-targets/analyze`, `POST /profit-targets/summarize` |
| Portfolio | `GET /portfolio`, `POST /portfolio/refresh-prices`, `POST /portfolio/check` |
| Alerts | `GET /alerts/channels`, `POST /alerts/scans` (delivery requires `can_send_alerts`) |

The API returns JSON-safe values. Missing indicators are represented as
`null`, and partial scan failures are returned separately from valid results.

## Configuration

Copy `.env.example` to `.env` and configure:

```dotenv
TSE_ADMIN_USER=admin
TSE_ADMIN_PASSWORD=                 # set a unique value; do not copy a placeholder
TSE_JWT_SECRET=                     # generate with: openssl rand -hex 32
TSE_CORS_ORIGINS=http://localhost:5173
```

The API adds security headers, disables caching for authenticated/API
responses, and applies bounded request limits. `X-Forwarded-For` is ignored by
default; behind a reverse proxy, set `TSE_TRUSTED_PROXY_CIDRS` to the proxy
network(s) that are allowed to supply it. External alert delivery and channel
tests require the persisted `can_send_alerts` capability. The bootstrap admin
receives it; existing installations can grant or revoke it with
`python -m src.stock_screener.auth grant-alerts <username>` or
`revoke-alerts <username>`. Startup revokes the old example bootstrap
password if it is still present; use the CLI to set a replacement password.

Auto-scan settings are consumed by the single-worker scheduler started with
the FastAPI lifespan. It is the only supported broadcaster; the old GitHub
Actions scan workflow is retired and performs no delivery. Keep
`TSE_AUTO_SCAN_SCHEDULER=true` only on the single-instance deployment; ordinary
accounts cannot enable delivery.

Optional alert channels:

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
SLACK_WEBHOOK_URL=
```

Use a persistent `TSE_DATA_DIR` in production. The SQLite database, JWT
secret, caches, and per-user portfolio files are stored below that directory.
Portfolio writes use a per-file lock plus atomic replacement. If a file is
malformed, it is quarantined rather than silently overwritten. The pre-API
global `portfolio.json` is never assigned automatically; an administrator may
perform the one-time copy by setting `TSE_LEGACY_PORTFOLIO_USER_ID` to the
chosen user's numeric ID before that user opens the portfolio.

### Existing-state cutover to a persistent disk

Render deployments using `TSE_DATA_DIR=/data` do not automatically import a
legacy `./data` directory. Before switching traffic to the new service:

1. Stop writes to the old service and make a filesystem backup.
2. Verify the SQLite backup with `sqlite3 data/screener.db "PRAGMA integrity_check;"`.
3. Copy `screener.db`, `jwt_secret.key` (unless using `TSE_JWT_SECRET`), and the
   cache/portfolio files to the persistent disk as appropriate. Keep the old
   copy until the new deployment is verified.
4. Start one FastAPI worker so schema migrations run, then verify login,
   settings, watchlists, target rows, and per-user portfolios.
5. If intentionally importing the old global `portfolio.json`, set
   `TSE_LEGACY_PORTFOLIO_USER_ID` to the verified destination user ID before
   that user's first portfolio request, then confirm the copy and quarantine
   marker state.
6. Re-grant required alert capabilities with the CLI if the migration did not
   preserve the `can_send_alerts` column, and only then route users to the new
   service.

If existing state is intentionally discarded, remove/rename the old state
before first boot and bootstrap a new administrator; do not treat an empty
`/data` directory as a successful migration.

## Deployment (Render)

`render.yaml` builds the React bundle and starts a single Uvicorn worker:

```bash
npm ci --prefix frontend
npm run build --prefix frontend
pip install -r requirements.txt
```

Production disables Swagger/OpenAPI by default (`TSE_ENABLE_DOCS=false`).
For production, set `TSE_JWT_SECRET`, `TSE_ADMIN_USER`, and a unique
`TSE_ADMIN_PASSWORD` of at least 12 characters. `render.yaml` attaches the Starter service's
persistent disk at `/data` and sets `TSE_DATA_DIR=/data`; leave the service at
one worker while SQLite and JSON state are used. Set an explicit
`TSE_CORS_ORIGINS` value only when the frontend is hosted on another origin,
and configure `TSE_TRUSTED_PROXY_CIDRS` for the proxy in front of Render.

## Development commands

```bash
# Python tests
pytest

# Ruff
ruff check backend src tests

# Frontend production build
npm run build --prefix frontend

# CI also runs the Python tests/lint and a high-severity npm audit
```

## License

MIT
