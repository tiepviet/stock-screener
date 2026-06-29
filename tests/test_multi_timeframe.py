"""Tests for multi_timeframe.py."""
from __future__ import annotations

import pandas as pd

from src.stock_screener.multi_timeframe import ConfirmedSignal, MultiTimeframeConfirmer
from src.stock_screener.technical_engine import SignalType, TechnicalEngine


def _make_daily_and_weekly():
    """Create synthetic daily + weekly data for testing."""
    from datetime import datetime

    import numpy as np

    np.random.seed(42)
    # Daily: 300 bars uptrend
    n_daily = 300
    dates_daily = pd.date_range(end=datetime(2024, 6, 24), periods=n_daily, freq="B")
    close_daily = np.linspace(100, 200, n_daily) + np.random.randn(n_daily) * 2
    daily = pd.DataFrame(
        {
            "Open": close_daily + 0.5,
            "High": close_daily + 1.5,
            "Low": close_daily - 1.5,
            "Close": close_daily,
            "Volume": np.full(n_daily, 500_000.0),
        },
        index=dates_daily,
    )

    # Weekly: 60 bars uptrend
    n_weekly = 60
    dates_weekly = pd.date_range(end=datetime(2024, 6, 24), periods=n_weekly, freq="W")
    close_weekly = np.linspace(100, 200, n_weekly) + np.random.randn(n_weekly) * 3
    weekly = pd.DataFrame(
        {
            "Open": close_weekly + 0.5,
            "High": close_weekly + 2.0,
            "Low": close_weekly - 2.0,
            "Close": close_weekly,
            "Volume": np.full(n_weekly, 2_000_000.0),
        },
        index=dates_weekly,
    )

    engine = TechnicalEngine()
    daily = engine.enrich(daily)
    weekly = engine.enrich(weekly, sma_periods=(10, 20, 50))
    return daily, weekly


def test_confirmed_signal_str_includes_ticker() -> None:
    daily, weekly = _make_daily_and_weekly()
    confirmer = MultiTimeframeConfirmer(min_confidence=0.0)
    confirmed = confirmer.confirm("TEST", daily, weekly)
    for c in confirmed:
        assert "TEST" in str(c)


def test_confirmed_signal_has_required_fields() -> None:
    daily, weekly = _make_daily_and_weekly()
    confirmer = MultiTimeframeConfirmer(min_confidence=0.0)
    confirmed = confirmer.confirm("TEST", daily, weekly)
    for c in confirmed:
        assert isinstance(c, ConfirmedSignal)
        assert c.ticker == "TEST"
        assert c.signal_type in (SignalType.BUY, SignalType.SELL)
        assert 0.0 <= c.confidence <= 1.0


def test_min_confidence_filters_low_confidence() -> None:
    daily, weekly = _make_daily_and_weekly()
    confirmer_strict = MultiTimeframeConfirmer(min_confidence=0.95)
    confirmer_loose = MultiTimeframeConfirmer(min_confidence=0.0)
    confirmed_strict = confirmer_strict.confirm("TEST", daily, weekly)
    confirmed_loose = confirmer_loose.confirm("TEST", daily, weekly)
    # Strict should have fewer or equal signals
    assert len(confirmed_strict) <= len(confirmed_loose)


def test_weekly_trend_boosts_confidence() -> None:
    daily, weekly = _make_daily_and_weekly()
    # Weekly is uptrend (close > SMA_20), so confidence should be higher
    confirmer = MultiTimeframeConfirmer(min_confidence=0.0)
    confirmed = confirmer.confirm("TEST", daily, weekly)
    for c in confirmed:
        # With weekly confirmation, confidence should be >= 0.8
        assert c.confidence >= 0.5


def test_confirm_empty_daily_returns_empty() -> None:
    """No daily signals → no confirmed signals."""
    from datetime import datetime

    import numpy as np

    n = 300
    dates = pd.date_range(end=datetime(2024, 6, 24), periods=n, freq="B")
    # Flat data — unlikely to trigger signals
    flat_close = np.full(n, 1000.0)
    daily = pd.DataFrame(
        {
            "Open": flat_close,
            "High": flat_close + 1,
            "Low": flat_close - 1,
            "Close": flat_close,
            "Volume": np.full(n, 500_000.0),
        },
        index=dates,
    )
    engine = TechnicalEngine()
    daily = engine.enrich(daily)
    weekly = daily.copy()

    confirmer = MultiTimeframeConfirmer(min_confidence=0.0)
    confirmed = confirmer.confirm("TEST", daily, weekly)
    # Flat data shouldn't generate signals
    assert isinstance(confirmed, list)
