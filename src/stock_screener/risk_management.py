"""
Risk Management — position sizing and stop-loss logic.

Position sizing: risk 1% of total capital per trade.
Stop-loss: hard cap at 7% from entry, or ATR-based (whichever is tighter).
Trailing stop: moves up as price increases to lock in profits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .technical_engine import Signal, SignalType

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_RISK_PER_TRADE = 0.01   # 1% of total capital
DEFAULT_HARD_STOP_PCT = 0.07    # 7% max loss


@dataclass
class PositionPlan:
    """Output of risk calculation for a single trade."""

    ticker: str
    entry_price: float
    stop_loss: float
    shares: int
    position_value: float
    risk_amount: float
    risk_pct: float
    strategy: str

    def __str__(self) -> str:
        return (
            f"{self.ticker}: {self.shares} shares @ {self.entry_price:.2f} "
            f"| SL={self.stop_loss:.2f} | Risk=¥{self.risk_amount:,.0f} "
            f"({self.risk_pct:.1%}) | Value=¥{self.position_value:,.0f}"
        )


class RiskManager:
    """Calculate position sizes and enforce stop-loss rules.

    Rules:
      - Risk exactly 1% of total capital per trade.
      - Stop-loss is the tighter of: ATR-based SL or 7% hard cap.
      - Minimum 1 share (no fractional shares for TSE).
    """

    def __init__(
        self,
        total_capital: float,
        risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
        hard_stop_pct: float = DEFAULT_HARD_STOP_PCT,
    ) -> None:
        """Initialize risk manager.

        Args:
            total_capital: Total portfolio capital in JPY.
            risk_per_trade: Fraction of capital to risk per trade (default 0.01 = 1%).
            hard_stop_pct: Maximum loss fraction before hard stop (default 0.07 = 7%).
        """
        if total_capital <= 0:
            raise ValueError("total_capital must be positive")
        if not 0 < risk_per_trade < 1:
            raise ValueError("risk_per_trade must be between 0 and 1")
        if not 0 < hard_stop_pct < 1:
            raise ValueError("hard_stop_pct must be between 0 and 1")

        self.total_capital = total_capital
        self.risk_per_trade = risk_per_trade
        self.hard_stop_pct = hard_stop_pct

    def calculate_position(self, signal: Signal) -> PositionPlan:
        """Compute position size for a BUY signal.

        Args:
            signal: BUY signal from a strategy.

        Returns:
            PositionPlan with shares, stop-loss, and risk amounts.

        Raises:
            ValueError: If signal is not BUY, or entry price is non-positive.
        """
        if signal.signal_type != SignalType.BUY:
            raise ValueError(f"Position sizing only for BUY signals, got {signal.signal_type}")
        if signal.price <= 0:
            raise ValueError(f"Signal price must be positive, got {signal.price}")

        entry = signal.price
        hard_stop = entry * (1 - self.hard_stop_pct)
        strategy_stop = signal.stop_loss if signal.stop_loss is not None else hard_stop

        # Use tighter stop (higher price = less risk)
        stop_loss = max(strategy_stop, hard_stop)

        risk_amount = self.total_capital * self.risk_per_trade
        risk_per_share = entry - stop_loss

        if risk_per_share <= 0:
            logger.warning(
                "%s: risk_per_share <= 0 (entry=%.2f, sl=%.2f). Skipping position.",
                signal.ticker, entry, stop_loss,
            )
            return PositionPlan(
                ticker=signal.ticker,
                entry_price=entry,
                stop_loss=round(stop_loss, 2),
                shares=0,
                position_value=0.0,
                risk_amount=0.0,
                risk_pct=0.0,
                strategy=signal.strategy,
            )
        else:
            shares = max(1, int(risk_amount / risk_per_share))

        position_value = shares * entry
        actual_risk = shares * risk_per_share
        risk_pct = actual_risk / self.total_capital if self.total_capital > 0 else 0

        plan = PositionPlan(
            ticker=signal.ticker,
            entry_price=entry,
            stop_loss=round(stop_loss, 2),
            shares=shares,
            position_value=round(position_value, 2),
            risk_amount=round(actual_risk, 2),
            risk_pct=round(risk_pct, 4),
            strategy=signal.strategy,
        )
        logger.info("Position: %s", plan)
        return plan

    def batch_positions(self, signals: list[Signal]) -> list[PositionPlan]:
        """Calculate positions for multiple BUY signals.

        Respects total capital — stops when capital allocation would exceed limit.

        Args:
            signals: List of BUY signals.

        Returns:
            List of PositionPlan objects.
        """
        plans: list[PositionPlan] = []
        allocated = 0.0

        for sig in sorted(signals, key=lambda s: s.price):
            plan = self.calculate_position(sig)
            if plan.shares == 0:
                continue
            if allocated + plan.position_value > self.total_capital:
                logger.warning(
                    "Skipping %s: would exceed capital (allocated=%.0f, need=%.0f)",
                    plan.ticker, allocated, plan.position_value,
                )
                continue
            allocated += plan.position_value
            plans.append(plan)

        return plans

    def check_stop_loss(
        self,
        position: PositionPlan,
        current_price: float,
    ) -> str | None:
        """Check if current price triggers stop-loss.

        Args:
            position: Active position.
            current_price: Latest market price.

        Returns:
            'STOP_LOSS' if triggered, None otherwise.
        """
        if current_price <= position.stop_loss:
            logger.warning(
                "STOP LOSS TRIGGERED: %s @ %.2f (SL=%.2f)",
                position.ticker, current_price, position.stop_loss,
            )
            return "STOP_LOSS"
        return None


# ---------------------------------------------------------------------------
# Trailing Stop
# ---------------------------------------------------------------------------


@dataclass
class TrailingStopState:
    """Tracks trailing stop state for a single position."""

    ticker: str
    entry_price: float
    highest_price: float
    current_stop: float
    trail_pct: float

    def update(self, current_price: float) -> float:
        """Update trailing stop based on current price.

        If price rises, stop moves up. If price falls, stop stays.

        Args:
            current_price: Latest market price.

        Returns:
            Updated stop loss price.
        """
        if current_price > self.highest_price:
            self.highest_price = current_price
            new_stop = round(current_price * (1 - self.trail_pct), 2)
            if new_stop > self.current_stop:
                self.current_stop = new_stop
                logger.info(
                    "%s: Trailing stop updated to %.2f (highest=%.2f)",
                    self.ticker, self.current_stop, self.highest_price,
                )
        return self.current_stop

    def check_exit(self, current_price: float) -> str | None:
        """Check if current price triggers trailing stop exit.

        Args:
            current_price: Latest market price.

        Returns:
            'TRAILING_STOP' if triggered, None otherwise.
        """
        if current_price <= self.current_stop:
            logger.warning(
                "TRAILING STOP TRIGGERED: %s @ %.2f (SL=%.2f, highest=%.2f)",
                self.ticker, current_price, self.current_stop, self.highest_price,
            )
            return "TRAILING_STOP"
        return None


class TrailingStopManager:
    """Manage trailing stops for multiple positions.

    Usage:
        manager = TrailingStopManager(trail_pct=0.05)
        manager.open("7203", entry_price=1000, initial_stop=930)
        # ... on each price update ...
        manager.update("7203", current_price=1050)
        exit_signal = manager.check_exit("7203", current_price=1020)
    """

    def __init__(self, trail_pct: float = 0.05) -> None:
        """Initialize trailing stop manager.

        Args:
            trail_pct: Trailing stop percentage (0.05 = 5% below highest price).
        """
        if not 0 < trail_pct < 1:
            raise ValueError("trail_pct must be between 0 and 1")
        self.trail_pct = trail_pct
        self._positions: dict[str, TrailingStopState] = {}

    def open(self, ticker: str, entry_price: float, initial_stop: float | None = None) -> None:
        """Register a new position for trailing stop tracking.

        Args:
            ticker: Stock ticker.
            entry_price: Entry price.
            initial_stop: Initial stop loss (defaults to entry * (1 - trail_pct)).
        """
        if initial_stop is None:
            initial_stop = round(entry_price * (1 - self.trail_pct), 2)
        self._positions[ticker] = TrailingStopState(
            ticker=ticker,
            entry_price=entry_price,
            highest_price=entry_price,
            current_stop=initial_stop,
            trail_pct=self.trail_pct,
        )
        logger.info("%s: Trailing stop opened @ %.2f, SL=%.2f", ticker, entry_price, initial_stop)

    def update(self, ticker: str, current_price: float) -> float | None:
        """Update trailing stop for a position.

        Args:
            ticker: Stock ticker.
            current_price: Latest market price.

        Returns:
            Updated stop loss, or None if position not tracked.
        """
        pos = self._positions.get(ticker)
        if pos is None:
            return None
        return pos.update(current_price)

    def check_exit(self, ticker: str, current_price: float) -> str | None:
        """Check if trailing stop is triggered.

        Args:
            ticker: Stock ticker.
            current_price: Latest market price.

        Returns:
            'TRAILING_STOP' if triggered, None otherwise.
        """
        pos = self._positions.get(ticker)
        if pos is None:
            return None
        return pos.check_exit(current_price)

    def close(self, ticker: str) -> TrailingStopState | None:
        """Close and remove a position from tracking.

        Args:
            ticker: Stock ticker.

        Returns:
            Final TrailingStopState or None if not found.
        """
        return self._positions.pop(ticker, None)

    def get_state(self, ticker: str) -> TrailingStopState | None:
        """Get current trailing stop state for a position."""
        return self._positions.get(ticker)

    @property
    def active_positions(self) -> list[str]:
        """List of tickers with active trailing stops."""
        return list(self._positions.keys())
