"""Shared fixtures: synthetic OHLCV data + tmp dirs."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def synthetic_ohlcv() -> pd.DataFrame:
    """Build a 300-bar synthetic OHLCV for tests (no network)."""
    np.random.seed(42)
    n = 300
    dates = pd.date_range(end=datetime(2024, 6, 24), periods=n, freq="B")
    close = 1000 + np.cumsum(np.random.randn(n) * 5)
    high = close + np.abs(np.random.randn(n) * 3)
    low = close - np.abs(np.random.randn(n) * 3)
    open_ = close + np.random.randn(n) * 2
    vol = np.random.randint(100_000, 1_000_000, n).astype(float)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=dates,
    )


@pytest.fixture
def uptrending_ohlcv() -> pd.DataFrame:
    """Clean uptrend — should trigger PullbackMA on rebound."""
    n = 250
    dates = pd.date_range(end=datetime(2024, 6, 24), periods=n, freq="B")
    close = np.linspace(100, 200, n) + np.random.RandomState(1).randn(n) * 2
    return pd.DataFrame(
        {
            "Open": close + 0.5,
            "High": close + 1.5,
            "Low": close - 1.5,
            "Close": close,
            "Volume": np.full(n, 500_000.0),
        },
        index=dates,
    )


@pytest.fixture
def tmp_cache(monkeypatch, tmp_path: Path) -> Path:
    """Redirect CACHE_DIR / FUND_CACHE_DIR to tmp_path."""
    from src.stock_screener import data_loader

    monkeypatch.setattr(data_loader, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(data_loader, "FUND_CACHE_DIR", tmp_path / "fundamentals")
    (tmp_path / "fundamentals").mkdir(exist_ok=True)
    data_loader._fund_cache_mem.clear()
    return tmp_path
