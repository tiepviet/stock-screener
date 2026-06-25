"""
Profit Target Calculator — compute target exit prices per position.

Lightweight helper: given an entry price and a desired % gain, compute
the exit price. Also produces a portfolio-level summary (total invested,
total target value, weighted target %, total expected profit).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TargetRow:
    """A single position with its target.

    Attributes:
        ticker: Stock ticker symbol.
        entry_price: Buy price per share.
        target_pct: Desired gain as percentage (e.g. 5.0 means +5%).
        shares: Optional share count for portfolio-level totals.
    """

    ticker: str
    entry_price: float
    target_pct: float = 5.0
    shares: int = 0

    def __post_init__(self) -> None:
        if self.entry_price < 0:
            raise ValueError(f"entry_price must be >= 0, got {self.entry_price}")
        if not -100 <= self.target_pct <= 1000:
            raise ValueError(f"target_pct out of range: {self.target_pct}")
        if self.shares < 0:
            raise ValueError(f"shares must be >= 0, got {self.shares}")

    @property
    def exit_price(self) -> float:
        """Computed target sell price = entry * (1 + target_pct/100)."""
        return round(self.entry_price * (1 + self.target_pct / 100), 2)

    @property
    def per_share_profit(self) -> float:
        return round(self.exit_price - self.entry_price, 2)

    @property
    def position_value(self) -> float:
        """Cost basis = entry_price * shares. 0 if shares unset."""
        return self.entry_price * self.shares

    @property
    def target_value(self) -> float:
        return self.exit_price * self.shares


@dataclass
class TargetSummary:
    """Aggregated totals across multiple rows."""

    rows: list[TargetRow] = field(default_factory=list)
    total_invested: float = 0.0
    total_target_value: float = 0.0
    total_profit: float = 0.0
    weighted_target_pct: float = 0.0
    position_count: int = 0

    def to_dict(self) -> dict:
        return {
            "total_invested": round(self.total_invested, 2),
            "total_target_value": round(self.total_target_value, 2),
            "total_profit": round(self.total_profit, 2),
            "weighted_target_pct": round(self.weighted_target_pct, 4),
            "position_count": self.position_count,
        }


def calculate_exit_price(entry: float, target_pct: float) -> float:
    """Compute target sell price for a single position.

    Args:
        entry: Buy price per share (must be > 0).
        target_pct: Desired gain as percent (e.g. 5.0 = +5%).

    Returns:
        Target exit price, rounded to 2 decimals.

    Raises:
        ValueError: If entry is non-positive.
    """
    if entry <= 0:
        raise ValueError(f"entry must be > 0, got {entry}")
    return round(entry * (1 + target_pct / 100), 2)


def summarize(rows: list[TargetRow]) -> TargetSummary:
    """Aggregate totals across rows. Rows with shares=0 contribute to
    weighted_target_pct only if entry_price > 0 (equal-weight fallback).

    Args:
        rows: List of TargetRow.

    Returns:
        TargetSummary with portfolio-level metrics.
    """
    valid = [r for r in rows if r.entry_price > 0]
    total_invested = sum(r.position_value for r in valid)
    total_target_value = sum(r.target_value for r in valid)
    total_profit = total_target_value - total_invested

    if total_invested > 0:
        # Money-weighted target %
        weighted = (total_target_value / total_invested - 1) * 100
    elif valid:
        # Equal-weight fallback when no shares specified
        weighted = sum(r.target_pct for r in valid) / len(valid)
    else:
        weighted = 0.0

    return TargetSummary(
        rows=list(rows),
        total_invested=total_invested,
        total_target_value=total_target_value,
        total_profit=total_profit,
        weighted_target_pct=weighted,
        position_count=len(valid),
    )
