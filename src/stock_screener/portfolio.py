"""
Portfolio Tracker — track open positions, P/L, and sector exposure.

Maintains a local JSON portfolio file and provides real-time
unrealized P/L calculation and sector diversification checks.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
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

    @property
    def market_value(self) -> float:
        return self.current_price * self.shares

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.shares


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

    def add_position(self, plan: PositionPlan, sector: str = "") -> None:
        """Add a new position from a RiskManager plan.

        Args:
            plan: PositionPlan from risk_management module.
            sector: Sector classification string. If empty, attempts to
                auto-fetch from yfinance (best-effort, falls back to "").
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

        pos = PortfolioPosition(
            ticker=plan.ticker,
            shares=plan.shares,
            entry_price=plan.entry_price,
            entry_date=datetime.now().strftime("%Y-%m-%d"),
            stop_loss=plan.stop_loss,
            strategy=plan.strategy,
            sector=sector or "Unknown",
        )
        self.positions[plan.ticker] = pos
        self._save()
        logger.info("Added position: %s", pos)

    def close_position(self, ticker: str, exit_price: float, reason: str = "") -> None:
        """Close a position and record the trade.

        Args:
            ticker: Ticker to close.
            exit_price: Exit price.
            reason: Reason for closing (STOP_LOSS, MANUAL, etc.).
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
        })
        self._save()
        logger.info("Closed %s: P/L=¥%.0f (%.2f%%) [%s]", ticker, pnl, pnl_pct * 100, reason)

    def update_prices(self) -> None:
        """Refresh current prices for all open positions.

        Price lookup order: fast_info.lastPrice -> previousClose -> info.currentPrice
        -> regularMarketPrice -> previousClose. Fails silently per position; the
        last known price is retained on error.
        """
        import yfinance as yf

        for ticker, pos in self.positions.items():
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

    def summary_df(self) -> pd.DataFrame:
        """Return positions as a DataFrame for display."""
        if not self.positions:
            return pd.DataFrame()
        rows = [asdict(p) for p in self.positions.values()]
        df = pd.DataFrame(rows)
        cols = ["ticker", "shares", "entry_price", "current_price", "stop_loss",
                "unrealized_pnl", "unrealized_pnl_pct", "sector", "strategy", "entry_date"]
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
