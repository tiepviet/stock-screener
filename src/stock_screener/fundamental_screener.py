"""
Fundamental Screener — filter stocks by financial metrics.

Supports flexible condition-based filtering: ROE, P/E, P/B, EPS growth,
dividend yield, market cap. Pluggable via data_loader BaseDataLoader.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from .data_loader import BaseDataLoader, YFinanceDataLoader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Condition model
# ---------------------------------------------------------------------------

@dataclass
class Condition:
    """A single filter rule applied to a fundamental metric.

    Attributes:
        metric: Key name from fundamentals dict (e.g. 'roe', 'pe').
        operator: Comparison operator string: '>', '>=', '<', '<=', '==', '!='.
        value: Threshold value to compare against.
    """

    _OPS: ClassVar[dict[str, Callable[[float, float], bool]]] = {
        ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
    }

    metric: str
    operator: str
    value: float

    def evaluate(self, data: dict) -> bool:
        """Check if a fundamentals dict passes this condition.

        Args:
            data: Dict of fundamental metrics (from data_loader).

        Returns:
            True if condition passes. Missing values fail the check
            (strict — prevents stocks with no fundamental data from passing).
        """
        val = data.get(self.metric)
        if val is None:
            return False
        op_func = self._OPS.get(self.operator)
        if op_func is None:
            logger.warning("Unknown operator '%s', condition fails", self.operator)
            return False
        try:
            return op_func(float(val), self.value)
        except (TypeError, ValueError):
            return False


# ---------------------------------------------------------------------------
# Screener
# ---------------------------------------------------------------------------

class FundamentalScreener:
    """Screen a universe of stocks against a list of fundamental conditions.

    Usage:
        screener = FundamentalScreener(loader)
        results = screener.screen(["7203", "6758", "9984"], conditions)
    """

    def __init__(self, loader: BaseDataLoader | None = None) -> None:
        """Initialize screener.

        Args:
            loader: Data loader instance. Defaults to YFinanceDataLoader.
        """
        self.loader = loader or YFinanceDataLoader()

    def screen(
        self,
        tickers: list[str],
        conditions: list[Condition],
    ) -> pd.DataFrame:
        """Screen tickers against all conditions.

        Fetches fundamentals for each ticker and applies every condition.
        Tickers must pass ALL conditions (AND logic).

        Args:
            tickers: List of raw ticker strings.
            conditions: List of Condition objects.

        Returns:
            DataFrame with one row per passing ticker, plus all fundamental columns.
            Empty DataFrame if nothing passes.
        """
        if not tickers:
            logger.warning("Empty ticker list, returning empty DataFrame")
            return pd.DataFrame()

        if not conditions:
            logger.warning("No conditions specified, returning all fundamentals")

        # Batch fetch all fundamentals (uses cache)
        all_fundies = self.loader.fetch_batch_fundamentals(tickers)

        rows: list[dict] = []

        for ticker in tickers:
            fundies = all_fundies.get(ticker, {})
            fundies["ticker"] = ticker

            if all(c.evaluate(fundies) for c in conditions):
                rows.append(fundies)
                logger.info("PASS: %s", ticker)
            else:
                logger.debug("FAIL: %s", ticker)

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        cols = ["ticker"] + [c for c in df.columns if c != "ticker"]
        return df[cols]

    @staticmethod
    def from_dict(conditions: list[dict]) -> list[Condition]:
        """Convert raw dicts to Condition objects.

        Args:
            conditions: List of dicts with keys 'metric', 'operator', 'value'.

        Returns:
            List of Condition instances.

        Example:
            [{"metric": "roe", "operator": ">", "value": 15}]
        """
        return [Condition(**c) for c in conditions]


# ---------------------------------------------------------------------------
# Predefined condition sets
# ---------------------------------------------------------------------------

def default_japan_value_conditions() -> list[Condition]:
    """Conservative value screen for TSE stocks.

    ROE > 10%, P/E < 20, P/B < 3, positive EPS, dividend yield > 1%.
    """
    return [
        Condition("roe", ">", 0.10),
        Condition("pe", "<", 20.0),
        Condition("pb", "<", 3.0),
        Condition("eps", ">", 0),
        Condition("dividend_yield", ">", 0.01),
    ]


def growth_conditions(
    min_roe: float = 0.15,
    max_pe: float = 25.0,
    min_dividend: float = 0.005,
) -> list[Condition]:
    """Growth-oriented screen."""
    return [
        Condition("roe", ">", min_roe),
        Condition("pe", "<", max_pe),
        Condition("dividend_yield", ">", min_dividend),
        Condition("eps", ">", 0),
    ]
