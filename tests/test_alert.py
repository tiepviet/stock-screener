"""Tests for alert formatters and CLI helpers."""
from __future__ import annotations

from datetime import datetime

from src.stock_screener.alert import (
    format_signal_alert,
    format_summary_report,
)
from src.stock_screener.technical_engine import Signal, SignalType


def _sig(ticker: str = "7203", price: float = 1000.0, signal_type=SignalType.BUY) -> Signal:
    return Signal(
        ticker=ticker,
        signal_type=signal_type,
        strategy="VolumeBreakout",
        date=datetime(2024, 1, 1),
        price=price,
        stop_loss=900.0,
    )


def test_format_signal_alert_empty() -> None:
    out = format_signal_alert([], "2024-01-01")
    assert "Không có tín hiệu" in out or "không" in out.lower()


def test_format_signal_alert_with_buy() -> None:
    out = format_signal_alert([_sig()], "2024-01-01")
    assert "7203" in out
    assert "MUA" in out or "BUY" in out


def test_format_summary_report_counts() -> None:
    results = {"7203": [_sig()], "6758": [], "9984": [_sig(ticker="9984", price=2000.0)]}
    out = format_summary_report(results, "2024-01-01")
    assert "7203" in out
    assert "9984" in out
    assert "Đã scan: 3 mã" in out
    assert "Tín hiệu: 2" in out
