"""
Risk Management — position sizing and stop-loss logic.

Position sizing: risk 1% of total capital per trade.
Stop-loss: hard cap at 7% from entry, or ATR-based (whichever is tighter).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

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
        """
        if signal.signal_type != SignalType.BUY:
            raise ValueError(f"Position sizing only for BUY signals, got {signal.signal_type}")

        entry = signal.price
        hard_stop = entry * (1 - self.hard_stop_pct)
        strategy_stop = signal.stop_loss if signal.stop_loss is not None else hard_stop

        # Use tighter stop (higher price = less risk)
        stop_loss = max(strategy_stop, hard_stop)

        risk_amount = self.total_capital * self.risk_per_trade
        risk_per_share = entry - stop_loss

        if risk_per_share <= 0:
            logger.warning(
                "%s: risk_per_share <= 0 (entry=%.2f, sl=%.2f). Using 1 share.",
                signal.ticker, entry, stop_loss,
            )
            shares = 1
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
    ) -> Optional[str]:
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
