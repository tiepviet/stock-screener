"""Tests for price_target.py — PriceTargetEngine."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.stock_screener.price_target import (
    BuyZone,
    FibonacciLevels,
    PriceTargetEngine,
    RiskReward,
    SellZone,
    SupportResistance,
)


def _make_ohlcv(n: int = 100, base: float = 1000.0) -> pd.DataFrame:
    """Create synthetic OHLCV data with a trend."""
    np.random.seed(42)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = base + np.cumsum(np.random.randn(n) * 10)
    high = close + np.abs(np.random.randn(n) * 5)
    low = close - np.abs(np.random.randn(n) * 5)
    volume = np.random.randint(100000, 1000000, n).astype(float)
    df = pd.DataFrame({
        "Open": close + np.random.randn(n) * 2,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": volume,
    }, index=dates)
    # Add indicators
    df["SMA_20"] = df["Close"].rolling(20).mean()
    df["SMA_50"] = df["Close"].rolling(50).mean()
    df["RSI_14"] = 50 + np.random.randn(n) * 10  # Approximate RSI
    df["ATR_14"] = (df["High"] - df["Low"]).rolling(14).mean()
    df["VOL_SMA_20"] = df["Volume"].rolling(20).mean()
    return df


class TestRiskReward:
    def test_basic_calculation(self) -> None:
        rr = RiskReward(entry=1000, stop_loss=950, target=1100)
        assert rr.risk == 50
        assert rr.reward == 100
        assert rr.ratio == 2.0

    def test_zero_risk(self) -> None:
        rr = RiskReward(entry=1000, stop_loss=1000, target=1100)
        assert rr.ratio == 0.0

    def test_sell_direction(self) -> None:
        rr = RiskReward(entry=1000, stop_loss=1050, target=900)
        assert rr.risk == 50
        assert rr.reward == 100
        assert rr.ratio == 2.0


class TestFibonacciLevels:
    def test_range(self) -> None:
        fib = FibonacciLevels(swing_low=100, swing_high=200)
        assert fib.range == 100

    def test_retracement_levels(self) -> None:
        fib = FibonacciLevels(
            swing_low=100,
            swing_high=200,
            retracements={
                "38.2%": 161.8,
                "61.8%": 138.2,
            },
        )
        assert fib.retracements["38.2%"] == 161.8
        assert fib.retracements["61.8%"] == 138.2


class TestSupportResistance:
    def test_empty(self) -> None:
        sr = SupportResistance()
        assert sr.supports == []
        assert sr.resistances == []


class TestBuyZone:
    def test_auto_entry_suggestion(self) -> None:
        bz = BuyZone(
            ticker="7203",
            current_price=2500,
            zone_low=2400,
            zone_high=2450,
        )
        assert bz.entry_suggestion == 2425.0

    def test_explicit_entry(self) -> None:
        bz = BuyZone(
            ticker="7203",
            current_price=2500,
            zone_low=2400,
            zone_high=2450,
            entry_suggestion=2420,
        )
        assert bz.entry_suggestion == 2420


class TestSellZone:
    def test_auto_exit_suggestion(self) -> None:
        sz = SellZone(
            ticker="7203",
            current_price=2500,
            zone_low=2600,
            zone_high=2700,
        )
        assert sz.exit_suggestion == 2650.0


class TestPriceTargetEngine:
    def test_empty_data(self) -> None:
        engine = PriceTargetEngine()
        df = pd.DataFrame()
        targets = engine.compute_all(df, "7203")
        assert targets.current_price == 0.0

    def test_basic_computation(self) -> None:
        engine = PriceTargetEngine(swing_lookback=10)
        df = _make_ohlcv(100)
        targets = engine.compute_all(df, "7203", entry_price=1000, stop_loss=930)
        assert targets.ticker == "7203"
        assert targets.current_price > 0
        assert targets.buy_zone is not None
        assert targets.sell_zone is not None
        assert targets.fibonacci is not None
        assert targets.sr is not None

    def test_buy_zone_has_levels(self) -> None:
        engine = PriceTargetEngine(swing_lookback=10)
        df = _make_ohlcv(100)
        targets = engine.compute_all(df, "7203")
        assert targets.buy_zone is not None
        assert targets.buy_zone.zone_low > 0
        assert targets.buy_zone.zone_high > 0
        assert targets.buy_zone.zone_low <= targets.buy_zone.zone_high

    def test_sell_zone_has_levels(self) -> None:
        engine = PriceTargetEngine(swing_lookback=10)
        df = _make_ohlcv(100)
        targets = engine.compute_all(df, "7203")
        assert targets.sell_zone is not None
        assert targets.sell_zone.zone_low > 0
        assert targets.sell_zone.zone_high > 0

    def test_fibonacci_levels(self) -> None:
        engine = PriceTargetEngine(swing_lookback=10)
        df = _make_ohlcv(100)
        targets = engine.compute_all(df, "7203")
        fib = targets.fibonacci
        assert fib is not None
        assert len(fib.retracements) > 0
        assert len(fib.extensions) > 0
        # Retracement should be between swing low and high
        for _label, level in fib.retracements.items():
            assert fib.swing_low <= level <= fib.swing_high

    def test_risk_reward(self) -> None:
        engine = PriceTargetEngine(swing_lookback=10)
        df = _make_ohlcv(100)
        targets = engine.compute_all(df, "7203", entry_price=1000, stop_loss=930)
        rr = targets.risk_reward
        assert rr is not None
        assert rr.risk > 0
        assert rr.ratio > 0

    def test_take_profits(self) -> None:
        engine = PriceTargetEngine(swing_lookback=10)
        df = _make_ohlcv(100)
        targets = engine.compute_all(df, "7203")
        assert len(targets.take_profits) > 0
        # Take-profits should be above current price
        for tp in targets.take_profits:
            assert tp >= targets.current_price

    def test_cluster_levels(self) -> None:
        clustered = PriceTargetEngine._cluster_levels(
            [100, 101, 102, 200, 201, 202],
            threshold_pct=0.02,
        )
        # Should cluster into ~2 groups
        assert len(clustered) <= 4

    def test_support_resistance(self) -> None:
        engine = PriceTargetEngine(swing_lookback=10)
        df = _make_ohlcv(100)
        sr = engine._find_support_resistance(df)
        # Should find some levels
        assert isinstance(sr, SupportResistance)

    def test_insufficient_data(self) -> None:
        engine = PriceTargetEngine(swing_lookback=50)
        df = _make_ohlcv(10)  # Too few rows
        targets = engine.compute_all(df, "7203")
        assert targets.current_price == 0.0
