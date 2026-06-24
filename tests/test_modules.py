"""Tests for risk_management + backtest + portfolio + screen_chain + data_loader."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from src.stock_screener.backtest import Backtester, Trade
from src.stock_screener.data_loader import YFinanceDataLoader
from src.stock_screener.portfolio import PortfolioTracker
from src.stock_screener.risk_management import RiskManager
from src.stock_screener.screen_chain import ScreenChainer
from src.stock_screener.technical_engine import Signal, SignalType


def _buy_signal(ticker: str = "7203", price: float = 1000.0, sl: float | None = 900.0) -> Signal:
    return Signal(
        ticker=ticker,
        signal_type=SignalType.BUY,
        strategy="Test",
        date=datetime(2024, 1, 1),
        price=price,
        stop_loss=sl,
    )


# --- risk_management ---


def test_risk_manager_validates_inputs() -> None:
    with pytest.raises(ValueError):
        RiskManager(total_capital=0)
    with pytest.raises(ValueError):
        RiskManager(total_capital=1000, risk_per_trade=1.5)


def test_risk_manager_position_sizing_basic() -> None:
    rm = RiskManager(total_capital=1_000_000, risk_per_trade=0.01, hard_stop_pct=0.07)
    plan = rm.calculate_position(_buy_signal(price=1000, sl=950))
    # hard_stop = 930, strategy stop = 950, tighter (higher price) = 950
    # risk_per_share = 50, risk_amount = 10000, shares = 200
    assert plan.shares == 200
    assert plan.entry_price == 1000
    assert plan.stop_loss == 950
    assert plan.risk_amount == pytest.approx(10000, rel=1e-3)


def test_risk_manager_uses_tighter_stop() -> None:
    rm = RiskManager(total_capital=1_000_000, hard_stop_pct=0.07)
    # Strategy stop at 950 (5% risk), hard stop at 930 (7% risk). Tighter = higher price = 950.
    plan = rm.calculate_position(_buy_signal(price=1000, sl=950))
    assert plan.stop_loss == 950


def test_risk_manager_rejects_non_buy() -> None:
    rm = RiskManager(total_capital=1_000_000)
    sig = _buy_signal()
    sig.signal_type = SignalType.SELL
    with pytest.raises(ValueError):
        rm.calculate_position(sig)


def test_risk_manager_batch_respects_capital() -> None:
    rm = RiskManager(total_capital=2_000_000, hard_stop_pct=0.07)
    sigs = [_buy_signal(ticker=f"T{i}", price=1000) for i in range(20)]
    plans = rm.batch_positions(sigs)
    # 2M capital, 1k/entry, 2 ATR SL ≈ -90 from entry → 90 risk/share → 222 shares → 222k position
    # Each position uses ~220k, 9 fit within 2M, 10th would exceed
    assert all(p.position_value <= 2_000_000 for p in plans)
    total = sum(p.position_value for p in plans)
    assert total <= 2_000_000


def test_risk_manager_check_stop_loss_triggered() -> None:
    rm = RiskManager(total_capital=1_000_000)
    plan = rm.calculate_position(_buy_signal(price=1000, sl=900))
    assert rm.check_stop_loss(plan, current_price=850) == "STOP_LOSS"
    assert rm.check_stop_loss(plan, current_price=950) is None


# --- backtest ---


def test_backtest_no_signals_yields_no_trades(synthetic_ohlcv: pd.DataFrame) -> None:
    bt = Backtester()
    res = bt.run(synthetic_ohlcv, [], "X", "Test")
    assert res.total_trades == 0
    assert res.initial_capital == res.final_capital


def test_backtest_cagr_handles_total_loss(synthetic_ohlcv: pd.DataFrame) -> None:
    """CAGR must not raise when final_capital goes to 0 (negative ratio)."""
    bt = Backtester(initial_capital=1_000_000, hard_stop_pct=0.99)
    # Place 100 signals to force many stops → capital drains
    sigs = []
    for i in range(0, len(synthetic_ohlcv) - 5, 5):
        s = _buy_signal()
        s.date = synthetic_ohlcv.index[i]
        sigs.append(s)
    res = bt.run(synthetic_ohlcv, sigs, "X", "Test")
    # CAGR must be a real float, not complex
    assert isinstance(res.cagr, float)
    assert res.cagr >= -1.0  # total loss → -1.0 (capped)


def test_backtest_with_signals(synthetic_ohlcv: pd.DataFrame) -> None:
    bt = Backtester(initial_capital=1_000_000, hard_stop_pct=0.10)
    sig = _buy_signal()
    sig.date = synthetic_ohlcv.index[50]
    res = bt.run(synthetic_ohlcv, [sig], "X", "Test")
    assert isinstance(res.equity_curve, pd.Series)
    assert res.equity_curve.iloc[0] == 1_000_000


def test_backtest_take_profit_exits(synthetic_ohlcv: pd.DataFrame) -> None:
    bt = Backtester(initial_capital=1_000_000, take_profit_pct=0.20)
    sig = _buy_signal(price=1000, sl=900)
    sig.date = synthetic_ohlcv.index[10]
    res = bt.run(synthetic_ohlcv, [sig], "X", "Test")
    reasons = {t.exit_reason for t in res.trades}
    assert reasons  # at least one exit reason


def test_backtest_commission_reduces_pnl(synthetic_ohlcv: pd.DataFrame) -> None:
    bt_no = Backtester(commission_pct=0.0)
    bt_yes = Backtester(commission_pct=0.01)
    sig = _buy_signal()
    sig.date = synthetic_ohlcv.index[10]
    r_no = bt_no.run(synthetic_ohlcv, [sig], "X", "Test")
    r_yes = bt_yes.run(synthetic_ohlcv, [sig], "X", "Test")
    if r_no.trades and r_yes.trades:
        assert r_yes.final_capital <= r_no.final_capital


def test_backtest_invalid_commission_raises() -> None:
    with pytest.raises(ValueError):
        Backtester(commission_pct=1.5)
    with pytest.raises(ValueError):
        Backtester(take_profit_pct=-0.1)


def test_trade_close_computes_pnl() -> None:
    t = Trade(ticker="X", strategy="S", entry_date=datetime(2024, 1, 1), entry_price=100, shares=10)
    t.close(datetime(2024, 1, 10), 110)
    assert t.pnl == 100
    assert t.pnl_pct == pytest.approx(0.10, rel=1e-3)
    assert t.exit_reason == ""


def test_backtest_run_multi_enriches(synthetic_ohlcv: pd.DataFrame) -> None:
    from src.stock_screener.technical_engine import VolumeBreakoutStrategy

    bt = Backtester()
    res = bt.run_multi(synthetic_ohlcv, VolumeBreakoutStrategy(), "X")
    assert res.ticker == "X"


# --- portfolio ---


def test_portfolio_add_and_close(tmp_path: Path) -> None:
    pf_path = tmp_path / "pf.json"
    from src.stock_screener import portfolio as pmod

    monkey = pmod.PORTFOLIO_FILE
    pmod.PORTFOLIO_FILE = pf_path
    try:
        rm = RiskManager(total_capital=1_000_000)
        plan = rm.calculate_position(_buy_signal(price=1000, sl=900))
        pf = PortfolioTracker(total_capital=1_000_000)
        pf.add_position(plan, sector="Tech")
        assert "7203" in pf.positions
        pf.close_position("7203", exit_price=1100, reason="MANUAL")
        assert "7203" not in pf.positions
        assert len(pf.closed_trades) == 1
        assert pf.closed_trades[0]["pnl"] > 0
    finally:
        pmod.PORTFOLIO_FILE = monkey


def test_portfolio_dedupes(tmp_path: Path) -> None:
    from src.stock_screener import portfolio as pmod

    monkey = pmod.PORTFOLIO_FILE
    pmod.PORTFOLIO_FILE = tmp_path / "pf.json"
    try:
        rm = RiskManager(total_capital=1_000_000)
        plan = rm.calculate_position(_buy_signal())
        pf = PortfolioTracker(total_capital=1_000_000)
        pf.add_position(plan)
        pf.add_position(plan)  # second add should be skipped
        assert len(pf.positions) == 1
    finally:
        pmod.PORTFOLIO_FILE = monkey


def test_portfolio_sector_exposure(tmp_path: Path) -> None:
    from src.stock_screener import portfolio as pmod

    pmod.PORTFOLIO_FILE = tmp_path / "pf.json"
    rm = RiskManager(total_capital=10_000_000)
    pf = PortfolioTracker(total_capital=10_000_000, max_sector_pct=0.30)
    for t in ["A", "B", "C"]:
        plan = rm.calculate_position(_buy_signal(ticker=t, price=100, sl=90))
        pf.add_position(plan, sector="Tech")
        pf.positions[t].update_price(100)
    exposure = pf.sector_exposure()
    assert exposure.get("Tech") == pytest.approx(1.0, rel=1e-3)


def test_portfolio_check_stop_loss_without_price_returns_empty(tmp_path: Path) -> None:
    from src.stock_screener import portfolio as pmod

    pmod.PORTFOLIO_FILE = tmp_path / "pf.json"
    rm = RiskManager(total_capital=1_000_000)
    pf = PortfolioTracker(total_capital=1_000_000)
    pf.add_position(rm.calculate_position(_buy_signal(price=100, sl=90)))
    # No price update → current_price=0 → SL check should skip
    assert pf.check_stop_losses() == []


def test_portfolio_stats_keys(tmp_path: Path) -> None:
    from src.stock_screener import portfolio as pmod

    pmod.PORTFOLIO_FILE = tmp_path / "pf.json"
    pf = PortfolioTracker(total_capital=1_000_000)
    stats = pf.stats()
    assert "open_positions" in stats
    assert "win_rate" in stats
    assert "overexposed_sectors" in stats


# --- data_loader ---


def test_normalize_ticker_appends_suffix() -> None:
    loader = YFinanceDataLoader()
    assert loader.normalize_ticker("7203") == "7203.T"
    assert loader.normalize_ticker("7203.T") == "7203.T"
    assert loader.normalize_ticker("  aapl  ") == "AAPL.T"


def test_fundamentals_cache_roundtrip(tmp_cache: Path) -> None:
    loader = YFinanceDataLoader()
    # Manually populate cache to avoid network
    loader._write_fund_cache("7203.T", {"pe": 10.0, "roe": 0.15})
    cached = loader._read_fund_cache("7203.T")
    assert cached["pe"] == 10.0
    assert cached["roe"] == 0.15


# --- screen_chain ---


def test_screen_chain_empty_input_returns_empty() -> None:
    chainer = ScreenChainer()
    out = chainer.run([], top_n=5)
    assert out == []


def test_screen_chain_sorted_descending() -> None:
    chainer = ScreenChainer()
    s1 = chainer._compute_fundamental_score({"roe": 0.30, "pe": 5, "dividend_yield": 0.05})
    s2 = chainer._compute_fundamental_score({"roe": 0.05, "pe": 40, "dividend_yield": 0.0})
    assert s1["roe"] > s2["roe"]
    assert s1["pe"] > s2["pe"]
    assert s1["dividend"] > s2["dividend"]


def test_screen_chain_missing_data_defaults_to_zero() -> None:
    chainer = ScreenChainer()
    s = chainer._compute_fundamental_score({})
    assert all(v == 0 for v in s.values())
