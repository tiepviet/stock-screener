"""
Price Target Engine — predict entry/exit zones using technical analysis.

Computes:
  - Fibonacci retracement & extension levels
  - Support/Resistance from swing highs/lows
  - Risk/Reward ratio
  - Buy zone (optimal entry range)
  - Sell zone (take-profit range)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class FibonacciLevels:
    """Fibonacci retracement and extension levels."""

    swing_low: float
    swing_high: float
    retracements: dict[str, float] = field(default_factory=dict)
    extensions: dict[str, float] = field(default_factory=dict)

    @property
    def range(self) -> float:
        return self.swing_high - self.swing_low


@dataclass
class SupportResistance:
    """Support and resistance levels."""

    supports: list[float] = field(default_factory=list)
    resistances: list[float] = field(default_factory=list)


@dataclass
class RiskReward:
    """Risk/Reward calculation for a trade."""

    entry: float
    stop_loss: float
    target: float
    risk: float = 0.0
    reward: float = 0.0
    ratio: float = 0.0

    def __post_init__(self) -> None:
        self.risk = abs(self.entry - self.stop_loss)
        self.reward = abs(self.target - self.entry)
        self.ratio = round(self.reward / self.risk, 2) if self.risk > 0 else 0.0


@dataclass
class BuyZone:
    """Suggested buy zone for a stock."""

    ticker: str
    current_price: float
    zone_low: float
    zone_high: float
    support_levels: list[float] = field(default_factory=list)
    entry_suggestion: float = 0.0
    reasoning: str = ""

    def __post_init__(self) -> None:
        if self.entry_suggestion == 0.0:
            self.entry_suggestion = round((self.zone_low + self.zone_high) / 2, 2)


@dataclass
class SellZone:
    """Suggested sell zone for a stock."""

    ticker: str
    current_price: float
    zone_low: float
    zone_high: float
    resistance_levels: list[float] = field(default_factory=list)
    exit_suggestion: float = 0.0
    take_profits: list[float] = field(default_factory=list)
    reasoning: str = ""

    def __post_init__(self) -> None:
        if self.exit_suggestion == 0.0:
            self.exit_suggestion = round((self.zone_low + self.zone_high) / 2, 2)


@dataclass
class PriceTargets:
    """Combined price targets for a single ticker."""

    ticker: str
    current_price: float
    buy_zone: BuyZone | None = None
    sell_zone: SellZone | None = None
    fibonacci: FibonacciLevels | None = None
    sr: SupportResistance | None = None
    risk_reward: RiskReward | None = None
    take_profits: list[float] = field(default_factory=list)
    stop_loss: float | None = None
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Price Target Engine
# ---------------------------------------------------------------------------


class PriceTargetEngine:
    """Compute price targets from OHLCV data.

    Uses:
      - Swing high/low detection for support/resistance
      - Fibonacci retracement & extensions
      - ATR-based volatility for zones
      - R:R ratio optimization
    """

    def __init__(
        self,
        swing_lookback: int = 20,
        atr_period: int = 14,
        atr_mult: float = 1.5,
    ) -> None:
        """Initialize engine.

        Args:
            swing_lookback: Bars to look back for swing high/low detection.
            atr_period: ATR period for volatility-based zones.
            atr_mult: Multiplier for ATR to compute zone width.
        """
        self.swing_lookback = swing_lookback
        self.atr_period = atr_period
        self.atr_mult = atr_mult

    def compute_all(
        self,
        df: pd.DataFrame,
        ticker: str,
        entry_price: float | None = None,
        stop_loss: float | None = None,
    ) -> PriceTargets:
        """Compute all price targets for a ticker.

        Args:
            df: OHLCV DataFrame with technical indicators (enriched).
            ticker: Stock ticker.
            entry_price: Optional entry price (defaults to last Close).
            stop_loss: Optional stop-loss price.

        Returns:
            PriceTargets with all computed levels.
        """
        if df is None or df.empty or len(df) < self.swing_lookback + 1:
            return PriceTargets(
                ticker=ticker,
                current_price=0.0,
                reasoning="Insufficient data",
            )

        last_close = float(df["Close"].iloc[-1])
        entry = entry_price or last_close

        # Compute components
        sr = self._find_support_resistance(df)
        fib = self._compute_fibonacci(df)
        atr = self._get_atr(df)

        # Determine zones
        buy_zone = self._compute_buy_zone(
            ticker, last_close, entry, sr, fib, atr
        )
        sell_zone = self._compute_sell_zone(
            ticker, last_close, entry, sr, fib, atr
        )

        # R:R
        rr = None
        if stop_loss and sell_zone:
            rr = RiskReward(
                entry=entry,
                stop_loss=stop_loss,
                target=sell_zone.exit_suggestion,
            )

        # Take-profit levels
        take_profits = self._compute_take_profits(entry, fib, sr, atr)

        return PriceTargets(
            ticker=ticker,
            current_price=last_close,
            buy_zone=buy_zone,
            sell_zone=sell_zone,
            fibonacci=fib,
            sr=sr,
            risk_reward=rr,
            take_profits=take_profits,
            stop_loss=stop_loss,
        )

    # --- Support/Resistance ---

    def _find_support_resistance(self, df: pd.DataFrame) -> SupportResistance:
        """Find support and resistance from swing highs/lows.

        Swing high: High > High of N bars on each side.
        Swing low: Low < Low of N bars on each side.
        """
        highs = df["High"].values
        lows = df["Low"].values
        n = self.swing_lookback

        swing_highs: list[float] = []
        swing_lows: list[float] = []

        for i in range(n, len(df) - n):
            # Swing high
            window_high = highs[i - n : i + n + 1]
            if highs[i] == window_high.max() and highs[i] > highs[i - 1]:
                swing_highs.append(float(highs[i]))

            # Swing low
            window_low = lows[i - n : i + n + 1]
            if lows[i] == window_low.min() and lows[i] < lows[i - 1]:
                swing_lows.append(float(lows[i]))

        # Cluster nearby levels (within 2% of each other)
        resistance_levels = self._cluster_levels(swing_highs, threshold_pct=0.02)
        support_levels = self._cluster_levels(swing_lows, threshold_pct=0.02)

        # Sort
        support_levels.sort(reverse=True)  # Highest support first
        resistance_levels.sort()  # Lowest resistance first

        return SupportResistance(
            supports=support_levels[:5],  # Top 5
            resistances=resistance_levels[:5],  # Top 5
        )

    @staticmethod
    def _cluster_levels(levels: list[float], threshold_pct: float = 0.02) -> list[float]:
        """Cluster nearby price levels into single levels."""
        if not levels:
            return []

        sorted_levels = sorted(levels)
        clustered: list[float] = []
        cluster: list[float] = [sorted_levels[0]]

        for i in range(1, len(sorted_levels)):
            if (sorted_levels[i] - cluster[0]) / cluster[0] <= threshold_pct:
                cluster.append(sorted_levels[i])
            else:
                clustered.append(round(sum(cluster) / len(cluster), 2))
                cluster = [sorted_levels[i]]
        clustered.append(round(sum(cluster) / len(cluster), 2))

        return clustered

    # --- Fibonacci ---

    def _compute_fibonacci(self, df: pd.DataFrame) -> FibonacciLevels:
        """Compute Fibonacci retracement and extension levels.

        Uses the most recent swing high and swing low within lookback window.
        """
        recent = df.tail(self.swing_lookback * 3)

        swing_low = float(recent["Low"].min())
        swing_high = float(recent["High"].max())

        fib = FibonacciLevels(swing_low=swing_low, swing_high=swing_high)

        # Retracement levels (from high, pullback toward low)
        retrace_pcts = {
            "23.6%": 0.236,
            "38.2%": 0.382,
            "50.0%": 0.500,
            "61.8%": 0.618,
            "78.6%": 0.786,
        }
        for label, pct in retrace_pcts.items():
            fib.retracements[label] = round(
                swing_high - (swing_high - swing_low) * pct, 2
            )

        # Extension levels (beyond swing high)
        extension_pcts = {
            "127.2%": 1.272,
            "161.8%": 1.618,
            "200.0%": 2.000,
            "261.8%": 2.618,
        }
        for label, pct in extension_pcts.items():
            fib.extensions[label] = round(
                swing_low + (swing_high - swing_low) * pct, 2
            )

        return fib

    # --- ATR helper ---

    def _get_atr(self, df: pd.DataFrame) -> float:
        """Get current ATR value."""
        atr_col = f"ATR_{self.atr_period}"
        if atr_col in df.columns:
            val = df[atr_col].iloc[-1]
            if not pd.isna(val):
                return float(val)
        return 0.0

    # --- Buy Zone ---

    def _compute_buy_zone(
        self,
        ticker: str,
        current_price: float,
        entry: float,
        sr: SupportResistance,
        fib: FibonacciLevels,
        atr: float,
    ) -> BuyZone:
        """Compute optimal buy zone.

        Strategy:
        1. Find nearest support levels below current price
        2. Use Fibonacci retracement levels as additional support
        3. Zone = [lowest support, highest support below current]
        """
        # Collect all potential supports below current price
        all_supports: list[float] = []

        # From S/R
        for s in sr.supports:
            if s < current_price:
                all_supports.append(s)

        # From Fibonacci retracements
        for _label, level in fib.retracements.items():
            if level < current_price:
                all_supports.append(level)

        if not all_supports:
            # Fallback: use ATR-based zone below current
            if atr > 0:
                zone_low = round(current_price - self.atr_mult * atr, 2)
                zone_high = round(current_price - 0.5 * atr, 2)
            else:
                zone_low = round(current_price * 0.95, 2)
                zone_high = round(current_price * 0.98, 2)
            return BuyZone(
                ticker=ticker,
                current_price=current_price,
                zone_low=zone_low,
                zone_high=zone_high,
                reasoning="No support found — using ATR-based zone",
            )

        # Zone = nearest supports
        all_supports.sort(reverse=True)
        zone_high = all_supports[0]  # Nearest support
        zone_low = all_supports[min(2, len(all_supports) - 1)]  # 3rd support

        return BuyZone(
            ticker=ticker,
            current_price=current_price,
            zone_low=zone_low,
            zone_high=zone_high,
            support_levels=all_supports[:5],
            reasoning=f"Buy zone at support levels (nearest: {zone_high:.0f})",
        )

    # --- Sell Zone ---

    def _compute_sell_zone(
        self,
        ticker: str,
        current_price: float,
        entry: float,
        sr: SupportResistance,
        fib: FibonacciLevels,
        atr: float,
    ) -> SellZone:
        """Compute optimal sell zone.

        Strategy:
        1. Find nearest resistance levels above current price
        2. Use Fibonacci extension levels as additional targets
        3. Zone = [lowest resistance above current, highest target]
        """
        all_resistances: list[float] = []

        # From S/R
        for r in sr.resistances:
            if r > current_price:
                all_resistances.append(r)

        # From Fibonacci extensions
        for _label, level in fib.extensions.items():
            if level > current_price:
                all_resistances.append(level)

        # From Fibonacci retracements (if price is below them)
        for _label, level in fib.retracements.items():
            if level > current_price:
                all_resistances.append(level)

        if not all_resistances:
            # Fallback: ATR-based zone above current
            if atr > 0:
                zone_low = round(current_price + 0.5 * atr, 2)
                zone_high = round(current_price + self.atr_mult * atr, 2)
            else:
                zone_low = round(current_price * 1.02, 2)
                zone_high = round(current_price * 1.05, 2)
            return SellZone(
                ticker=ticker,
                current_price=current_price,
                zone_low=zone_low,
                zone_high=zone_high,
                reasoning="No resistance found — using ATR-based zone",
            )

        all_resistances.sort()
        zone_low = all_resistances[0]  # Nearest resistance
        zone_high = all_resistances[min(2, len(all_resistances) - 1)]  # 3rd

        # Take-profit levels = all resistances
        take_profits = all_resistances[:5]

        return SellZone(
            ticker=ticker,
            current_price=current_price,
            zone_low=zone_low,
            zone_high=zone_high,
            resistance_levels=all_resistances[:5],
            take_profits=take_profits,
            reasoning=f"Sell zone at resistance levels (nearest: {zone_low:.0f})",
        )

    # --- Take-Profit Levels ---

    def _compute_take_profits(
        self,
        entry: float,
        fib: FibonacciLevels,
        sr: SupportResistance,
        atr: float,
    ) -> list[float]:
        """Compute multi-level take-profit targets.

        Returns sorted list of take-profit prices.
        """
        targets: list[float] = []

        # From Fibonacci extensions
        for _label, level in fib.extensions.items():
            if level > entry:
                targets.append(level)

        # From resistance levels
        for r in sr.resistances:
            if r > entry:
                targets.append(r)

        # Remove duplicates and sort
        targets = sorted(set(targets))

        # If no targets, use ATR-based defaults
        if not targets and atr > 0:
            targets = [
                round(entry + atr, 2),
                round(entry + 2 * atr, 2),
                round(entry + 3 * atr, 2),
            ]

        return targets[:5]
