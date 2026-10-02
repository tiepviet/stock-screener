"""Fail-closed earnings status regression tests."""

from __future__ import annotations

from src.stock_screener.earnings_calendar import EarningsCalendar, EarningsInfo


def test_unknown_or_failed_earnings_is_not_classified_as_safe() -> None:
    calendar = EarningsCalendar(loader=object(), warning_days=14)  # type: ignore[arg-type]
    calendar.check_batch = lambda tickers: {  # type: ignore[method-assign]
        "7203": EarningsInfo(ticker="7203", status="error", error="provider failed"),
        "6758": EarningsInfo(ticker="6758", status="unknown"),
    }
    safe, risky = calendar.filter_safe(["7203", "6758"])
    assert safe == []
    assert risky == ["7203", "6758"]
