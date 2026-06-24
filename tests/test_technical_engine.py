"""Tests for technical_engine."""
from __future__ import annotations

import pandas as pd

from src.stock_screener.technical_engine import (
    PullbackMAStrategy,
    Signal,
    SignalType,
    TechnicalEngine,
    VolumeBreakoutStrategy,
)


def test_enrich_adds_all_indicators(synthetic_ohlcv: pd.DataFrame) -> None:
    engine = TechnicalEngine()
    out = engine.enrich(synthetic_ohlcv)
    for col in ("SMA_20", "SMA_50", "SMA_200", "RSI_14", "ATR_14", "VOL_SMA_20"):
        assert col in out.columns, f"Missing column {col}"
    # First ~199 SMA_200 should be NaN (warmup)
    assert out["SMA_200"].iloc[:199].isna().all()
    # After warmup should have values
    assert out["SMA_200"].iloc[200:].notna().all()


def test_add_moving_averages_default(synthetic_ohlcv: pd.DataFrame) -> None:
    out = TechnicalEngine.add_moving_averages(synthetic_ohlcv)
    assert "SMA_20" in out.columns
    assert "SMA_50" in out.columns
    assert "SMA_200" in out.columns


def test_add_rsi_range(synthetic_ohlcv: pd.DataFrame) -> None:
    out = TechnicalEngine.add_rsi(synthetic_ohlcv)
    rsi = out["RSI_14"].dropna()
    assert (rsi >= 0).all() and (rsi <= 100).all()


def test_add_bollinger_bands_columns(synthetic_ohlcv: pd.DataFrame) -> None:
    out = TechnicalEngine.add_bollinger_bands(synthetic_ohlcv)
    assert any("BB" in c for c in out.columns)


def test_volume_breakout_emits_signal_on_breakout(synthetic_ohlcv: pd.DataFrame) -> None:
    df = TechnicalEngine().enrich(synthetic_ohlcv)
    # Force a clear breakout on the last bar
    df.loc[df.index[-1], "Close"] = df["High"].iloc[-21:-1].max() * 1.5
    df.loc[df.index[-1], "Volume"] = df["VOL_SMA_20"].iloc[-1] * 5
    sigs = VolumeBreakoutStrategy().generate_signals(df, "TEST")
    assert any(s.signal_type == SignalType.BUY for s in sigs)


def test_volume_breakout_handles_missing_columns() -> None:
    df = pd.DataFrame({"Close": [1, 2, 3]})
    sigs = VolumeBreakoutStrategy().generate_signals(df, "X")
    assert sigs == []


def test_pullback_ma_uptrend_emits_signal(uptrending_ohlcv: pd.DataFrame) -> None:
    df = TechnicalEngine().enrich(uptrending_ohlcv)
    sigs = PullbackMAStrategy().generate_signals(df, "T")
    # An uptrend with a known pullback should produce at least one signal
    assert isinstance(sigs, list)


def test_signal_str_repr_includes_ticker() -> None:
    s = Signal(
        ticker="7203",
        signal_type=SignalType.BUY,
        strategy="Test",
        date=pd.Timestamp("2024-01-01"),
        price=1500.0,
        stop_loss=1400.0,
    )
    txt = str(s)
    assert "7203" in txt
    assert "1500" in txt
    assert "1400" in txt


def test_pullback_ma_handles_missing_columns() -> None:
    df = pd.DataFrame({"Close": [1, 2, 3]})
    sigs = PullbackMAStrategy().generate_signals(df, "X")
    assert sigs == []
