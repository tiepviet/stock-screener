"""FastAPI application for the TSE Stock Screener.

Run locally with::

    uvicorn backend.main:app --reload

The legacy Streamlit source file is not part of the runtime. This module is
the primary JSON backend and uses a versioned ``/api/v1`` prefix consumed by
the web client.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.stock_screener import auth, db, jwt_auth, user_store
from src.stock_screener.alert import SlackSender, TelegramSender
from src.stock_screener.data_loader import ProviderBusyError, enforce_cache_quota
from src.stock_screener.portfolio import PortfolioStorageError
from src.stock_screener.profit_target import TargetRow, calculate_exit_price, summarize
from src.stock_screener.watchlist import canonical_ticker

from . import services
from .dependencies import CurrentUser, client_ip, get_current_user
from .models import (
    AlertChannelTestRequest,
    AlertScanRequest,
    AutoScanSettingsRequest,
    BacktestRequest,
    ChangePasswordRequest,
    ChartSettingsRequest,
    EarningsRequest,
    FundamentalScreenRequest,
    LoginRequest,
    LoginResponse,
    MultiTimeframeRequest,
    PortfolioAddRequest,
    PortfolioCheckRequest,
    PortfolioCloseRequest,
    PriceTargetRequest,
    ProfitTargetCalculateRequest,
    ProfitTargetSummarizeRequest,
    SetupStatusResponse,
    SidebarSettingsRequest,
    SignalScanRequest,
    SmartScreenRequest,
    TargetRowsRequest,
    TrailingStopRequest,
    UserResponse,
    WatchlistRequest,
)
from .scheduler import auto_scan_loop, reset_auto_scan_schedule
from .security import SecurityHeadersMiddleware
from .serializers import (
    backtest_dict,
    confirmed_signal_dict,
    dataframe_records,
    portfolio_dict,
    price_targets_dict,
    safe_value,
    signal_dict,
)

logger = logging.getLogger(__name__)
API_PREFIX = "/api/v1"


def _health_payload() -> dict[str, Any]:
    return {"status": "ok", "service": "tse-stock-screener", "version": "1.0.0"}


def health() -> dict[str, Any]:
    """Public module-level health handler used by the unversioned route."""

    return _health_payload()


def _initialise_storage() -> None:
    """Initialize SQLite and the optional environment bootstrap account."""

    db.init_db()
    jwt_auth._load_or_create_secret()
    auth.bootstrap_admin_from_env()
    auth.revoke_known_bootstrap_credentials()
    enforce_cache_quota()


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _docs_enabled() -> bool:
    environment = os.getenv("TSE_ENVIRONMENT", "development").strip().lower()
    default = environment not in {"production", "prod"}
    return _bool_env("TSE_ENABLE_DOCS", default)


def _allowed_hosts() -> list[str]:
    configured = os.getenv("TSE_ALLOWED_HOSTS", "")
    return [item.strip() for item in configured.split(",") if item.strip()]


def _cors_origins() -> list[str]:
    """Return explicitly configured browser origins.

    Same-origin production deployments need no CORS allowance.  Requiring an
    explicit ``TSE_CORS_ORIGINS`` value avoids accidentally exposing a Render
    deployment to every public origin by default.
    """

    configured = os.getenv("TSE_CORS_ORIGINS", os.getenv("CORS_ORIGINS", ""))
    if not configured.strip():
        return []
    origins: list[str] = []
    for raw_origin in configured.split(","):
        origin = raw_origin.strip()
        if not origin:
            continue
        if origin == "*":
            if os.getenv("TSE_ENVIRONMENT", "development").strip().lower() in {
                "production",
                "prod",
            }:
                logger.warning("Ignoring wildcard CORS origin in production")
                continue
            origins.append(origin)
            continue
        try:
            parsed = urlsplit(origin)
        except ValueError:
            logger.warning("Ignoring invalid CORS origin")
            continue
        production = os.getenv("TSE_ENVIRONMENT", "development").strip().lower() in {
            "production",
            "prod",
        }
        if (
            parsed.scheme not in {"http", "https"}
            or (production and parsed.scheme != "https")
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            logger.warning("Ignoring invalid CORS origin: %s", origin)
            continue
        origins.append(origin.rstrip("/"))
    return origins


def _public_validation_details(exc: ValidationError) -> list[dict[str, Any]]:
    """Return validation metadata without echoing rejected input values."""

    return [
        {
            "type": str(error.get("type", "validation_error")),
            "loc": list(error.get("loc", ())),
            "msg": str(error.get("msg", "Invalid request")),
        }
        for error in exc.errors()
    ]


def _alert_delivery_allowed(user: CurrentUser) -> bool:
    """Return whether the persisted account capability permits alert delivery."""

    return bool(user.can_send_alerts)


def _require_alert_delivery(user: CurrentUser) -> None:
    if not _alert_delivery_allowed(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Alert delivery is not permitted for this account",
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    # ``run_in_threadpool`` prevents SQLite schema creation and bcrypt bootstrap
    # from blocking the event loop during application startup.
    await run_in_threadpool(_initialise_storage)
    stop = asyncio.Event()
    task: asyncio.Task | None = None
    if _bool_env("TSE_AUTO_SCAN_SCHEDULER", True):
        task = asyncio.create_task(auto_scan_loop(stop))
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _user_payload(
    user_id: int, username: str, can_send_alerts: bool = False
) -> dict[str, Any]:
    return {
        "id": user_id,
        "username": username,
        "can_send_alerts": bool(can_send_alerts),
    }


def _issue_token(user: CurrentUser | auth.UserRecord) -> str:
    user_id = user.id
    username = user.username
    return jwt_auth.create_token(
        user_id,
        username,
        token_version=auth.get_token_version(user_id),
    )


def _parse_ticker(value: str) -> str:
    """Validate a path ticker without allowing provider path tricks."""

    ticker = canonical_ticker(value)
    if not ticker or len(ticker) > 20:
        raise HTTPException(status_code=422, detail="Invalid ticker")
    if not ticker[0].isalnum() or not all(ch.isalnum() or ch in "._-" for ch in ticker):
        raise HTTPException(status_code=422, detail="Invalid ticker")
    return ticker


def _sidebar_payload(raw: Any) -> dict[str, Any]:
    """Return both canonical API fields and compatibility field names."""

    if raw is None:
        raw = {}
    try:
        settings = SidebarSettingsRequest.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_public_validation_details(exc)) from exc
    result = settings.model_dump(mode="json")
    result.update(
        {
            "capital": settings.capital,
            "total_capital": settings.capital,
            "risk_percent": round(settings.risk_pct, 10),
            "risk_fraction": settings.risk_per_trade,
            "hard_stop_percent": round(settings.hard_stop_pct * 100, 10),
            "settings": settings.as_legacy_dict(),
        }
    )
    return result


def _chart_payload(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    try:
        settings = ChartSettingsRequest.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_public_validation_details(exc)) from exc
    return settings.model_dump(mode="json")


def _portfolio_settings(user: CurrentUser) -> dict[str, Any]:
    raw = user_store.get_setting(user.id, "sidebar", default={})
    return _sidebar_payload(raw)


def _portfolio_response(tracker: Any) -> dict[str, Any]:
    result = portfolio_dict(tracker)
    result.update(
        {
            "total_capital": safe_value(tracker.total_capital),
            "max_sector_pct": safe_value(tracker.max_sector_pct),
        }
    )
    return result


def _normalise_target_rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [row.model_dump(mode="json") if hasattr(row, "model_dump") else dict(row) for row in rows]


def _chart_rows(frame: Any, limit: int) -> list[dict[str, Any]]:
    """Map domain OHLCV/indicator names to the lower-case web contract."""

    rows = dataframe_records(frame, limit=limit)
    aliases = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
        "SMA_20": "sma_20",
        "SMA_50": "sma_50",
        "SMA_200": "sma_200",
        "RSI_14": "rsi_14",
        "ATR_14": "atr_14",
        "VOL_SMA_20": "vol_sma_20",
    }
    result: list[dict[str, Any]] = []
    for row in rows:
        converted: dict[str, Any] = {}
        for key, value in row.items():
            converted[aliases.get(str(key), str(key))] = value
        result.append(converted)
    # Frontend charts use a percentage change column when available.
    closes = [safe_value(item.get("close")) for item in result]
    for index, item in enumerate(result):
        previous = closes[index - 1] if index else None
        current = item.get("close")
        item["change_pct"] = (
            (float(current) - float(previous)) / float(previous)
            if isinstance(current, (int, float)) and isinstance(previous, (int, float)) and previous
            else None
        )
    return result


def _valid_profit_rows(rows: list[dict[str, Any]]) -> list[TargetRow]:
    result: list[TargetRow] = []
    for row in rows:
        try:
            entry_price = float(row.get("entry_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not row.get("ticker") or entry_price <= 0:
            continue
        try:
            result.append(
                TargetRow(
                    ticker=str(row["ticker"]),
                    entry_price=entry_price,
                    target_pct=float(row.get("target_pct", 5) or 0),
                    shares=int(row.get("shares", 0) or 0),
                )
            )
        except (TypeError, ValueError):
            # Draft/invalid rows are ignored by the summary endpoint; the
            # persisted rows endpoint remains strict about shape and ranges.
            continue
    return result


def _register_routes(router: APIRouter) -> None:
    # ------------------------------------------------------------------
    # Health and authentication
    # ------------------------------------------------------------------
    @router.get("/health", tags=["system"])
    def versioned_health() -> dict[str, Any]:
        return _health_payload()

    @router.get("/auth/setup-status", response_model=SetupStatusResponse, tags=["auth"])
    def setup_status() -> SetupStatusResponse:
        has_users = db.user_count() > 0
        return SetupStatusResponse(has_users=has_users, needs_setup=not has_users)

    @router.post("/auth/login", response_model=LoginResponse, tags=["auth"])
    async def login(payload: LoginRequest, request: Request) -> LoginResponse:
        try:
            record, error = await run_in_threadpool(
                auth.attempt_login,
                payload.username,
                payload.password,
                client_ip(request),
            )
        except Exception as exc:
            logger.exception("Login failed unexpectedly")
            raise HTTPException(status_code=500, detail="Login service unavailable") from exc
        if error == "locked":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts; try again later",
                headers={"Retry-After": str(auth.LOCKOUT_WINDOW_MINUTES * 60)},
            )
        if error == "invalid" or record is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        try:
            version = await run_in_threadpool(auth.get_token_version, record.id)
            token = await run_in_threadpool(
                _issue_token,
                CurrentUser(
                    record.id,
                    record.username,
                    version,
                    record.can_send_alerts,
                ),
            )
        except Exception as exc:
            logger.exception("Could not issue login token")
            raise HTTPException(status_code=500, detail="Login service unavailable") from exc
        return LoginResponse(
            access_token=token,
            user=UserResponse(
                id=record.id,
                username=record.username,
                can_send_alerts=record.can_send_alerts,
            ),
        )

    @router.get("/auth/me", tags=["auth"])
    def me(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        return _user_payload(user.id, user.username, user.can_send_alerts)

    @router.post("/auth/logout", tags=["auth"])
    def logout(user: CurrentUser = Depends(get_current_user)) -> dict[str, str]:
        try:
            auth.increment_token_version(user.id)
        except KeyError:
            # The auth dependency already proved existence; this is defensive
            # for a concurrent account deletion.
            pass
        return {"message": "Logged out"}

    @router.post("/auth/change-password", tags=["auth"])
    async def change_password(
        payload: ChangePasswordRequest,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        valid = await run_in_threadpool(auth.verify_user, user.username, payload.current_password)
        if valid is None or valid.id != user.id:
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        try:
            await run_in_threadpool(auth.change_password, user.id, payload.new_password)
            new_user = CurrentUser(
                user.id,
                user.username,
                await run_in_threadpool(auth.get_token_version, user.id),
                user.can_send_alerts,
            )
            # Existing tokens are intentionally revoked; return a fresh token
            # so a single-user client can continue without a second login.
            token = await run_in_threadpool(_issue_token, new_user)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "message": "Password changed; existing sessions were revoked",
            "access_token": token,
            "token_type": "bearer",
            "user": _user_payload(
                new_user.id, new_user.username, new_user.can_send_alerts
            ),
        }

    # ------------------------------------------------------------------
    # Per-user settings, watchlist and target rows
    # ------------------------------------------------------------------
    @router.get("/me/settings/sidebar", tags=["settings"])
    def get_sidebar_settings(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        return _sidebar_payload(user_store.get_setting(user.id, "sidebar", default={}))

    @router.put("/me/settings/sidebar", tags=["settings"])
    def save_sidebar_settings(
        payload: SidebarSettingsRequest,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        stored = payload.model_dump(mode="json")
        # Keep the compatibility shape in the same record so older clients can
        # continue to read settings without a migration step.
        stored.update(payload.as_legacy_dict())
        user_store.set_setting(user.id, "sidebar", stored)
        return _sidebar_payload(stored)

    @router.post("/me/settings/sidebar/reset", tags=["settings"])
    def reset_sidebar_settings(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        defaults = SidebarSettingsRequest()
        stored = defaults.model_dump(mode="json")
        stored.update(defaults.as_legacy_dict())
        user_store.set_setting(user.id, "sidebar", stored)
        user_store.save_target_rows(user.id, [])
        return _sidebar_payload(stored)

    @router.get("/me/settings/chart", tags=["settings"])
    def get_chart_settings(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        return _chart_payload(user_store.get_setting(user.id, "chart", default={}))

    @router.put("/me/settings/chart", tags=["settings"])
    def save_chart_settings(
        payload: ChartSettingsRequest,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        user_store.set_setting(user.id, "chart", payload.model_dump(mode="json"))
        return payload.model_dump(mode="json")

    @router.get("/me/profit-target-rows", tags=["settings"])
    def get_target_rows(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        rows = safe_value(user_store.get_target_rows(user.id))
        return {"rows": rows, "target_rows": rows, "count": len(rows)}

    @router.put("/me/profit-target-rows", tags=["settings"])
    def save_target_rows(
        payload: TargetRowsRequest,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        rows = _normalise_target_rows(payload.rows)
        user_store.save_target_rows(user.id, rows)
        return {"rows": rows, "target_rows": rows, "count": len(rows)}

    @router.get("/me/watchlist", tags=["settings"])
    def get_watchlist(user: CurrentUser = Depends(get_current_user)) -> list[str]:
        return safe_value(user_store.get_watchlist(user.id))

    @router.post("/me/watchlist", tags=["settings"])
    def add_watchlist(
        payload: WatchlistRequest,
        user: CurrentUser = Depends(get_current_user),
    ) -> list[str]:
        try:
            for ticker in payload.tickers:
                user_store.add_to_watchlist(user.id, ticker)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return safe_value(user_store.get_watchlist(user.id))

    @router.delete("/me/watchlist/{ticker}", tags=["settings"])
    def remove_watchlist(
        ticker: str,
        user: CurrentUser = Depends(get_current_user),
    ) -> list[str]:
        canonical = _parse_ticker(ticker)
        user_store.remove_from_watchlist(user.id, canonical)
        return safe_value(user_store.get_watchlist(user.id))

    @router.get("/me/auto-scan", tags=["settings"])
    def get_auto_scan(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        raw = user_store.get_setting(user.id, "auto_scan", default={})
        try:
            return AutoScanSettingsRequest.model_validate(raw or {}).model_dump(mode="json")
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_public_validation_details(exc)) from exc

    @router.put("/me/auto-scan", tags=["settings"])
    def save_auto_scan(
        payload: AutoScanSettingsRequest,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        if payload.enabled:
            _require_alert_delivery(user)
        user_store.set_setting(user.id, "auto_scan", payload.model_dump(mode="json"))
        reset_auto_scan_schedule(user.id, enabled=payload.enabled)
        return payload.model_dump(mode="json")

    @router.delete("/me/auto-scan", tags=["settings"])
    def delete_auto_scan(user: CurrentUser = Depends(get_current_user)) -> dict[str, bool]:
        user_store.set_setting(user.id, "auto_scan", {})
        reset_auto_scan_schedule(user.id, enabled=False)
        return {"deleted": True}

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    @router.get("/markets/defaults", tags=["markets"])
    def market_defaults(_user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        from src.stock_screener.watchlist import AI_TICKERS, DEFAULT_TICKERS, USER_TICKERS

        return {
            "default_tickers": safe_value(DEFAULT_TICKERS),
            "user_tickers": safe_value(USER_TICKERS),
            "ai_tickers": safe_value(AI_TICKERS),
            "intervals": ["1d", "1h", "1wk"],
            "timeframes": {"daily": "1d", "weekly": "1wk"},
        }

    @router.get("/markets/{ticker}/chart", include_in_schema=False)
    @router.get("/markets/{ticker}/ohlcv", tags=["markets"])
    def market_ohlcv(
        ticker: str,
        interval: Literal["1d", "1h", "1wk"] = Query(default="1d"),
        lookback_days: int = Query(default=365, ge=30, le=3650),
        start: date | None = Query(default=None),
        end: date | None = Query(default=None),
        limit: int = Query(default=5000, ge=1, le=10000),
        _user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        canonical = _parse_ticker(ticker)
        loader = services.get_loader()
        frame = services.fetch_enriched(
            loader,
            canonical,
            lookback_days,
            interval=interval,
            start=start,
            end=end,
        )
        start_date, end_date = services.date_window(lookback_days, start=start, end=end)
        rows = _chart_rows(frame, limit)
        return {
            "ticker": canonical,
            "normalized_ticker": loader.normalize_ticker(canonical),
            "interval": interval,
            "start": start_date,
            "end": end_date,
            "count": len(rows),
            "candles": rows,
            "ohlcv": rows,
            "rows": rows,
        }

    @router.get("/markets/{ticker}/fundamentals", tags=["markets"])
    def market_fundamentals(
        ticker: str,
        _user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        canonical = _parse_ticker(ticker)
        loader = services.get_loader()
        try:
            with services.provider_slot():
                data = loader.fetch_fundamentals(canonical)
        except ProviderBusyError:
            raise
        except ValueError as exc:
            raise services.MarketDataError("fundamentals data is unavailable") from exc
        except Exception as exc:
            logger.exception("Fundamentals request failed for %s", canonical)
            raise services.ProviderUnavailableError(
                "fundamentals provider is unavailable"
            ) from exc
        safe = safe_value(data)
        return {
            "ticker": canonical,
            "normalized_ticker": loader.normalize_ticker(canonical),
            "data": safe,
            "fundamentals": safe,
            **(safe if isinstance(safe, dict) else {}),
        }

    # ------------------------------------------------------------------
    # Screeners and signal engines
    # ------------------------------------------------------------------
    @router.post("/screeners/fundamental", tags=["screeners"])
    def fundamental_screen(
        payload: FundamentalScreenRequest,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        rows, errors = services.screen_fundamentals(payload, user.id)
        rows = safe_value(rows)
        return {"results": rows, "rows": rows, "count": len(rows), "errors": errors}

    @router.post("/screeners/smart", tags=["screeners"])
    def smart_screen(
        payload: SmartScreenRequest,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        results = safe_value([item.__dict__ for item in services.smart_screen(payload, user.id)])
        return {"results": results, "rows": results, "count": len(results)}

    @router.post("/signals/scans", tags=["signals"])
    def signal_scan(
        payload: SignalScanRequest,
        _user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        signals, errors = services.scan_signals(payload)
        serialized = [signal_dict(signal) for signal in signals]
        plans = services.position_plans(
            signals,
            capital=payload.capital_jpy,
            risk_per_trade=payload.risk_per_trade,
            hard_stop_pct=payload.hard_stop_pct,
        )
        plan_rows = [safe_value(vars(plan)) for plan in plans]
        by_type: dict[str, int] = {}
        by_strategy: dict[str, int] = {}
        for signal in serialized:
            by_type[signal["signal_type"]] = by_type.get(signal["signal_type"], 0) + 1
            by_strategy[signal["strategy"]] = by_strategy.get(signal["strategy"], 0) + 1
        return {
            "signals": serialized,
            "results": serialized,
            "position_plans": plan_rows,
            "count": len(serialized),
            "errors": errors,
            "summary": {"by_type": by_type, "by_strategy": by_strategy},
        }

    @router.post("/signals/multi-timeframe", tags=["signals"])
    def mtf_scan(
        payload: MultiTimeframeRequest,
        _user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        results = [confirmed_signal_dict(item) for item in services.scan_multi_timeframe(payload)]
        return {"signals": results, "results": results, "count": len(results)}

    # ------------------------------------------------------------------
    # Earnings, targets and backtesting
    # ------------------------------------------------------------------
    @router.post("/earnings/checks", tags=["earnings"])
    def earnings_check(
        payload: EarningsRequest,
        _user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        results, safe_tickers, risky_tickers = services.check_earnings(payload)
        serialized = safe_value(results)
        unknown = [
            ticker
            for ticker, info in results.items()
            if info.next_earnings_date is None
        ]
        safe_tickers = [ticker for ticker in safe_tickers if ticker not in unknown]
        return {
            "results": serialized,
            "items": safe_value(list(results.values())),
            "safe": safe_tickers,
            "risky": risky_tickers,
            "unknown": unknown,
            "count": len(serialized),
        }

    @router.post("/price-targets/analyze", tags=["targets"])
    def analyze_price_target(
        payload: PriceTargetRequest,
        _user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        result = price_targets_dict(services.analyze_price_targets(payload))
        return {"targets": result, "result": result, **result}

    @router.post("/profit-targets/calculate", tags=["targets"])
    def calculate_profit_target(
        payload: ProfitTargetCalculateRequest,
        _user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        try:
            exit_price = calculate_exit_price(payload.entry_price, payload.target_pct)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        profit = round(exit_price - payload.entry_price, 2)
        return {
            "entry_price": payload.entry_price,
            "target_pct": payload.target_pct,
            "exit_price": exit_price,
            "per_share_profit": profit,
            "profit": profit,
        }

    @router.post("/profit-targets/summarize", tags=["targets"])
    def summarize_profit_targets(
        payload: ProfitTargetSummarizeRequest,
        _user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        rows = _valid_profit_rows([row.model_dump(mode="json") for row in payload.rows])
        result = summarize(rows)
        detail = []
        for row in rows:
            detail.append(
                {
                    "ticker": row.ticker,
                    "entry_price": row.entry_price,
                    "target_pct": row.target_pct,
                    "shares": row.shares,
                    "exit_price": row.exit_price,
                    "per_share_profit": row.per_share_profit,
                    "position_value": row.position_value,
                    "target_value": row.target_value,
                    "profit": round(row.target_value - row.position_value, 2),
                }
            )
        summary = result.to_dict()
        return {
            "summary": summary,
            "rows": detail,
            "total_invested": summary["total_invested"],
            "total_target_value": summary["total_target_value"],
            "total_profit": summary["total_profit"],
            "weighted_target_pct": summary["weighted_target_pct"],
            "position_count": summary["position_count"],
        }

    @router.post("/backtests", tags=["backtest"])
    def run_backtest_endpoint(
        payload: BacktestRequest,
        _user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        result = backtest_dict(services.run_backtest(payload))
        return {"result": result, "metrics": result, **result}

    # ------------------------------------------------------------------
    # Portfolio (one persistence file per authenticated user)
    # ------------------------------------------------------------------
    @router.get("/portfolio", tags=["portfolio"])
    def get_portfolio(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        tracker = services.portfolio_tracker(user.id, _portfolio_settings(user))
        return _portfolio_response(tracker)

    @router.post("/portfolio/positions", status_code=201, tags=["portfolio"])
    def add_portfolio_position(
        payload: PortfolioAddRequest,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        tracker = services.portfolio_tracker(user.id, _portfolio_settings(user))
        sector = payload.sector
        if not sector:
            try:
                with services.provider_slot():
                    sector = (tracker.loader.fetch_fundamentals(payload.ticker) or {}).get("sector") or ""
            except Exception:
                sector = "Unknown"
            if not sector:
                sector = "Unknown"
        with tracker.transaction() as locked:
            if payload.ticker in locked.positions:
                raise HTTPException(status_code=409, detail="Position already exists")
            services.add_portfolio_position(locked, payload, sector=sector)
            response = _portfolio_response(locked)
        return response

    @router.post("/portfolio/positions/{ticker}/close", tags=["portfolio"])
    def close_portfolio_position(
        ticker: str,
        payload: PortfolioCloseRequest,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        canonical = _parse_ticker(ticker)
        tracker = services.portfolio_tracker(user.id, _portfolio_settings(user))
        with tracker.transaction() as locked:
            if canonical not in locked.positions:
                raise HTTPException(status_code=404, detail="Position not found")
            try:
                locked.close_position(canonical, payload.exit_price, payload.reason)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            response = _portfolio_response(locked)
        return response

    @router.patch("/portfolio/positions/{ticker}/trailing-stop", tags=["portfolio"])
    def enable_portfolio_trailing(
        ticker: str,
        payload: TrailingStopRequest,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        canonical = _parse_ticker(ticker)
        tracker = services.portfolio_tracker(user.id, _portfolio_settings(user))
        with tracker.transaction() as locked:
            if not locked.enable_trailing(canonical, payload.trail_pct):
                raise HTTPException(status_code=404, detail="Position not found")
            response = _portfolio_response(locked)
        return response

    @router.post("/portfolio/positions/{ticker}/recalculate-targets", tags=["portfolio"])
    def recalculate_portfolio_targets(
        ticker: str,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        canonical = _parse_ticker(ticker)
        tracker = services.portfolio_tracker(user.id, _portfolio_settings(user))
        if canonical not in tracker.positions:
            raise HTTPException(status_code=404, detail="Position not found")
        targets = tracker.recalc_targets(canonical)
        tracker._load()
        return {
            "ticker": canonical,
            "take_profit_levels": safe_value(targets),
            **_portfolio_response(tracker),
        }

    @router.post("/portfolio/refresh-prices", tags=["portfolio"])
    def refresh_portfolio_prices(
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        tracker = services.portfolio_tracker(user.id, _portfolio_settings(user))
        tracker.update_prices()
        tracker._load()
        return _portfolio_response(tracker)

    @router.post("/portfolio/check", tags=["portfolio"])
    def check_portfolio(
        payload: PortfolioCheckRequest | None = Body(default=None),
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        request = payload or PortfolioCheckRequest()
        if request.send_alerts:
            _require_alert_delivery(user)
        tracker = services.portfolio_tracker(user.id, _portfolio_settings(user))
        if request.refresh_prices:
            tracker.update_prices()
            tracker._load()
        with tracker.transaction() as locked:
            events = locked.full_check()
            response = {
                "events": safe_value(events),
                "alerts_sent": {"telegram": False, "slack": False},
                **_portfolio_response(locked),
            }
        if request.send_alerts and any(events.values()):
            # Re-read the persisted capability after any provider work so a
            # revoked account cannot send using a stale JWT identity.
            current_user = auth.get_by_username(user.username)
            if current_user is None or current_user.id != user.id or not current_user.can_send_alerts:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Alert delivery is not permitted for this account",
                )
            configured_channels = tuple(
                channel
                for channel, configured in {
                    "telegram": bool(
                        os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")
                    ),
                    "slack": bool(os.getenv("SLACK_WEBHOOK_URL")),
                }.items()
                if configured
            )
            claims = tracker.claim_alert_events(events, configured_channels)
            if claims:
                message = (
                    f"Portfolio alerts: stop_loss={events['stop_losses']}, "
                    f"trailing_stops={events['trailing_stops']}, "
                    f"take_profits={events['take_profits']}"
                )
                delivery = {"telegram": False, "slack": False}
                for channel, signature in claims.items():
                    sender = TelegramSender() if channel == "telegram" else SlackSender()
                    try:
                        sent = bool(
                            sender.send(f"<b>{message}</b>")
                            if channel == "telegram"
                            else sender.send(message)
                        )
                    except Exception:
                        logger.exception("Portfolio alert delivery failed for %s", channel)
                        sent = False
                    delivery[channel] = sent
                    tracker.record_alert_delivery(signature, channel, delivered=sent)
                response["alerts_sent"] = delivery
                response["alerts_sent_any"] = any(delivery.values())
        return response

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------
    @router.get("/alerts/channels", tags=["alerts"])
    def alert_channels(_user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        telegram_configured = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
        slack_configured = bool(os.getenv("SLACK_WEBHOOK_URL"))
        return {
            "telegram": {"configured": telegram_configured},
            "slack": {"configured": slack_configured},
            "telegram_configured": telegram_configured,
            "slack_configured": slack_configured,
        }

    @router.post("/alerts/channels/{channel}/test", tags=["alerts"])
    def test_alert_channel(
        channel: str,
        payload: AlertChannelTestRequest | None = Body(default=None),
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        _require_alert_delivery(user)
        if payload and "message" in payload.model_fields_set:
            raise HTTPException(
                status_code=422,
                detail="Custom alert test messages are not supported",
            )
        message = "Test alert from TSE Stock Screener"
        if channel == "telegram":
            sent = TelegramSender().send(message)
        elif channel == "slack":
            sent = SlackSender().send(message)
        else:
            raise HTTPException(status_code=404, detail="Unknown alert channel")
        return {
            "channel": channel,
            "sent": sent,
            "message": "Test alert sent" if sent else "Test alert was not delivered",
        }

    @router.post("/alerts/scans", tags=["alerts"])
    def alert_scan(
        payload: AlertScanRequest,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, Any]:
        if payload.send_alerts:
            _require_alert_delivery(user)

        def delivery_allowed() -> bool:
            current = auth.get_by_username(user.username)
            return bool(
                current is not None
                and current.id == user.id
                and current.can_send_alerts
            )

        results, delivery, scan_errors = services.run_alert_scan_with_delivery(
            payload,
            before_delivery=delivery_allowed if payload.send_alerts else None,
        )
        serialized = {
            ticker: [signal_dict(signal) for signal in signals]
            for ticker, signals in results.items()
        }
        total = sum(len(signals) for signals in serialized.values())
        return {
            "results": serialized,
            "signals": [signal for signals in serialized.values() for signal in signals],
            "count": total,
            "errors": scan_errors,
            "alerts_sent": any(delivery.values()),
            "delivery": delivery,
            "channels_configured": {
                "telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
                "slack": bool(os.getenv("SLACK_WEBHOOK_URL")),
            },
        }


def create_app() -> FastAPI:
    """Create an application instance (useful for tests and ASGI servers)."""

    docs_enabled = _docs_enabled()
    application = FastAPI(
        title="TSE Stock Screener API",
        description="JSON backend for Tokyo Stock Exchange screening and signals.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    origins = _cors_origins()
    allow_all = "*" in origins
    # Security is the outer layer so preflight and early error responses also
    # receive baseline headers and rate-limit accounting. CORS remains inside
    # it; the security layer mirrors its allowlist for responses it creates
    # before the downstream CORS middleware can run.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if allow_all else origins,
        allow_credentials=not allow_all,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )
    allowed_hosts = _allowed_hosts()
    if allowed_hosts:
        application.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=allowed_hosts,
        )
    application.add_middleware(
        SecurityHeadersMiddleware,
        cors_origins=origins,
        cors_allow_all=allow_all,
    )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Never include Pydantic's raw ``input`` in a public response. A
        # body-level error can contain credentials or other secrets alongside
        # the field that failed validation.
        errors = [
            {
                "type": str(error.get("type", "validation_error")),
                "loc": list(error.get("loc", ())),
                "msg": str(error.get("msg", "Invalid request")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @application.exception_handler(services.ServiceError)
    async def service_error_handler(_: Request, exc: services.ServiceError) -> JSONResponse:
        status_code = status.HTTP_400_BAD_REQUEST
        if isinstance(exc, services.MarketDataError):
            status_code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, services.ProviderUnavailableError):
            status_code = status.HTTP_502_BAD_GATEWAY
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})

    @application.exception_handler(services.AlertDeliveryRevokedError)
    async def alert_revoked_error_handler(
        _: Request, exc: services.AlertDeliveryRevokedError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "Alert delivery is not permitted for this account"},
        )

    @application.exception_handler(ProviderBusyError)
    async def provider_busy_error_handler(
        _: Request, exc: ProviderBusyError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": str(exc)},
            headers={"Retry-After": "5"},
        )

    @application.exception_handler(PortfolioStorageError)
    async def portfolio_storage_error_handler(
        _: Request, exc: PortfolioStorageError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": str(exc)},
            headers={"Retry-After": "60"},
        )

    @application.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled API error",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    router = APIRouter(prefix=API_PREFIX)
    _register_routes(router)
    application.include_router(router)

    # A small unversioned health URL is convenient for load balancers and does
    # not expose any authenticated data.
    application.add_api_route(
        "/health",
        health,
        methods=["GET"],
        include_in_schema=False,
    )

    # Serve the production React bundle when it exists. API routes are
    # registered first, so static mounting cannot shadow them. The fallback
    # keeps client-side navigation working for browser refreshes.
    frontend_dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if frontend_dist.is_dir() and (frontend_dist / "assets").is_dir():
        application.mount(
            "/assets",
            StaticFiles(directory=frontend_dist / "assets"),
            name="frontend-assets",
        )

        @application.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str) -> FileResponse:
            if full_path.startswith("api/") or (
                not docs_enabled and full_path in {"docs", "redoc", "openapi.json"}
            ):
                raise HTTPException(status_code=404, detail="Not found")
            return FileResponse(frontend_dist / "index.html")

    return application


# The importable ASGI application used by ``uvicorn backend.main:app``.
app = create_app()

__all__ = ["API_PREFIX", "app", "create_app", "health", "lifespan"]
