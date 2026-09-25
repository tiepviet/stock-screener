"""Application services for the FastAPI routes.

All functions in this module are synchronous.  FastAPI runs synchronous route
handlers in Starlette's worker threadpool, and the existing domain modules
already use bounded thread pools for batch provider calls.  Keeping the
orchestration synchronous also makes it straightforward to reuse the same
objects in CLI/tests without an async event loop.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from src.stock_screener.alert import AlertScanner
from src.stock_screener.backtest import Backtester
from src.stock_screener.data_loader import ProviderBusyError, YFinanceDataLoader, jst_now
from src.stock_screener.earnings_calendar import EarningsCalendar
from src.stock_screener.fundamental_screener import Condition, FundamentalScreener
from src.stock_screener.multi_timeframe import MultiTimeframeConfirmer
from src.stock_screener.portfolio import PortfolioTracker, migrate_legacy_portfolio_to_user
from src.stock_screener.price_target import PriceTargetEngine
from src.stock_screener.risk_management import PositionPlan, RiskManager
from src.stock_screener.screen_chain import ScreenChainer
from src.stock_screener.technical_engine import (
    OverboughtReversalSellStrategy,
    PullbackMAStrategy,
    TechnicalEngine,
    TrendBreakdownSellStrategy,
    VolumeBreakoutStrategy,
)

logger = logging.getLogger(__name__)


def _provider_limit() -> int:
    try:
        value = int(os.getenv("TSE_PROVIDER_MAX_CONCURRENCY", "8"))
    except (TypeError, ValueError):
        value = 8
    return max(1, min(value, 64))


_PROVIDER_SEMAPHORE = threading.BoundedSemaphore(_provider_limit())


@contextmanager
def provider_slot() -> Iterator[None]:
    """Bound concurrent provider work across simultaneous API requests."""

    if not _PROVIDER_SEMAPHORE.acquire(timeout=30):
        raise ProviderBusyError("market data provider capacity is busy; retry shortly")
    try:
        yield
    finally:
        _PROVIDER_SEMAPHORE.release()


class ServiceError(RuntimeError):
    """An expected domain/provider error safe to expose as a 4xx/502."""


class MarketDataError(ServiceError):
    """Market data could not be loaded for a valid request."""


class ProviderUnavailableError(ServiceError):
    """The upstream market-data provider failed or timed out."""


class AlertDeliveryRevokedError(RuntimeError):
    """The persisted alert capability changed before delivery."""


def _date_string(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def date_window(
    lookback_days: int,
    start: date | str | None = None,
    end: date | str | None = None,
) -> tuple[str, str]:
    """Return a validated YYYY-MM-DD window ending in the JST market date."""

    end_date = (
        date.fromisoformat(str(end)[:10])
        if end is not None
        else jst_now().date()
    )
    start_date = (
        date.fromisoformat(str(start)[:10])
        if start is not None
        else end_date - timedelta(days=lookback_days)
    )
    if start_date > end_date:
        raise ServiceError("start must be on or before end")
    if (end_date - start_date).days > 3650:
        raise ServiceError("date range cannot exceed 3650 days")
    return start_date.isoformat(), end_date.isoformat()


def get_loader() -> YFinanceDataLoader:
    """Create a loader per operation; its disk cache remains shared."""

    return YFinanceDataLoader()


def fetch_ohlcv(
    loader: YFinanceDataLoader,
    ticker: str,
    lookback_days: int,
    interval: str = "1d",
    start: date | str | None = None,
    end: date | str | None = None,
) -> pd.DataFrame:
    start_date, end_date = date_window(lookback_days, start=start, end=end)
    try:
        with provider_slot():
            return loader.fetch_ohlcv(ticker, start_date, end_date, interval)
    except ProviderBusyError:
        raise
    except ValueError as exc:
        raise MarketDataError(str(exc)) from exc
    except Exception as exc:  # provider failures should not expose internals
        logger.exception("OHLCV request failed for %s", ticker)
        raise ProviderUnavailableError("market data provider is unavailable") from exc


def fetch_enriched(
    loader: YFinanceDataLoader,
    ticker: str,
    lookback_days: int,
    interval: str = "1d",
    start: date | str | None = None,
    end: date | str | None = None,
) -> pd.DataFrame:
    df = fetch_ohlcv(loader, ticker, lookback_days, interval, start, end)
    try:
        return TechnicalEngine().enrich(df)
    except Exception as exc:
        logger.exception("Indicator calculation failed for %s", ticker)
        raise ProviderUnavailableError("technical indicator calculation failed") from exc


def _conditions_from_payload(payload: Any) -> list[Condition]:
    if payload.conditions:
        return [Condition(item.metric, item.operator, item.value) for item in payload.conditions]

    # A request with no explicit conditions gets a conservative default
    # screen, while individual thresholds can override the corresponding
    # default one.  This mirrors the legacy dashboard while remaining a
    # useful API default.
    defaults = {
        "roe": (">", 0.08),
        "pe": ("<", 20.0),
        "pb": ("<", 3.0),
        "eps": (">", 0.0),
        "dividend_yield": (">", 0.005),
    }
    if payload.min_roe is not None:
        defaults["roe"] = (">", payload.min_roe)
    if payload.max_pe is not None:
        defaults["pe"] = ("<", payload.max_pe)
    if payload.max_pb is not None:
        defaults["pb"] = ("<", payload.max_pb)
    if payload.min_dividend_yield is not None:
        defaults["dividend_yield"] = (">", payload.min_dividend_yield)
    return [Condition(metric, operator, value) for metric, (operator, value) in defaults.items()]


def screen_fundamentals(
    payload: Any, user_id: int
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    tickers = list(payload.tickers)
    if payload.watchlist_only:
        from src.stock_screener import user_store

        watchlist = set(user_store.get_watchlist(user_id))
        # An empty watchlist means "no filter", matching the legacy UI.
        if watchlist:
            tickers = [ticker for ticker in tickers if ticker in watchlist]

    if not tickers:
        return [], []

    conditions = _conditions_from_payload(payload)
    try:
        with provider_slot():
            screener = FundamentalScreener(get_loader())
            result = screener.screen(tickers, conditions)
            errors = [
                {"ticker": ticker, "error": message}
                for ticker, message in getattr(screener, "last_errors", {}).items()
            ]
    except ProviderBusyError:
        raise
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc
    except Exception as exc:
        logger.exception("Fundamental screen failed")
        raise ServiceError("fundamental screen failed") from exc
    return _records_from_fundamental_frame(result), errors


def _records_from_fundamental_frame(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return [
        {str(key): value for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _strategy(name: str, payload: Any) -> Any:
    if name == "VolumeBreakout":
        return VolumeBreakoutStrategy(
            lookback=payload.volume_breakout_lookback,
            volume_mult=payload.volume_multiplier,
        )
    if name == "PullbackMA":
        return PullbackMAStrategy(rsi_max=payload.pullback_rsi_max)
    if name == "TrendBreakdown":
        return TrendBreakdownSellStrategy()
    if name == "OverboughtReversal":
        return OverboughtReversalSellStrategy()
    raise ServiceError(f"unsupported strategy: {name}")


def scan_signals(payload: Any) -> tuple[list[Any], list[dict[str, str]]]:
    loader = get_loader()
    start, end = date_window(payload.lookback_days)
    strategy_names = list(payload.strategies)
    if not payload.include_sell_strategies:
        strategy_names = [name for name in strategy_names if name in {"VolumeBreakout", "PullbackMA"}]
    if not strategy_names:
        raise ServiceError("at least one buy strategy must be enabled")
    strategies = [_strategy(name, payload) for name in strategy_names]
    signals: list[Any] = []
    errors: list[dict[str, str]] = []

    def scan_one(ticker: str) -> list[Any]:
        frame = fetch_enriched(loader, ticker, payload.lookback_days, start=start, end=end)
        found: list[Any] = []
        for strategy in strategies:
            found.extend(strategy.generate_signals(frame, ticker))
        return found

    # A provider failure for one symbol should not discard successful symbols.
    with ThreadPoolExecutor(max_workers=min(len(payload.tickers), 8)) as pool:
        futures = {pool.submit(scan_one, ticker): ticker for ticker in payload.tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                signals.extend(future.result())
            except Exception as exc:
                logger.info("Signal scan failed for %s: %s", ticker, exc)
                errors.append({"ticker": ticker, "error": "data unavailable"})
    signals.sort(key=lambda item: (item.ticker, str(item.date), item.strategy))
    return signals, errors


def position_plans(
    signals: list[Any],
    capital: float,
    risk_per_trade: float,
    hard_stop_pct: float,
) -> list[PositionPlan]:
    """Size BUY signals using the same risk manager as the legacy UI."""

    try:
        return RiskManager(
            total_capital=capital,
            risk_per_trade=risk_per_trade,
            hard_stop_pct=hard_stop_pct,
        ).batch_positions([signal for signal in signals if signal.signal_type.value == "BUY"])
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


def smart_screen(payload: Any, user_id: int) -> list[Any]:
    tickers = list(payload.tickers)
    if payload.watchlist_only:
        from src.stock_screener import user_store

        watchlist = set(user_store.get_watchlist(user_id))
        if watchlist:
            tickers = [ticker for ticker in tickers if ticker in watchlist]
    if not tickers:
        return []

    default_weights = {
        "roe": 0.25,
        "pe": 0.20,
        "trend": 0.20,
        "volume": 0.15,
        "rsi": 0.10,
        "dividend": 0.10,
    }
    weights = {**default_weights, **(payload.weights or {})}
    try:
        with provider_slot():
            return ScreenChainer(loader=get_loader(), weights=weights).run(
                tickers,
                fundamental_conditions=_conditions_from_payload(payload),
                top_n=payload.top_n,
                lookback_days=payload.lookback_days,
            )
    except ProviderBusyError:
        raise
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc
    except Exception as exc:
        logger.exception("Smart screen failed")
        raise ServiceError("smart screen failed") from exc


def scan_multi_timeframe(payload: Any) -> list[Any]:
    try:
        with provider_slot():
            return MultiTimeframeConfirmer(
                loader=get_loader(),
                min_confidence=payload.min_confidence,
            ).scan_tickers(payload.tickers, lookback_days=payload.lookback_days)
    except ProviderBusyError:
        raise
    except Exception as exc:
        logger.exception("Multi-timeframe scan failed")
        raise ServiceError("multi-timeframe scan failed") from exc


def check_earnings(payload: Any) -> tuple[dict[str, Any], list[str], list[str]]:
    calendar = EarningsCalendar(loader=get_loader(), warning_days=payload.warning_days)
    try:
        with provider_slot():
            results = calendar.check_batch(payload.tickers)
        # filter_safe repeats provider calls in the current domain module.  We
        # derive the partition from the one result set instead.
    except ProviderBusyError:
        raise
    except Exception as exc:
        logger.exception("Earnings check failed")
        raise ServiceError("earnings provider request failed") from exc
    safe: list[str] = []
    risky: list[str] = []
    for ticker in payload.tickers:
        info = results.get(ticker)
        (risky if info is not None and info.is_upcoming else safe).append(ticker)
    return results, safe, risky


def analyze_price_targets(payload: Any) -> Any:
    loader = get_loader()
    frame = fetch_enriched(loader, payload.ticker, payload.lookback_days)
    entry = payload.entry_price
    if entry is None:
        if frame.empty:
            raise MarketDataError("no valid close price")
        entry = float(frame["Close"].iloc[-1])
    stop_loss = payload.stop_loss
    if stop_loss is None:
        stop_loss = entry * (1 - payload.hard_stop_pct)
    if stop_loss >= entry:
        raise ServiceError("stop_loss must be below entry price")
    try:
        return PriceTargetEngine(
            swing_lookback=payload.swing_lookback,
            atr_period=payload.atr_period,
            atr_mult=payload.atr_mult,
        ).compute_all(frame, payload.ticker, entry_price=entry, stop_loss=stop_loss)
    except Exception as exc:
        logger.exception("Price target calculation failed for %s", payload.ticker)
        raise ServiceError("price target calculation failed") from exc


def run_backtest(payload: Any) -> Any:
    loader = get_loader()
    frame = fetch_ohlcv(loader, payload.ticker, payload.lookback_days)
    if payload.strategy == "VolumeBreakout":
        strategy = VolumeBreakoutStrategy()
    else:
        strategy = PullbackMAStrategy()
    try:
        backtester = Backtester(
            initial_capital=payload.initial_capital,
            risk_per_trade=payload.risk_per_trade,
            hard_stop_pct=payload.hard_stop_pct,
            max_holding_days=payload.max_holding_days,
            take_profit_pct=payload.take_profit_pct,
            commission_pct=payload.commission_pct,
            slippage_pct=payload.slippage_pct,
        )
        return backtester.run_multi(frame, strategy, payload.ticker)
    except (ValueError, KeyError) as exc:
        raise ServiceError(str(exc)) from exc
    except ProviderBusyError:
        raise
    except Exception as exc:
        logger.exception("Backtest failed for %s", payload.ticker)
        raise ServiceError("backtest failed") from exc


def portfolio_tracker(user_id: int, settings: dict[str, Any] | None = None) -> PortfolioTracker:
    """Build a tracker whose persistence is isolated to ``user_id``."""

    migrate_legacy_portfolio_to_user(user_id)
    settings = settings or {}
    capital = settings.get("capital", settings.get("total_capital", 10_000_000))
    try:
        capital = float(capital)
    except (TypeError, ValueError):
        capital = 10_000_000
    if not math.isfinite(capital) or capital <= 0:
        capital = 10_000_000
    try:
        max_sector = float(settings.get("max_sector_pct", 0.30))
    except (TypeError, ValueError):
        max_sector = 0.30
    if not math.isfinite(max_sector):
        max_sector = 0.30
    return PortfolioTracker(
        total_capital=capital,
        max_sector_pct=max(0.0, min(max_sector, 1.0)),
        user_id=user_id,
    )


def add_portfolio_position(
    tracker: PortfolioTracker,
    payload: Any,
    sector: str | None = None,
) -> None:
    plan = PositionPlan(
        ticker=payload.ticker,
        entry_price=payload.entry_price,
        stop_loss=payload.stop_loss,
        shares=payload.shares,
        position_value=round(payload.entry_price * payload.shares, 2),
        risk_amount=round((payload.entry_price - payload.stop_loss) * payload.shares, 2),
        risk_pct=0.0,
        strategy=payload.strategy,
    )
    try:
        tracker.add_position(
            plan,
            sector=payload.sector if sector is None else sector,
            take_profit_levels=payload.take_profit_levels,
            trail_pct=payload.trail_pct,
        )
    except Exception as exc:
        logger.exception("Could not add portfolio position")
        raise ServiceError("could not add portfolio position") from exc


def run_alert_scan_with_delivery(
    payload: Any,
    *,
    before_delivery: Callable[[], bool] | None = None,
) -> tuple[dict[str, list[Any]], dict[str, bool], list[dict[str, str]]]:
    scanner = AlertScanner(tickers=payload.tickers, lookback_days=payload.lookback_days)
    try:
        with provider_slot():
            results = scanner.scan()
        if payload.send_alerts:
            if before_delivery is not None and not before_delivery():
                raise AlertDeliveryRevokedError("alert delivery capability was revoked")
            scanner.deliver_results(results, can_deliver=before_delivery)
        return (
            results,
            dict(scanner.last_delivery),
            [
                {"ticker": ticker, "error": message}
                for ticker, message in scanner.scan_errors.items()
            ],
        )
    except (ProviderBusyError, AlertDeliveryRevokedError):
        raise
    except Exception as exc:
        logger.exception("Alert scan failed")
        raise ServiceError("alert scan failed") from exc


def run_alert_scan(payload: Any) -> dict[str, list[Any]]:
    """Backward-compatible scan helper without delivery metadata."""

    results, _, _ = run_alert_scan_with_delivery(payload)
    return results


__all__ = [
    "AlertDeliveryRevokedError",
    "MarketDataError",
    "ServiceError",
    "add_portfolio_position",
    "analyze_price_targets",
    "check_earnings",
    "date_window",
    "fetch_enriched",
    "fetch_ohlcv",
    "get_loader",
    "portfolio_tracker",
    "position_plans",
    "ProviderUnavailableError",
    "provider_slot",
    "run_alert_scan",
    "run_alert_scan_with_delivery",
    "run_backtest",
    "scan_multi_timeframe",
    "scan_signals",
    "screen_fundamentals",
    "smart_screen",
]
