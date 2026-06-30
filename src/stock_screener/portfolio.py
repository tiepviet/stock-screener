"""
Portfolio Tracker — track open positions, P/L, sector exposure,
trailing stops, and take-profit alerts.

Maintains a local JSON portfolio file and provides real-time
unrealized P/L calculation and sector diversification checks.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from .data_loader import YFinanceDataLoader
from .risk_management import PositionPlan

logger = logging.getLogger(__name__)

PORTFOLIO_FILE = Path(__file__).parent.parent.parent / "portfolio.json"


# ---------------------------------------------------------------------------
# Position model
# ---------------------------------------------------------------------------


@dataclass
class PortfolioPosition:
    """An active position in the portfolio."""

    ticker: str
    shares: int
    entry_price: float
    entry_date: str
    stop_loss: float
    strategy: str
    sector: str = ""
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    peak_price: float = 0.0            # highest price seen since entry
    trail_pct: float = 0.0             # 0 = trailing disabled
    trailing_stop: float = 0.0         # current trailing stop price
    take_profit_levels: list[float] = field(default_factory=list)
    tp_hit: list[bool] = field(default_factory=list)  # which TPs already triggered

    def __post_init__(self) -> None:
        if self.peak_price == 0:
            self.peak_price = self.entry_price
        if self.trailing_stop == 0:
            self.trailing_stop = self.stop_loss
        if not self.tp_hit:
            self.tp_hit = [False] * len(self.take_profit_levels)

    def update_price(self, price: float) -> None:
        """Update current price and recalculate P/L.

        Raises:
            ValueError: If price is negative.
        """
        if price < 0:
            raise ValueError(f"Price must be non-negative, got {price}")
        self.current_price = price
        self.unrealized_pnl = (price - self.entry_price) * self.shares
        self.unrealized_pnl_pct = (price - self.entry_price) / self.entry_price
        if price > self.peak_price:
            self.peak_price = price

    @property
    def market_value(self) -> float:
        return self.current_price * self.shares

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.shares

    @property
    def pnl_since_peak(self) -> float:
        """Drawdown % from peak price."""
        if self.peak_price <= 0:
            return 0.0
        return (self.current_price - self.peak_price) / self.peak_price

    def enable_trailing_stop(self, trail_pct: float = 0.05) -> None:
        """Activate trailing stop at given % below highest price."""
        self.trail_pct = trail_pct
        self.trailing_stop = round(self.peak_price * (1 - trail_pct), 2)
        if self.trailing_stop < self.stop_loss:
            self.trailing_stop = self.stop_loss

    def update_trailing_stop(self) -> float | None:
        """Move trailing stop up if price rose. Returns new stop or None."""
        if self.trail_pct <= 0:
            return None
        candidate = round(self.peak_price * (1 - self.trail_pct), 2)
        if candidate > self.trailing_stop:
            self.trailing_stop = candidate
            logger.info(
                "%s: trailing stop raised to ¥%.2f (peak=¥%.2f)",
                self.ticker, self.trailing_stop, self.peak_price,
            )
        return self.trailing_stop

    def check_trailing_stop(self) -> bool:
        """Check if price hit trailing stop. True = should exit."""
        if self.trail_pct <= 0 or self.current_price <= 0:
            return False
        return self.current_price <= self.trailing_stop

    def check_take_profits(self) -> list[int]:
        """Return indices of newly hit take-profit levels."""
        hit = []
        for i, tp in enumerate(self.take_profit_levels):
            if not self.tp_hit[i] and self.current_price >= tp:
                self.tp_hit[i] = True
                hit.append(i)
        return hit

    @property
    def highest_tp_hit(self) -> int:
        """Highest TP index that has been hit (-1 = none)."""
        for i in range(len(self.tp_hit) - 1, -1, -1):
            if self.tp_hit[i]:
                return i
        return -1


# ---------------------------------------------------------------------------
# Portfolio manager
# ---------------------------------------------------------------------------


class PortfolioTracker:
    """Manage portfolio positions, P/L tracking, and sector exposure.

    Persists to portfolio.json for cross-session continuity.
    """

    def __init__(
        self,
        total_capital: float = 10_000_000,
        max_sector_pct: float = 0.30,
    ) -> None:
        """Initialize tracker.

        Args:
            total_capital: Total portfolio capital for allocation checks.
            max_sector_pct: Max allowed exposure per sector (default 30%).
        """
        self.total_capital = total_capital
        self.max_sector_pct = max_sector_pct
        self.loader = YFinanceDataLoader()
        self.positions: dict[str, PortfolioPosition] = {}
        self.closed_trades: list[dict] = []
        self._load()

    # --- Persistence ---

    def _load(self) -> None:
        if PORTFOLIO_FILE.exists():
            try:
                data = json.loads(PORTFOLIO_FILE.read_text())
                for t, pos_data in data.get("positions", {}).items():
                    self.positions[t] = PortfolioPosition(**pos_data)
                self.closed_trades = data.get("closed_trades", [])
                logger.info("Loaded portfolio: %d positions", len(self.positions))
            except Exception:
                logger.exception("Failed to load portfolio")

    def _save(self) -> None:
        data = {
            "positions": {t: asdict(p) for t, p in self.positions.items()},
            "closed_trades": self.closed_trades,
            "updated_at": datetime.now().isoformat(),
        }
        PORTFOLIO_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    # --- Position management ---

    def add_position(
        self,
        plan: PositionPlan,
        sector: str = "",
        take_profit_levels: list[float] | None = None,
        trail_pct: float = 0.0,
    ) -> None:
        """Add a new position from a RiskManager plan.

        Args:
            plan: PositionPlan from risk_management module.
            sector: Sector classification string.
            take_profit_levels: Optional list of TP prices.
            trail_pct: Trailing stop % (0 = disabled).
        """
        if plan.ticker in self.positions:
            logger.warning("Position for %s already exists — skipping", plan.ticker)
            return

        if not sector:
            try:
                fundies = self.loader.fetch_fundamentals(plan.ticker)
                sector = fundies.get("sector") or ""
            except Exception:
                logger.debug("Sector auto-fetch failed for %s", plan.ticker)

        tps = take_profit_levels or []
        pos = PortfolioPosition(
            ticker=plan.ticker,
            shares=plan.shares,
            entry_price=plan.entry_price,
            entry_date=datetime.now().strftime("%Y-%m-%d"),
            stop_loss=plan.stop_loss,
            strategy=plan.strategy,
            sector=sector or "Unknown",
            peak_price=plan.entry_price,
            trailing_stop=plan.stop_loss,
            trail_pct=trail_pct,
            take_profit_levels=tps,
            tp_hit=[False] * len(tps),
        )
        self.positions[plan.ticker] = pos
        self._save()
        logger.info("Added position: %s", pos)

    def close_position(self, ticker: str, exit_price: float, reason: str = "") -> None:
        """Close a position and record the trade.

        Args:
            ticker: Ticker to close.
            exit_price: Exit price.
            reason: Reason for closing (STOP_LOSS, TRAILING_STOP, TAKE_PROFIT, MANUAL).
        """
        if ticker not in self.positions:
            logger.warning("No position for %s", ticker)
            return

        pos = self.positions.pop(ticker)
        pos.update_price(exit_price)
        pnl = pos.unrealized_pnl
        pnl_pct = pos.unrealized_pnl_pct

        self.closed_trades.append({
            "ticker": ticker,
            "shares": pos.shares,
            "entry_price": pos.entry_price,
            "entry_date": pos.entry_date,
            "exit_price": exit_price,
            "exit_date": datetime.now().strftime("%Y-%m-%d"),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "strategy": pos.strategy,
            "reason": reason,
            "peak_price": pos.peak_price,
        })
        self._save()
        logger.info("Closed %s: P/L=¥%.0f (%.2f%%) [%s]", ticker, pnl, pnl_pct * 100, reason)

    def recalc_targets(self, ticker: str) -> list[float]:
        """Recalculate take-profit levels using PriceTargetEngine.

        Returns:
            List of TP prices (or empty if calc fails).
        """
        if ticker not in self.positions:
            return []
        from datetime import datetime, timedelta

        from .price_target import PriceTargetEngine
        from .technical_engine import TechnicalEngine

        pos = self.positions[ticker]
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        try:
            df = self.loader.fetch_ohlcv(ticker, start, end)
            df = TechnicalEngine().enrich(df)
            pte = PriceTargetEngine()
            targets = pte.compute_all(
                df, ticker,
                entry_price=pos.entry_price,
                stop_loss=pos.stop_loss,
            )
            tps = targets.take_profits[:3] if targets.take_profits else []
            pos.take_profit_levels = tps
            pos.tp_hit = [False] * len(tps)
            self._save()
            return tps
        except Exception:
            logger.exception("recalc_targets failed for %s", ticker)
            return []

    def enable_trailing(self, ticker: str, trail_pct: float = 0.05) -> bool:
        """Enable trailing stop for a position."""
        if ticker not in self.positions:
            return False
        self.positions[ticker].enable_trailing_stop(trail_pct)
        self._save()
        return True

    # --- Monitoring ---

    def update_prices(self) -> None:
        """Refresh current prices for all open positions (parallel).

        Price lookup order: fast_info.lastPrice -> previousClose -> info.currentPrice
        -> regularMarketPrice -> previousClose. Fails silently per position; the
        last known price is retained on error.
        """
        from concurrent.futures import ThreadPoolExecutor

        import yfinance as yf

        def _update_one(ticker: str, pos) -> None:
            try:
                normalized = self.loader.normalize_ticker(ticker)
                t = yf.Ticker(normalized)
                price = self._fetch_latest_price(t)
                if price is not None and price > 0:
                    pos.update_price(float(price))
                else:
                    logger.warning("%s: no price available — keeping last known", ticker)
            except Exception:
                logger.exception("Failed to update price for %s", ticker)

        items = list(self.positions.items())
        with ThreadPoolExecutor(max_workers=min(len(items), 8)) as pool:
            for ticker, pos in items:
                pool.submit(_update_one, ticker, pos)
        self._save()

    @staticmethod
    def _fetch_latest_price(t) -> float | None:
        """Try multiple price sources. Returns None if all fail."""
        try:
            fi = t.fast_info
            if hasattr(fi, "get"):
                price = fi.get("lastPrice") or fi.get("previousClose")
            else:
                price = getattr(fi, "last_price", None) or getattr(fi, "previous_close", None)
            if price:
                return float(price)
        except Exception:
            pass

        try:
            info = t.info
            if info:
                for key in ("currentPrice", "regularMarketPrice", "previousClose"):
                    val = info.get(key)
                    if val:
                        return float(val)
        except Exception:
            pass

        return None

    def update_trailing_stops(self) -> list[tuple[str, float]]:
        """Move trailing stops up for all positions with trail enabled.

        Returns:
            List of (ticker, new_stop) for stops that moved.
        """
        moved = []
        for ticker, pos in self.positions.items():
            if pos.trail_pct > 0:
                old = pos.trailing_stop
                new = pos.update_trailing_stop()
                if new is not None and new != old:
                    moved.append((ticker, new))
        if moved:
            self._save()
        return moved

    def check_stop_losses(self) -> list[str]:
        """Check all positions against their stop-loss levels.

        Returns:
            List of tickers that hit stop-loss.
        """
        triggered: list[str] = []
        for ticker, pos in self.positions.items():
            if pos.current_price <= 0:
                logger.debug("%s: no current price — skipping SL check", ticker)
                continue
            if pos.current_price <= pos.stop_loss:
                triggered.append(ticker)
                logger.warning("STOP LOSS: %s @ %.2f (SL=%.2f)", ticker, pos.current_price, pos.stop_loss)
        return triggered

    def check_trailing_stops(self) -> list[tuple[str, float]]:
        """Check trailing stops for all positions.

        Returns:
            List of (ticker, current_stop) for triggered stops.
        """
        triggered = []
        for ticker, pos in self.positions.items():
            if pos.check_trailing_stop():
                triggered.append((ticker, pos.trailing_stop))
                logger.warning(
                    "TRAILING STOP: %s @ %.2f (stop=%.2f, peak=%.2f)",
                    ticker, pos.current_price, pos.trailing_stop, pos.peak_price,
                )
        return triggered

    def check_take_profits(self) -> dict[str, list[int]]:
        """Check take-profit levels for all positions.

        Returns:
            Dict mapping ticker -> list of newly hit TP indices.
        """
        hit: dict[str, list[int]] = {}
        for ticker, pos in self.positions.items():
            if pos.current_price <= 0:
                continue
            new = pos.check_take_profits()
            if new:
                hit[ticker] = new
                for i in new:
                    logger.info(
                        "TAKE PROFIT %d: %s @ ¥%.2f (TP=¥%.2f)",
                        i + 1, ticker, pos.current_price, pos.take_profit_levels[i],
                    )
        if hit:
            self._save()
        return hit

    def full_check(self) -> dict[str, list]:
        """Run all checks in one call. Returns dict of events."""
        events = {
            "stop_losses": [],
            "trailing_stops": [],
            "take_profits": {},
        }
        events["stop_losses"] = self.check_stop_losses()
        events["trailing_stops"] = [t for t, _ in self.check_trailing_stops()]
        events["take_profits"] = self.check_take_profits()
        self.update_trailing_stops()
        return events

    # --- Analytics ---

    def total_market_value(self) -> float:
        """Sum of all position market values."""
        return sum(p.market_value for p in self.positions.values())

    def total_unrealized_pnl(self) -> float:
        """Sum of unrealized P/L across all positions."""
        return sum(p.unrealized_pnl for p in self.positions.values())

    def sector_exposure(self) -> dict[str, float]:
        """Calculate exposure per sector as fraction of total market value.

        Returns:
            Dict mapping sector name -> fraction (0.0 to 1.0).
        """
        mv = self.total_market_value()
        if mv == 0:
            return {}

        exposure: dict[str, float] = {}
        for pos in self.positions.values():
            sector = pos.sector or "Unknown"
            exposure[sector] = exposure.get(sector, 0) + pos.market_value

        return {s: v / mv for s, v in exposure.items()}

    def overexposed_sectors(self) -> dict[str, float]:
        """Return sectors exceeding max_sector_pct.

        Returns:
            Dict mapping sector -> actual fraction (only over-limit).
        """
        exposure = self.sector_exposure()
        return {s: f for s, f in exposure.items() if f > self.max_sector_pct}

    def summary_df(self) -> pd.DataFrame:
        """Return positions as a DataFrame for display."""
        if not self.positions:
            return pd.DataFrame()
        rows = [asdict(p) for p in self.positions.values()]
        df = pd.DataFrame(rows)
        cols = [
            "ticker", "shares", "entry_price", "current_price", "stop_loss",
            "trailing_stop", "peak_price", "unrealized_pnl", "unrealized_pnl_pct",
            "take_profit_levels", "tp_hit", "sector", "strategy", "entry_date",
        ]
        return df[[c for c in cols if c in df.columns]]

    def closed_trades_df(self) -> pd.DataFrame:
        """Return closed trades as a DataFrame."""
        if not self.closed_trades:
            return pd.DataFrame()
        return pd.DataFrame(self.closed_trades)

    def stats(self) -> dict:
        """Portfolio statistics."""
        closed = self.closed_trades
        winners = [t for t in closed if t["pnl"] > 0]
        losers = [t for t in closed if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in closed)

        return {
            "open_positions": len(self.positions),
            "total_market_value": round(self.total_market_value(), 2),
            "total_unrealized_pnl": round(self.total_unrealized_pnl(), 2),
            "closed_trades": len(closed),
            "winning_trades": len(winners),
            "losing_trades": len(losers),
            "win_rate": len(winners) / len(closed) if closed else 0,
            "total_realized_pnl": round(total_pnl, 2),
            "overexposed_sectors": self.overexposed_sectors(),
        }
