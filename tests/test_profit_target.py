"""Tests for profit_target calculator."""
from __future__ import annotations

import pytest

from src.stock_screener.profit_target import (
    TargetRow,
    calculate_exit_price,
    summarize,
)


def test_calculate_exit_price_basic() -> None:
    assert calculate_exit_price(1000, 5.0) == 1050.0
    assert calculate_exit_price(1000, 0) == 1000.0
    assert calculate_exit_price(1000, -5.0) == 950.0


def test_calculate_exit_price_invalid_entry() -> None:
    with pytest.raises(ValueError):
        calculate_exit_price(0, 5)
    with pytest.raises(ValueError):
        calculate_exit_price(-100, 5)


def test_target_row_exit_price() -> None:
    r = TargetRow(ticker="7203", entry_price=2000, target_pct=10.0)
    assert r.exit_price == 2200.0
    assert r.per_share_profit == 200.0


def test_target_row_validation() -> None:
    with pytest.raises(ValueError):
        TargetRow(ticker="X", entry_price=-1)
    with pytest.raises(ValueError):
        TargetRow(ticker="X", entry_price=100, target_pct=2000)
    with pytest.raises(ValueError):
        TargetRow(ticker="X", entry_price=100, shares=-1)


def test_target_row_position_value() -> None:
    r = TargetRow(ticker="7203", entry_price=2000, target_pct=10, shares=100)
    assert r.position_value == 200000
    assert r.target_value == pytest.approx(220000, rel=1e-3)


def test_summarize_empty() -> None:
    s = summarize([])
    assert s.total_invested == 0
    assert s.total_profit == 0
    assert s.position_count == 0


def test_summarize_with_shares() -> None:
    rows = [
        TargetRow("A", 1000, 5, shares=100),
        TargetRow("B", 2000, 10, shares=50),
    ]
    s = summarize(rows)
    assert s.total_invested == 100 * 1000 + 50 * 2000
    assert s.total_target_value == 100 * 1050 + 50 * 2200
    assert s.total_profit == s.total_target_value - s.total_invested
    assert s.position_count == 2
    # invested=200000, target=215000, weighted=215000/200000-1 = 7.5%
    assert s.weighted_target_pct == pytest.approx(7.5, rel=1e-3)


def test_summarize_without_shares_uses_equal_weight() -> None:
    rows = [
        TargetRow("A", 1000, 5),
        TargetRow("B", 2000, 10),
    ]
    s = summarize(rows)
    assert s.total_invested == 0
    assert s.weighted_target_pct == pytest.approx(7.5, rel=1e-3)
    assert s.position_count == 2


def test_summarize_skips_zero_entry() -> None:
    rows = [
        TargetRow("A", 0, 5),
        TargetRow("B", 1000, 10),
    ]
    s = summarize(rows)
    assert s.position_count == 1
