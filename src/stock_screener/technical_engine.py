"""
Technical Analysis Engine — indicator calculation and strategy signals.

Uses pandas_ta for indicators. Provides two built-in strategies:
  - VolumeBreakoutStrategy: price breakout on volume surge.
  - PullbackMAStrategy: buy pullback to MA20/MA50 in uptrend (price > MA200).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import pandas as pd
import pandas_ta_classic as ta

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal model
# ---------------------------------------------------------------------------

class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Signal:
    """Represents a trading signal emitted by a strategy."""

    ticker: str
    signal_type: SignalType
    strategy: str
    date: datetime
    price: float
    stop_loss: float | None = None
    metadata: dict = field(default_factory=dict)

    def __str__(self) -> str:
        sl = f" SL={self.stop_loss:.2f}" if self.stop_loss else ""
        return (
            f"[{self.signal_type.value}] {self.ticker} "
            f"@ {self.price:.2f} on {self.date.date()} "
            f"({self.strategy}){sl}"
        )


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------

class TechnicalEngine:
    """Compute common technical indicators on an OHLCV DataFrame.

    All methods return a new DataFrame. Use enrich() for single-pass computation.
    """

    @staticmethod
    def add_moving_averages(
        df: pd.DataFrame,
        periods: tuple[int, ...] = (20, 50, 200),
    ) -> pd.DataFrame:
        """Add simple moving averages (SMA).

        Args:
            df: OHLCV DataFrame.
            periods: Tuple of look-back periods.

        Returns:
            DataFrame with added 'SMA_<period>' columns.
        """
        out = df.copy()
        for p in periods:
            out[f"SMA_{p}"] = ta.sma(out["Close"], length=p)
        return out

    @staticmethod
    def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """Add RSI column."""
        out = df.copy()
        out[f"RSI_{period}"] = ta.rsi(out["Close"], length=period)
        return out

    @staticmethod
    def add_bollinger_bands(
        df: pd.DataFrame, period: int = 20, std: float = 2.0
    ) -> pd.DataFrame:
        """Add Bollinger Bands (BBM, BBH, BBL)."""
        out = df.copy()
        bb = ta.bbands(out["Close"], length=period, std=std)
        if bb is not None:
            out = pd.concat([out, bb], axis=1)
        return out

    @staticmethod
    def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """Add Average True Range."""
        out = df.copy()
        out[f"ATR_{period}"] = ta.atr(
            out["High"], out["Low"], out["Close"], length=period
        )
        return out

    @staticmethod
    def add_volume_sma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
        """Add volume SMA for breakout comparison."""
        out = df.copy()
        out[f"VOL_SMA_{period}"] = ta.sma(out["Volume"], length=period)
        return out

    def enrich(
        self,
        df: pd.DataFrame,
        sma_periods: tuple[int, ...] = (20, 50, 200),
        rsi_period: int = 14,
        atr_period: int = 14,
        vol_sma_period: int = 20,
    ) -> pd.DataFrame:
        """Add all standard indicators in a single pass (1 copy only).

        Args:
            df: Raw OHLCV DataFrame.
            sma_periods: SMA periods to compute.
            rsi_period: RSI look-back.
            atr_period: ATR look-back.
            vol_sma_period: Volume SMA look-back.

        Returns:
            Enriched DataFrame.
        """
        out = df.copy()  # Single copy

        # SMA
        for p in sma_periods:
            out[f"SMA_{p}"] = ta.sma(out["Close"], length=p)

        # RSI
        out[f"RSI_{rsi_period}"] = ta.rsi(out["Close"], length=rsi_period)

        # ATR
        out[f"ATR_{atr_period}"] = ta.atr(
            out["High"], out["Low"], out["Close"], length=atr_period
        )

        # Volume SMA
        out[f"VOL_SMA_{vol_sma_period}"] = ta.sma(out["Volume"], length=vol_sma_period)

        return out


# ---------------------------------------------------------------------------
# Strategy base + implementations
# ---------------------------------------------------------------------------

class BaseStrategy(ABC):
    """Abstract base for signal-generating strategies."""

    name: str = "BaseStrategy"

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame, ticker: str) -> list[Signal]:
        """Scan enriched OHLCV and return list of Signals.

        Args:
            df: OHLCV with technical indicators pre-computed.
            ticker: Raw ticker for labeling.

        Returns:
            List of Signal objects (may be empty).
        """
        ...


class VolumeBreakoutStrategy(BaseStrategy):
    """Detect price breakout above recent range on volume surge.

    Conditions:
      1. Close > highest High of previous N bars (default 20).
      2. Volume > multiplier * SMA(volume, 20) (default multiplier=1.2).
      3. Close > SMA_20 (trend filter).
    """

    name = "VolumeBreakout"

    def __init__(
        self,
        lookback: int = 20,
        volume_mult: float = 1.2,
        vol_sma_period: int = 20,
    ) -> None:
        self.lookback = lookback
        self.volume_mult = volume_mult
        self.vol_sma_period = vol_sma_period

    def generate_signals(self, df: pd.DataFrame, ticker: str) -> list[Signal]:
        """Scan for breakout signals.

        Args:
            df: Enriched OHLCV DataFrame.
            ticker: Raw ticker.

        Returns:
            List of BUY signals on breakout days.
        """
        signals: list[Signal] = []
        vol_sma_col = f"VOL_SMA_{self.vol_sma_period}"

        required = {"Close", "High", "Volume", "SMA_20", vol_sma_col}
        if not required.issubset(df.columns):
            logger.debug("VolumeBreakout skip %s: missing %s", ticker, required - set(df.columns))
            return signals
        if df.empty or len(df) < self.lookback + 1:
            logger.debug("VolumeBreakout skip %s: insufficient rows (%d)", ticker, len(df))
            return signals

        highs = df["High"]
        closes = df["Close"]
        volumes = df["Volume"]
        vol_sma = df[vol_sma_col]

        for i in range(self.lookback, len(df)):
            row = df.iloc[i]
            # 1. Price breakout: close > highest high of prior N bars
            prev_high = highs.iloc[i - self.lookback : i].max()
            if closes.iloc[i] <= prev_high:
                continue
            # 2. Volume surge
            if pd.isna(vol_sma.iloc[i]) or vol_sma.iloc[i] == 0:
                continue
            if volumes.iloc[i] < self.volume_mult * vol_sma.iloc[i]:
                continue
            # 3. Trend filter: above SMA_20
            if pd.isna(row.get("SMA_20")) or closes.iloc[i] < row["SMA_20"]:
                continue

            price = float(closes.iloc[i])
            atr_raw = row.get("ATR_14")
            atr_val = float(atr_raw) if atr_raw is not None and not pd.isna(atr_raw) else 0.0
            sl = round(price - 2 * atr_val, 2) if atr_val > 0 else None

            signals.append(
                Signal(
                    ticker=ticker,
                    signal_type=SignalType.BUY,
                    strategy=self.name,
                    date=df.index[i].to_pydatetime() if hasattr(df.index[i], "to_pydatetime") else df.index[i],
                    price=price,
                    stop_loss=sl,
                    metadata={
                        "prev_high": float(prev_high),
                        "volume_ratio": round(volumes.iloc[i] / vol_sma.iloc[i], 2),
                    },
                )
            )
        return signals


class PullbackMAStrategy(BaseStrategy):
    """Buy pullback to MA20/MA50 in a confirmed uptrend (price > MA200).

    Conditions:
      1. Close > SMA_200 (long-term uptrend).
      2. Previous Close < SMA_20 (or SMA_50) — was below.
      3. Current Close >= SMA_20 (or SMA_50) — crossed back above.
      4. RSI < 60 (not overbought).
    """

    name = "PullbackMA"

    def __init__(
        self,
        ma_periods: tuple[int, ...] = (20, 50),
        trend_ma: int = 200,
        rsi_max: float = 60.0,
    ) -> None:
        self.ma_periods = ma_periods
        self.trend_ma = trend_ma
        self.rsi_max = rsi_max

    def generate_signals(self, df: pd.DataFrame, ticker: str) -> list[Signal]:
        """Scan for pullback-to-MA signals.

        Args:
            df: Enriched OHLCV DataFrame.
            ticker: Raw ticker.

        Returns:
            List of BUY signals on pullback recovery days.
        """
        signals: list[Signal] = []
        trend_col = f"SMA_{self.trend_ma}"
        rsi_col = "RSI_14"

        required = {"Close", trend_col, rsi_col}
        if not required.issubset(df.columns):
            logger.debug("PullbackMA skip %s: missing %s", ticker, required - set(df.columns))
            return signals
        if df.empty or len(df) < 2:
            logger.debug("PullbackMA skip %s: insufficient rows (%d)", ticker, len(df))
            return signals

        for i in range(1, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]

            # Trend filter
            if pd.isna(row.get(trend_col)) or row["Close"] < row[trend_col]:
                continue
            # RSI filter
            if pd.isna(row.get(rsi_col)) or row[rsi_col] > self.rsi_max:
                continue

            for ma_p in self.ma_periods:
                ma_col = f"SMA_{ma_p}"
                if ma_col not in df.columns:
                    continue
                if pd.isna(row.get(ma_col)) or pd.isna(prev.get(ma_col)):
                    continue

                prev_close = prev["Close"]
                curr_close = row["Close"]
                prev_ma = prev[ma_col]
                curr_ma = row[ma_col]

                # Crossed above MA
                if prev_close < prev_ma and curr_close >= curr_ma:
                    price = float(curr_close)
                    atr_raw = row.get("ATR_14")
                    atr_val = float(atr_raw) if atr_raw is not None and not pd.isna(atr_raw) else 0.0
                    sl = round(price - 2 * atr_val, 2) if atr_val > 0 else None

                    signals.append(
                        Signal(
                            ticker=ticker,
                            signal_type=SignalType.BUY,
                            strategy=self.name,
                            date=df.index[i].to_pydatetime() if hasattr(df.index[i], "to_pydatetime") else df.index[i],
                            price=price,
                            stop_loss=sl,
                            metadata={
                                "ma_period": ma_p,
                                "trend_ma": self.trend_ma,
                                "rsi": round(float(row[rsi_col]), 2),
                            },
                        )
                    )
                    break  # one signal per bar
        return signals


# ---------------------------------------------------------------------------
# Sell strategies
# ---------------------------------------------------------------------------


class TrendBreakdownSellStrategy(BaseStrategy):
    """Sell when price breaks down below key MA support on volume.

    Conditions:
      1. Previous Close >= SMA_20 (was above support).
      2. Current Close < SMA_20 (broke below support).
      3. Close < SMA_50 (confirms weakness beyond short-term).
      4. Volume > volume_mult * SMA(Volume, 20) (selling pressure).
    """

    name = "TrendBreakdown"

    def __init__(
        self,
        breakdown_ma: int = 20,
        confirm_ma: int = 50,
        volume_mult: float = 1.2,
        vol_sma_period: int = 20,
    ) -> None:
        self.breakdown_ma = breakdown_ma
        self.confirm_ma = confirm_ma
        self.volume_mult = volume_mult
        self.vol_sma_period = vol_sma_period

    def generate_signals(self, df: pd.DataFrame, ticker: str) -> list[Signal]:
        signals: list[Signal] = []
        breakdown_col = f"SMA_{self.breakdown_ma}"
        confirm_col = f"SMA_{self.confirm_ma}"
        vol_sma_col = f"VOL_SMA_{self.vol_sma_period}"

        required = {"Close", "Volume", breakdown_col, confirm_col, vol_sma_col}
        if not required.issubset(df.columns):
            logger.debug("TrendBreakdown skip %s: missing %s", ticker, required - set(df.columns))
            return signals
        if df.empty or len(df) < 2:
            logger.debug("TrendBreakdown skip %s: insufficient rows (%d)", ticker, len(df))
            return signals

        for i in range(1, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]

            prev_close = prev["Close"]
            curr_close = row["Close"]
            prev_ma = prev[breakdown_col]
            curr_ma = row[breakdown_col]
            confirm = row[confirm_col]

            if pd.isna(prev_ma) or pd.isna(curr_ma) or pd.isna(confirm):
                continue

            # 1. Was above breakdown MA, now below
            if not (prev_close >= prev_ma and curr_close < curr_ma):
                continue
            # 2. Below confirm MA
            if curr_close >= confirm:
                continue
            # 3. Volume surge
            vol_sma = row[vol_sma_col]
            if pd.isna(vol_sma) or vol_sma == 0:
                continue
            if row["Volume"] < self.volume_mult * vol_sma:
                continue

            price = float(curr_close)
            atr_raw = row.get("ATR_14")
            atr_val = float(atr_raw) if atr_raw is not None and not pd.isna(atr_raw) else 0.0
            # For sell: stop loss is ABOVE entry (cap loss if reversal)
            sl = round(price + 2 * atr_val, 2) if atr_val > 0 else None

            signals.append(
                Signal(
                    ticker=ticker,
                    signal_type=SignalType.SELL,
                    strategy=self.name,
                    date=df.index[i].to_pydatetime() if hasattr(df.index[i], "to_pydatetime") else df.index[i],
                    price=price,
                    stop_loss=sl,
                    metadata={
                        "breakdown_ma": self.breakdown_ma,
                        "confirm_ma": self.confirm_ma,
                        "volume_ratio": round(row["Volume"] / vol_sma, 2),
                    },
                )
            )
        return signals


class OverboughtReversalSellStrategy(BaseStrategy):
    """Sell when RSI is overbought and price shows reversal.

    Conditions:
      1. Previous RSI >= rsi_overbought (was overbought).
      2. Current RSI < rsi_overbought (dropping from overbought).
      3. Current Close < Previous Close (price declining).
      4. Close < SMA_20 (below short-term trend).
    """

    name = "OverboughtReversal"

    def __init__(
        self,
        rsi_overbought: float = 70.0,
        trend_ma: int = 20,
        rsi_col: str = "RSI_14",
    ) -> None:
        self.rsi_overbought = rsi_overbought
        self.trend_ma = trend_ma
        self.rsi_col = rsi_col

    def generate_signals(self, df: pd.DataFrame, ticker: str) -> list[Signal]:
        signals: list[Signal] = []
        trend_col = f"SMA_{self.trend_ma}"

        required = {"Close", self.rsi_col, trend_col}
        if not required.issubset(df.columns):
            logger.debug("OverboughtReversal skip %s: missing %s", ticker, required - set(df.columns))
            return signals
        if df.empty or len(df) < 2:
            logger.debug("OverboughtReversal skip %s: insufficient rows (%d)", ticker, len(df))
            return signals

        for i in range(1, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]

            prev_rsi = prev[self.rsi_col]
            curr_rsi = row[self.rsi_col]
            prev_close = prev["Close"]
            curr_close = row["Close"]
            trend = row[trend_col]

            if pd.isna(prev_rsi) or pd.isna(curr_rsi) or pd.isna(trend):
                continue

            # 1. Was overbought, now dropping
            if not (prev_rsi >= self.rsi_overbought and curr_rsi < self.rsi_overbought):
                continue
            # 2. Price declining
            if curr_close >= prev_close:
                continue
            # 3. Below trend MA
            if curr_close >= trend:
                continue

            price = float(curr_close)
            atr_raw = row.get("ATR_14")
            atr_val = float(atr_raw) if atr_raw is not None and not pd.isna(atr_raw) else 0.0
            sl = round(price + 2 * atr_val, 2) if atr_val > 0 else None

            signals.append(
                Signal(
                    ticker=ticker,
                    signal_type=SignalType.SELL,
                    strategy=self.name,
                    date=df.index[i].to_pydatetime() if hasattr(df.index[i], "to_pydatetime") else df.index[i],
                    price=price,
                    stop_loss=sl,
                    metadata={
                        "rsi_prev": round(float(prev_rsi), 2),
                        "rsi_curr": round(float(curr_rsi), 2),
                        "trend_ma": self.trend_ma,
                    },
                )
            )
        return signals
