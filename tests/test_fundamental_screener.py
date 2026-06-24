"""Tests for fundamental_screener."""
from __future__ import annotations

from src.stock_screener.fundamental_screener import (
    Condition,
    FundamentalScreener,
    default_japan_value_conditions,
    growth_conditions,
)


class _StubLoader:
    def __init__(self, data: dict) -> None:
        self.data = data

    def fetch_batch_fundamentals(self, tickers):
        return {t: self.data.get(t, {}) for t in tickers}


def test_condition_evaluate_pass() -> None:
    c = Condition("roe", ">", 0.10)
    assert c.evaluate({"roe": 0.15})


def test_condition_evaluate_fail() -> None:
    c = Condition("roe", ">", 0.10)
    assert not c.evaluate({"roe": 0.05})


def test_condition_missing_metric_fails_strict() -> None:
    c = Condition("roe", ">", 0.10)
    assert not c.evaluate({})


def test_condition_unknown_operator_skips() -> None:
    c = Condition("roe", "??", 0.10)
    assert c.evaluate({"roe": 0.05})


def test_condition_handles_none_value() -> None:
    c = Condition("pe", "<", 20)
    assert not c.evaluate({"pe": None})


def test_screener_filters_correctly() -> None:
    loader = _StubLoader(
        {
            "AAA": {"roe": 0.20, "pe": 10, "pb": 1, "eps": 50, "dividend_yield": 0.03},
            "BBB": {"roe": 0.02, "pe": 10, "pb": 1, "eps": 50, "dividend_yield": 0.03},
            "CCC": {"roe": 0.20, "pe": 50, "pb": 1, "eps": 50, "dividend_yield": 0.03},
        }
    )
    s = FundamentalScreener(loader)
    out = s.screen(["AAA", "BBB", "CCC"], default_japan_value_conditions())
    assert list(out["ticker"]) == ["AAA"]


def test_screener_empty_input() -> None:
    s = FundamentalScreener(_StubLoader({}))
    out = s.screen([], default_japan_value_conditions())
    assert out.empty


def test_screener_no_conditions_returns_all() -> None:
    loader = _StubLoader({"AAA": {"roe": 0.20}, "BBB": {"roe": 0.05}})
    s = FundamentalScreener(loader)
    out = s.screen(["AAA", "BBB"], [])
    assert len(out) == 2


def test_from_dict() -> None:
    cs = FundamentalScreener.from_dict(
        [{"metric": "roe", "operator": ">", "value": 0.1}]
    )
    assert len(cs) == 1
    assert cs[0].metric == "roe"


def test_growth_conditions_uses_params() -> None:
    cs = growth_conditions(min_roe=0.20, max_pe=15.0)
    assert cs[0].value == 0.20
    assert cs[1].value == 15.0
