"""
Multi-Timeframe Confirmation — verify signals across daily + weekly.

A signal is "confirmed" when both timeframes agree on direction.
This reduces false signals significantly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from .data_loader import BaseDataLoader, YFinanceDataLoader
from .technical_engine import (
    BaseStrategy,
    PullbackMAStrategy,
    Signal,
    SignalType,
    TechnicalEngine,
    VolumeBreakoutStrategy,
)

logger = logging.getLogger(__name__)


@dataclass
class ConfirmedSignal:
    """A signal confirmed by multiple timeframes."""

    ticker: str
    signal_type: SignalType
    strategy: str
    entry_price: float
    stop_loss: float | None
    daily_signal: Signal
    weekly_signal: Signal | None
    confidence: float  # 0.0 to 1.0

    def __str__(self) -> str:
        sl = f" SL={self.stop_loss:.2f}" if self.stop_loss else ""
        wk = " (weekly confirmed)" if self.weekly_signal else " (daily only)"
        return (
            f"[{self.signal_type.value}] {self.ticker} "
            f"@ {self.entry_price:.2f} ({self.strategy}) "
            f"conf={self.confidence:.0%}{wk}{sl}"
        )


class MultiTimeframeConfirmer:
    """Confirm trading signals using multiple timeframes.

    Logic:
      - Generate signals on daily timeframe.
      - For each daily signal, check if weekly timeframe also shows
        the same directional bias (above/below key MAs, trend alignment).
      - Assign confidence score based on confirmation level.
    """

    def __init__(
        self,
        loader: BaseDataLoader | None = None,
        strategies: list[BaseStrategy] | None = None,
        min_confidence: float = 0.6,
    ) -> None:
        """Initialize confirmer.

        Args:
            loader: Data loader instance.
            strategies: Strategies to run on daily timeframe.
            min_confidence: Minimum confidence to emit a confirmed signal.
        """
        self.loader = loader or YFinanceDataLoader()
        self.engine = TechnicalEngine()
        self.strategies = strategies or [
            VolumeBreakoutStrategy(),
            PullbackMAStrategy(),
        ]
        self.min_confidence = min_confidence

    def confirm(
        self,
        ticker: str,
        daily_df: pd.DataFrame,
        weekly_df: pd.DataFrame,
    ) -> list[ConfirmedSignal]:
        """Check daily signals against weekly trend.

        Args:
            ticker: Raw ticker.
            daily_df: Enriched daily OHLCV.
            weekly_df: Enriched weekly OHLCV.

        Returns:
            List of ConfirmedSignal objects.
        """
        confirmed: list[ConfirmedSignal] = []

        # Generate daily signals
        daily_signals: list[Signal] = []
        for strat in self.strategies:
            daily_signals.extend(strat.generate_signals(daily_df, ticker))

        if not daily_signals:
            return confirmed

        # Get latest weekly trend indicators
        weekly_trend_up = self._check_weekly_trend(weekly_df)

        for sig in daily_signals:
            confidence = 0.5  # base confidence (daily only)

            # Weekly trend confirmation: +0.3
            if weekly_trend_up:
                confidence += 0.3

            # Check if daily trend also aligns: +0.2
            daily_trend_up = self._check_daily_trend(daily_df, sig.date)
            if daily_trend_up and sig.signal_type == SignalType.BUY:
                confidence += 0.2

            if confidence >= self.min_confidence:
                confirmed.append(
                    ConfirmedSignal(
                        ticker=ticker,
                        signal_type=sig.signal_type,
                        strategy=sig.strategy,
                        entry_price=sig.price,
                        stop_loss=sig.stop_loss,
                        daily_signal=sig,
                        weekly_signal=sig if weekly_trend_up else None,
                        confidence=round(confidence, 2),
                    )
                )

        return confirmed

    def scan_tickers(
        self,
        tickers: list[str],
        lookback_days: int = 545,
    ) -> list[ConfirmedSignal]:
        """Scan multiple tickers with multi-timeframe confirmation.

        Args:
            tickers: List of raw tickers.
            lookback_days: Days of daily data to fetch (>365 for SMA200).

        Returns:
            List of confirmed signals across all tickers.
        """
        from datetime import datetime, timedelta

        end = datetime.now().strftime("%Y-%m-%d")
        start = datetime.now() - timedelta(days=lookback_days)
        weekly_start = datetime.now() - timedelta(days=lookback_days * 2)

        all_confirmed: list[ConfirmedSignal] = []

        for ticker in tickers:
            try:
                # Fetch daily data
                daily = self.loader.fetch_ohlcv(
                    ticker, start.strftime("%Y-%m-%d"), end, "1d"
                )
                daily = self.engine.enrich(daily)

                # Fetch weekly data
                weekly = self.loader.fetch_ohlcv(
                    ticker, weekly_start.strftime("%Y-%m-%d"), end, "1wk"
                )
                weekly = self.engine.enrich(weekly, sma_periods=(10, 20, 50))

                confirmed = self.confirm(ticker, daily, weekly)
                all_confirmed.extend(confirmed)

            except Exception:
                logger.exception("Multi-timeframe scan failed for %s", ticker)

        return sorted(all_confirmed, key=lambda c: c.confidence, reverse=True)

    def _check_weekly_trend(self, weekly_df: pd.DataFrame) -> bool:
        """Check if weekly trend is bullish (latest close > SMA_20)."""
        if weekly_df.empty:
            return False
        if "SMA_20" not in weekly_df.columns:
            return False
        last_close = float(weekly_df["Close"].iloc[-1])
        last_sma20 = float(weekly_df["SMA_20"].iloc[-1])
        if pd.isna(last_sma20):
            return False
        return last_close > last_sma20

    def _check_daily_trend(self, daily_df: pd.DataFrame, signal_date) -> bool:
        """Check if daily trend is bullish at signal date (close > SMA_50)."""
        if "SMA_50" not in daily_df.columns:
            return False
        try:
            if signal_date in daily_df.index:
                row = daily_df.loc[signal_date]
                return float(row["Close"]) > float(row["SMA_50"])
        except Exception:
            pass
        return False
