"""Tests for risk_management + backtest + portfolio + screen_chain + data_loader."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from src.stock_screener import auth, user_store
from src.stock_screener.backtest import Backtester, Trade
from src.stock_screener.data_loader import YFinanceDataLoader
from src.stock_screener.portfolio import PortfolioTracker, PositionPlan
from src.stock_screener.risk_management import RiskManager
from src.stock_screener.screen_chain import ScreenChainer
from src.stock_screener.technical_engine import (
    Signal,
    SignalType,
    TechnicalEngine,
    VolumeBreakoutStrategy,
)


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


def test_risk_manager_zero_risk_per_share_returns_zero_shares() -> None:
    """When entry == stop_loss, risk_per_share=0, should return shares=0."""
    rm = RiskManager(total_capital=1_000_000)
    plan = rm.calculate_position(_buy_signal(price=1000, sl=1000))
    assert plan.shares == 0
    assert plan.risk_amount == 0.0
    assert plan.position_value == 0.0


# --- backtest ---


def test_backtest_no_signals_yields_no_trades(synthetic_ohlcv: pd.DataFrame) -> None:
    bt = Backtester()
    res = bt.run(synthetic_ohlcv, [], "X", "Test")
    assert res.total_trades == 0
    assert res.initial_capital == res.final_capital


def test_backtest_ignores_sell_signals(synthetic_ohlcv: pd.DataFrame) -> None:
    """H1: SELL/HOLD signals must never trigger a buy."""
    bt = Backtester(initial_capital=1_000_000, hard_stop_pct=0.10)
    sell = _buy_signal()
    sell.signal_type = SignalType.SELL
    sell.date = synthetic_ohlcv.index[50]
    res = bt.run(synthetic_ohlcv, [sell], "X", "Test")
    assert res.total_trades == 0
    assert res.final_capital == res.initial_capital


def test_backtest_skips_entry_when_capital_below_one_share(
    synthetic_ohlcv: pd.DataFrame,
) -> None:
    """H2: capital below one-share cost must skip entry, never go negative."""
    bt = Backtester(initial_capital=100, hard_stop_pct=0.10)
    sig = _buy_signal(price=1000, sl=900)
    sig.date = synthetic_ohlcv.index[50]
    res = bt.run(synthetic_ohlcv, [sig], "X", "Test")
    assert res.total_trades == 0
    assert res.final_capital == 100
    assert res.final_capital >= 0


def test_backtest_never_drives_capital_negative(
    synthetic_ohlcv: pd.DataFrame,
) -> None:
    """H2 regression: any series of signals keeps capital >= 0."""
    bt = Backtester(initial_capital=500, hard_stop_pct=0.05)
    sigs = []
    for i in range(0, len(synthetic_ohlcv) - 5, 5):
        s = _buy_signal(price=1000, sl=900)
        s.date = synthetic_ohlcv.index[i]
        sigs.append(s)
    res = bt.run(synthetic_ohlcv, sigs, "X", "Test")
    assert res.final_capital >= 0


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


def test_backtest_enters_at_next_bar_open(synthetic_ohlcv: pd.DataFrame) -> None:
    """B1: a signal on bar i must fill at bar i+1's open (no look-ahead)."""
    bt = Backtester(initial_capital=1_000_000, hard_stop_pct=0.99, slippage_pct=0.0)
    sig = _buy_signal(price=1000, sl=None)
    sig.date = synthetic_ohlcv.index[10]
    res = bt.run(synthetic_ohlcv, [sig], "X", "Test")
    assert res.total_trades == 1
    t = res.trades[0]
    assert t.entry_date == synthetic_ohlcv.index[11].to_pydatetime()
    assert t.entry_price == pytest.approx(float(synthetic_ohlcv["Open"].iloc[11]))


def test_backtest_signal_on_last_bar_is_not_actionable(synthetic_ohlcv: pd.DataFrame) -> None:
    """B1: a signal on the final bar has no next bar to fill — no trade."""
    bt = Backtester(initial_capital=1_000_000)
    sig = _buy_signal()
    sig.date = synthetic_ohlcv.index[-1]
    res = bt.run(synthetic_ohlcv, [sig], "X", "Test")
    assert res.total_trades == 0
    assert res.final_capital == res.initial_capital


def test_backtest_stop_loss_priority_over_take_profit() -> None:
    """B2: same bar hitting both TP and SL must exit at SL (worst case first)."""
    n = 5
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {
            "Open": [100.0] * n,
            "High": [101.0, 101.0, 120.0, 101.0, 101.0],
            "Low": [99.0, 99.0, 90.0, 99.0, 99.0],
            "Close": [100.0] * n,
            "Volume": [500_000.0] * n,
        },
        index=dates,
    )
    # SL = 95 (5% hard stop), TP = 110 (10%). Bar 2 spans both (90..120).
    bt = Backtester(initial_capital=1_000_000, hard_stop_pct=0.05, take_profit_pct=0.10, slippage_pct=0.0)
    sig = _buy_signal(price=100, sl=95)
    sig.date = dates[0]
    res = bt.run(df, [sig], "X", "Test")
    assert res.total_trades == 1
    assert res.trades[0].exit_reason == "STOP_LOSS"
    # Gap-down exit at open would be 100; here low crosses SL → exit at SL
    assert res.trades[0].exit_price == 95.0


def test_backtest_gap_down_exits_at_open() -> None:
    """B2: bar opens below SL → exit at the open, not the SL price."""
    n = 5
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {
            "Open": [100.0, 100.0, 80.0, 100.0, 100.0],
            "High": [101.0, 101.0, 90.0, 101.0, 101.0],
            "Low": [99.0, 99.0, 79.0, 99.0, 99.0],
            "Close": [100.0, 100.0, 85.0, 100.0, 100.0],
            "Volume": [500_000.0] * n,
        },
        index=dates,
    )
    bt = Backtester(initial_capital=1_000_000, hard_stop_pct=0.05, slippage_pct=0.0)
    sig = _buy_signal(price=100, sl=95)
    sig.date = dates[0]
    res = bt.run(df, [sig], "X", "Test")
    assert res.trades[0].exit_reason == "STOP_LOSS"
    assert res.trades[0].exit_price == 80.0  # min(open=80, SL=95)


def test_backtest_slippage_makes_fills_worse(synthetic_ohlcv: pd.DataFrame) -> None:
    """P6: slippage raises entry fills and lowers exit fills."""
    sig = _buy_signal(price=1000, sl=None)
    sig.date = synthetic_ohlcv.index[10]

    bt_clean = Backtester(
        initial_capital=1_000_000, hard_stop_pct=0.99, commission_pct=0.0, slippage_pct=0.0
    )
    bt_slip = Backtester(
        initial_capital=1_000_000, hard_stop_pct=0.99, commission_pct=0.0, slippage_pct=0.01
    )
    r_clean = bt_clean.run(synthetic_ohlcv, [sig], "X", "Test")
    r_slip = bt_slip.run(synthetic_ohlcv, [sig], "X", "Test")
    assert r_clean.total_trades == r_slip.total_trades == 1

    entry_clean = r_clean.trades[0].entry_price
    entry_slip = r_slip.trades[0].entry_price
    assert entry_slip > entry_clean  # buy fills worse

    exit_slip = r_slip.trades[0].exit_price
    assert exit_slip < r_clean.trades[0].exit_price  # sell fills worse


def test_backtest_defaults_include_costs() -> None:
    """P6: default backtest is not free — commission + slippage on by default."""
    bt = Backtester()
    assert bt.commission_pct == 0.001
    assert bt.slippage_pct == 0.001
    with pytest.raises(ValueError):
        Backtester(slippage_pct=1.5)


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


def test_portfolio_update_prices_empty_does_not_crash(tmp_path: Path) -> None:
    """C1: empty portfolio must not blow up ThreadPoolExecutor(0)."""
    from src.stock_screener import portfolio as pmod

    monkey = pmod.PORTFOLIO_FILE
    pmod.PORTFOLIO_FILE = tmp_path / "pf_empty.json"
    try:
        pf = PortfolioTracker(total_capital=1_000_000)
        assert len(pf.positions) == 0
        pf.update_prices()  # was: ValueError: max_workers must be > 0
    finally:
        pmod.PORTFOLIO_FILE = monkey


def test_portfolio_close_negative_price_keeps_position(tmp_path: Path) -> None:
    """H3: invalid exit price raises BEFORE state mutation — the position
    must survive a failed close (was: popped first, then lost forever)."""
    from src.stock_screener import portfolio as pmod

    monkey = pmod.PORTFOLIO_FILE
    pmod.PORTFOLIO_FILE = tmp_path / "pf_neg.json"
    try:
        rm = RiskManager(total_capital=1_000_000)
        plan = rm.calculate_position(_buy_signal())
        pf = PortfolioTracker(total_capital=1_000_000)
        pf.add_position(plan)
        with pytest.raises(ValueError):
            pf.close_position("7203", exit_price=-100, reason="MANUAL")
        assert "7203" in pf.positions
        assert len(pf.closed_trades) == 0
    finally:
        pmod.PORTFOLIO_FILE = monkey


def test_check_batch_empty_tickers_returns_empty() -> None:
    """C2: empty ticker list must return {} instead of crashing."""
    from src.stock_screener.earnings_calendar import EarningsCalendar

    cal = EarningsCalendar(loader=None)
    assert cal.check_batch([]) == {}


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


def test_fund_cache_v2_preserves_high_yield(tmp_cache: Path) -> None:
    """B3: a genuine dividend yield > 10% must not be divided by 100."""
    loader = YFinanceDataLoader()
    loader._write_fund_cache("7203.T", {"dividend_yield": 0.15})
    assert (tmp_cache / "fundamentals" / "7203_T.v2.json").exists()
    cached = loader._read_fund_cache("7203.T")
    assert cached["dividend_yield"] == 0.15


def test_fund_cache_ignores_legacy_v1(tmp_cache: Path) -> None:
    """B3: legacy v1 caches (no version suffix) are never read."""
    import json
    from datetime import datetime, timedelta

    loader = YFinanceDataLoader()
    legacy = tmp_cache / "fundamentals" / "7203_T.json"
    legacy.write_text(
        json.dumps({
            "_cached_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            "dividend_yield": 3.72,
        })
    )
    assert loader._read_fund_cache("7203.T") is None


def test_fund_cache_write_removes_legacy(tmp_cache: Path) -> None:
    """B3: writing v2 cleans up the stale legacy file."""
    loader = YFinanceDataLoader()
    legacy = tmp_cache / "fundamentals" / "7203_T.json"
    legacy.write_text("{}")
    loader._write_fund_cache("7203.T", {"dividend_yield": 0.03})
    assert not legacy.exists()


def test_intraday_cache_has_short_ttl(tmp_cache: Path) -> None:
    """B4: caches whose range ends today refresh intraday (30 min)."""
    import os
    from datetime import datetime, timedelta

    loader = YFinanceDataLoader()
    from src.stock_screener.data_loader import jst_now

    today = jst_now().strftime("%Y-%m-%d")
    path = tmp_cache / f"7203_T_2024-01-01_{today}_1d.parquet"
    path.write_bytes(b"x")

    # 2h old but well within the 24h window → stale for today's range
    old = datetime.now() - timedelta(hours=2)
    os.utime(path, (old.timestamp(), old.timestamp()))
    assert not loader._is_cache_fresh(path)

    # Fresh mtime → fresh
    os.utime(path, None)
    assert loader._is_cache_fresh(path)


def test_historical_cache_keeps_24h_ttl(tmp_cache: Path) -> None:
    """B4: historical ranges keep the full 24h TTL."""
    import os
    from datetime import datetime, timedelta

    loader = YFinanceDataLoader()
    path = tmp_cache / "7203_T_2024-01-01_2024-06-30_1d.parquet"
    path.write_bytes(b"x")
    old = datetime.now() - timedelta(hours=2)
    os.utime(path, (old.timestamp(), old.timestamp()))
    assert loader._is_cache_fresh(path)  # 2h < 24h


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


def test_screen_chain_weighted_normalization_consistent() -> None:
    """P2: sub-scores and composite use the same weighted normalization —
    previously fundamental/technical were plain averages while composite was
    weight-normalized, so the same raw inputs produced inconsistent rankings."""
    chainer = ScreenChainer()
    fund = {"roe": 0.9, "pe": 0.8, "dividend": 0.7}
    fs = chainer._weighted_avg(fund, chainer._FUND_FACTORS)
    expected = (0.25 * 0.9 + 0.20 * 0.8 + 0.10 * 0.7) / 0.55
    assert fs == pytest.approx(expected)

    # A single perfect factor can no longer max the sub-score (bucket-weighted)
    partial = chainer._weighted_avg({"roe": 1.0}, chainer._FUND_FACTORS)
    assert partial == pytest.approx(0.25 / 0.55)


def test_screen_chain_custom_weights_scale_invariant() -> None:
    """M9: custom weight dicts are scale-invariant — doubling every weight
    must not change the score, so UI sliders that normalize the sum to 1
    are safe."""
    chainer = ScreenChainer()
    fund = {"roe": 0.9, "pe": 0.5, "dividend": 0.6}
    custom = {"roe": 0.10, "pe": 0.20, "dividend": 0.15,
              "trend": 0.25, "volume": 0.15, "rsi": 0.15}
    scaled = {k: v * 2 for k, v in custom.items()}
    assert chainer._weighted_avg(fund, custom) == pytest.approx(
        chainer._weighted_avg(fund, scaled)
    )


def test_screen_chain_nonpositive_pe_scores_zero() -> None:
    """P3: PE of 0 or negative (no earnings) must score 0, not 1.0 — a
    loss-maker with zero reported P/E is not a value bargain."""
    chainer = ScreenChainer()
    s_zero = chainer._compute_fundamental_score({"roe": 0.2, "pe": 0})
    assert s_zero["pe"] == 0.0
    s_neg = chainer._compute_fundamental_score({"roe": 0.2, "pe": -5.0})
    assert s_neg["pe"] == 0.0
    s_missing = chainer._compute_fundamental_score({"roe": 0.2})
    assert s_missing["pe"] == 0.0
    s_normal = chainer._compute_fundamental_score({"roe": 0.2, "pe": 10.0})
    assert s_normal["pe"] == pytest.approx(0.8)


def test_screen_chain_nan_metrics_do_not_poison_score() -> None:
    """P3: NaN metric values (pandas fills missing columns with NaN) must
    behave like missing data — score 0 — never NaN, never max."""
    chainer = ScreenChainer()
    s = chainer._compute_fundamental_score(
        {"roe": 0.2, "pe": 10.0, "dividend_yield": float("nan")}
    )
    assert s["dividend"] == 0.0
    assert not any(v != v for v in s.values())  # no NaN in any score


class _UptrendStubLoader:
    """Stub loader: identical uptrend OHLCV for every ticker; fundamentals
    supplied per ticker."""

    def __init__(self, fundies: dict[str, dict]) -> None:
        self._fundies = fundies

    def fetch_batch_fundamentals(self, tickers: list[str]) -> dict[str, dict]:
        return {t: dict(self._fundies.get(t, {})) for t in tickers}

    def fetch_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        import numpy as np

        n = 260
        dates = pd.date_range(end="2024-06-24", periods=n, freq="B")
        close = np.linspace(150, 400, n)
        return pd.DataFrame(
            {
                "Open": close + 0.5,
                "High": close + 2.0,
                "Low": close - 1.0,
                "Close": close,
                "Volume": np.full(n, 500_000.0),
            },
            index=dates,
        )


def test_screen_chain_missing_data_penalized() -> None:
    """P3: identical fundamentals/technical except missing dividend — the
    incomplete stock must rank BELOW, not above, the complete one."""
    from src.stock_screener.fundamental_screener import Condition

    base = {"roe": 0.20, "pe": 10.0, "pb": 1.5, "eps": 100.0, "dividend_yield": 0.03}
    complete = dict(base)
    missing_dividend = {k: v for k, v in base.items() if k != "dividend_yield"}

    loader = _UptrendStubLoader({"A": complete, "B": missing_dividend})
    chainer = ScreenChainer(loader=loader)
    out = chainer.run(
        ["A", "B"],
        fundamental_conditions=[Condition("roe", ">", 0.0)],
        top_n=10,
    )
    by_ticker = {s.ticker: s for s in out}
    assert set(by_ticker) == {"A", "B"}
    # Complete data scores higher with identical technicals
    assert by_ticker["A"].score > by_ticker["B"].score
    # Technical sub-scores identical (P2: same normalization, same inputs)
    assert by_ticker["A"].technical_score == by_ticker["B"].technical_score
    # Missing dividend drags the fundamental sub-score (0.25+0.20 over 0.55)
    assert by_ticker["B"].fundamental_score < by_ticker["A"].fundamental_score


# --- trailing stop ---


def test_trailing_stop_manager_update_moves_stop_up() -> None:
    from src.stock_screener.risk_management import TrailingStopManager

    mgr = TrailingStopManager(trail_pct=0.05)
    mgr.open("7203", entry_price=1000, initial_stop=950)
    # Price rises → stop should move up
    new_stop = mgr.update("7203", current_price=1050)
    assert new_stop is not None
    assert new_stop > 950  # stop moved up
    assert new_stop == round(1050 * 0.95, 2)  # 997.5


def test_trailing_stop_manager_stop_does_not_move_down() -> None:
    from src.stock_screener.risk_management import TrailingStopManager

    mgr = TrailingStopManager(trail_pct=0.05)
    mgr.open("7203", entry_price=1000, initial_stop=950)
    mgr.update("7203", current_price=1050)  # stop moves to 997.5
    new_stop = mgr.update("7203", current_price=1020)  # price drops
    assert new_stop == 997.5  # stop stays


def test_trailing_stop_manager_check_exit() -> None:
    from src.stock_screener.risk_management import TrailingStopManager

    mgr = TrailingStopManager(trail_pct=0.05)
    mgr.open("7203", entry_price=1000, initial_stop=950)
    mgr.update("7203", current_price=1050)
    # Price drops below trailing stop
    assert mgr.check_exit("7203", current_price=990) == "TRAILING_STOP"
    assert mgr.check_exit("7203", current_price=1000) is None


def test_trailing_stop_manager_close_removes_position() -> None:
    from src.stock_screener.risk_management import TrailingStopManager

    mgr = TrailingStopManager(trail_pct=0.05)
    mgr.open("7203", entry_price=1000)
    closed = mgr.close("7203")
    assert closed is not None
    assert mgr.get_state("7203") is None


def test_trailing_stop_manager_active_positions() -> None:
    from src.stock_screener.risk_management import TrailingStopManager

    mgr = TrailingStopManager(trail_pct=0.05)
    mgr.open("7203", entry_price=1000)
    mgr.open("6758", entry_price=2000)
    assert set(mgr.active_positions) == {"7203", "6758"}
    mgr.close("7203")
    assert mgr.active_positions == ["6758"]


def test_trailing_stop_manager_validates_pct() -> None:
    from src.stock_screener.risk_management import TrailingStopManager

    with pytest.raises(ValueError):
        TrailingStopManager(trail_pct=0)
    with pytest.raises(ValueError):
        TrailingStopManager(trail_pct=1.5)


def test_batch_positions_skips_sell_signals() -> None:
    """batch_positions should skip SELL signals, not crash."""
    from src.stock_screener.technical_engine import Signal, SignalType

    rm = RiskManager(total_capital=1_000_000)
    buy = Signal(ticker="7203", signal_type=SignalType.BUY, strategy="test",
                 date=datetime(2025, 1, 1), price=2500, stop_loss=2300)
    sell = Signal(ticker="6758", signal_type=SignalType.SELL, strategy="test",
                  date=datetime(2025, 1, 1), price=12000, stop_loss=13000)
    plans = rm.batch_positions([buy, sell])
    assert len(plans) == 1
    assert plans[0].ticker == "7203"


def test_batch_positions_max_positions_cap() -> None:
    """max_positions caps the number of plans (cheapest-first)."""
    from src.stock_screener.technical_engine import Signal, SignalType

    rm = RiskManager(total_capital=100_000_000)
    sigs = [
        Signal(ticker=f"100{i}", signal_type=SignalType.BUY, strategy="test",
               date=datetime(2025, 1, 1), price=100 + i * 10, stop_loss=90)
        for i in range(5)
    ]
    plans = rm.batch_positions(sigs, max_positions=2)
    assert len(plans) == 2
    assert [p.ticker for p in plans] == ["1000", "1001"]


def test_batch_positions_max_positions_zero_unlimited() -> None:
    """max_positions=0 means unlimited (backward compatible default)."""
    from src.stock_screener.technical_engine import Signal, SignalType

    rm = RiskManager(total_capital=100_000_000)
    sigs = [
        Signal(ticker=f"200{i}", signal_type=SignalType.BUY, strategy="test",
               date=datetime(2025, 1, 1), price=100, stop_loss=90)
        for i in range(5)
    ]
    assert len(rm.batch_positions(sigs, max_positions=0)) == 5
    assert len(rm.batch_positions(sigs)) == 5


def test_batch_positions_negative_max_positions_raises() -> None:
    """Negative max_positions must raise ValueError."""
    from src.stock_screener.technical_engine import Signal, SignalType

    rm = RiskManager(total_capital=100_000_000)
    sig = Signal(ticker="7203", signal_type=SignalType.BUY, strategy="test",
                 date=datetime(2025, 1, 1), price=2500, stop_loss=2300)
    with pytest.raises(ValueError, match="max_positions"):
        rm.batch_positions([sig], max_positions=-1)


def test_jst_now_returns_japan_time() -> None:
    """M5: jst_now must be Asia/Tokyo regardless of server timezone —
    a UTC host would otherwise pick the wrong trading day."""
    from src.stock_screener.data_loader import jst_now

    now = jst_now()
    assert now.tzinfo is not None
    assert now.utcoffset() is not None
    assert now.utcoffset().total_seconds() == 9 * 3600
    assert now.tzinfo.key == "Asia/Tokyo"


# --- MEDIUM regression tests ---

def test_screen_chain_negative_top_n_raises() -> None:
    """M9: negative top_n must raise — a raw slice would treat scored[:-n]
    as "drop the last n" and return the WRONG stocks."""
    from src.stock_screener.fundamental_screener import Condition

    loader = _UptrendStubLoader({"A": {"roe": 0.20}})
    chainer = ScreenChainer(loader=loader)
    with pytest.raises(ValueError, match="top_n"):
        chainer.run(
            ["A"], fundamental_conditions=[Condition("roe", ">", 0.0)], top_n=-1
        )


def test_screen_chain_nan_volume_does_not_poison_score() -> None:
    """M8: NaN Volume on the last bar (suspended day) must not crash or
    yield a NaN score — the volume factor stays penalized instead."""

    class _NaNVolumeStubLoader:
        def fetch_batch_fundamentals(self, tickers):
            return {t: {"roe": 0.20} for t in tickers}

        def fetch_ohlcv(self, ticker, start, end):
            import numpy as np

            n = 260
            dates = pd.date_range(end="2024-06-24", periods=n, freq="B")
            close = np.linspace(150, 400, n)
            vol = np.full(n, 500_000.0)
            vol[-1] = np.nan
            return pd.DataFrame(
                {
                    "Open": close + 0.5,
                    "High": close + 2.0,
                    "Low": close - 1.0,
                    "Close": close,
                    "Volume": vol,
                },
                index=dates,
            )

    from src.stock_screener.fundamental_screener import Condition

    chainer = ScreenChainer(loader=_NaNVolumeStubLoader())
    out = chainer.run(
        ["A"], fundamental_conditions=[Condition("roe", ">", 0.0)], top_n=1
    )
    assert len(out) == 1
    assert out[0].score == out[0].score  # not NaN
    assert 0.0 <= out[0].score <= 1.0


def _make_ohlcv(close: list[float], freq: str = "B") -> pd.DataFrame:
    """Synthetic OHLCV from a close-price series."""
    import numpy as np

    n = len(close)
    dates = pd.date_range(end="2024-06-24", periods=n, freq=freq)
    c = np.array(close, dtype=float)
    return pd.DataFrame(
        {
            "Open": c + 0.5,
            "High": c + 2.0,
            "Low": c - 1.0,
            "Close": c,
            "Volume": np.full(n, 500_000.0),
        },
        index=dates,
    )


class _SellStrategy:
    """Emits a single SELL signal on the last bar."""

    def generate_signals(self, df, ticker):
        return [
            Signal(
                ticker=ticker,
                signal_type=SignalType.SELL,
                strategy="Test",
                date=df.index[-1],
                price=float(df["Close"].iloc[-1]),
                stop_loss=None,
            )
        ]


def test_multi_timeframe_sell_not_boosted_by_weekly_up() -> None:
    """M12: a bullish weekly trend must NOT boost a SELL signal — a
    contradictory confirmation would push SELLs over min_confidence."""
    from src.stock_screener.multi_timeframe import MultiTimeframeConfirmer

    daily = TechnicalEngine().enrich(_make_ohlcv(list(range(150, 300))), sma_periods=(50,))
    weekly_up = TechnicalEngine().enrich(
        _make_ohlcv(list(range(100, 400, 10)), freq="W-FRI"), sma_periods=(20,)
    )

    confirmer = MultiTimeframeConfirmer(
        strategies=[_SellStrategy()], min_confidence=0.6
    )
    confirmed = confirmer.confirm("7203", daily, weekly_up)
    # SELL confidence stays 0.5 (base) — no weekly/daily boost — below 0.6
    assert confirmed == []


def test_multi_timeframe_sell_boosted_by_weekly_down() -> None:
    """M12: a bearish weekly trend aligns with a SELL — +0.3 applies and
    the SELL clears min_confidence."""
    from src.stock_screener.multi_timeframe import MultiTimeframeConfirmer

    daily = TechnicalEngine().enrich(_make_ohlcv(list(range(150, 300))), sma_periods=(50,))
    weekly_down = TechnicalEngine().enrich(
        _make_ohlcv(list(range(400, 100, -10)), freq="W-FRI"), sma_periods=(20,)
    )

    confirmer = MultiTimeframeConfirmer(
        strategies=[_SellStrategy()], min_confidence=0.6
    )
    confirmed = confirmer.confirm("7203", daily, weekly_down)
    assert len(confirmed) == 1
    assert confirmed[0].signal_type == SignalType.SELL
    assert confirmed[0].confidence == 0.8


def test_profit_target_negative_pct_raises() -> None:
    """M13: target_pct <= -100 would produce a zero/negative exit price."""
    from src.stock_screener.profit_target import calculate_exit_price

    with pytest.raises(ValueError, match="target_pct"):
        calculate_exit_price(1000.0, -100.0)
    with pytest.raises(ValueError, match="target_pct"):
        calculate_exit_price(1000.0, -150.0)
    assert calculate_exit_price(1000.0, -50.0) == 500.0


def test_fundamental_condition_nan_fails_neq() -> None:
    """M14: NaN metric must fail EVERY condition — NaN != x is always True
    and would otherwise pass a "!= threshold" filter."""
    from src.stock_screener.fundamental_screener import Condition

    assert not Condition("pe", "!=", 10.0).evaluate({"pe": float("nan")})
    assert not Condition("pe", ">", 0.0).evaluate({"pe": float("nan")})
    assert not Condition("pe", "<", 1000.0).evaluate({"pe": float("nan")})
    assert Condition("pe", "!=", 10.0).evaluate({"pe": 12.0})


def test_portfolio_skips_zero_share_plan() -> None:
    """M15: a 0-share plan (RiskManager's no-capital fallback) must not
    create a phantom position."""
    from src.stock_screener.risk_management import PositionPlan

    tracker = PortfolioTracker()
    zero = PositionPlan(
        ticker="7203",
        entry_price=1000.0,
        stop_loss=900.0,
        shares=0,
        position_value=0.0,
        risk_amount=0.0,
        risk_pct=0.0,
        strategy="Test",
    )
    tracker.add_position(zero, sector="Auto")
    assert "7203" not in tracker.positions


def test_risk_manager_caps_shares_at_full_capital() -> None:
    """M6: position value must never exceed total capital — risk-based
    sizing is capped by total_capital / entry_price."""
    rm = RiskManager(total_capital=100_000.0, risk_per_trade=0.5)
    plan = rm.calculate_position(_buy_signal(price=1000.0, sl=950.0))
    assert plan.shares * plan.entry_price <= rm.total_capital + 1e-9
    # risk-based shares (1000) exceed the cap (100) -> capped
    assert plan.shares == 100


def test_risk_manager_nan_price_raises() -> None:
    """M7: NaN signal price must raise ValueError — silently sizing on
    NaN would poison position_value and risk calculations."""
    from src.stock_screener.technical_engine import Signal

    rm = RiskManager(total_capital=100_000.0)
    nan_sig = Signal(
        ticker="7203",
        signal_type=SignalType.BUY,
        strategy="Test",
        date=datetime(2024, 1, 1),
        price=float("nan"),
        stop_loss=900.0,
    )
    with pytest.raises(ValueError, match="price"):
        rm.calculate_position(nan_sig)


def test_cache_path_no_collision() -> None:
    """M18: tickers "X.Y" and "X_Y" must map to distinct cache files —
    the old dot->underscore mapping collapsed them into one file."""
    loader = YFinanceDataLoader()
    assert loader._cache_path("X.Y", "s", "e", "1d") != loader._cache_path(
        "X_Y", "s", "e", "1d"
    )


# --- LOW-severity regressions (auth / user_store / data_loader) ---


def test_hash_password_rejects_over_72_bytes():
    with pytest.raises(ValueError):
        auth.hash_password("x" * 73)


def test_verify_password_malformed_hash_false():
    assert auth.verify_password("short", "$2b$12$invalid-hash$") is False
    assert auth.verify_password("short", "") is False


def test_verify_password_dummy_hash_returns_false():
    assert auth.verify_password("anything", auth._DUMMY_HASH) is False


def test_watchlist_t_suffix_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(user_store.db, "DB_PATH", tmp_path / "t.db")
    with user_store.db.connect() as conn:
        conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)", ("lowuser", "x", "now"))
        uid = conn.execute("SELECT id FROM users WHERE username='lowuser'").fetchone()[0]
    user_store.add_to_watchlist(uid, "7203")
    user_store.add_to_watchlist(uid, "7203.T")
    user_store.add_to_watchlist(uid, " 7974 ")
    assert user_store.get_watchlist(uid) == ["7203", "7974"]
    user_store.remove_from_watchlist(uid, "7203.T")
    assert user_store.get_watchlist(uid) == ["7974"]


def test_target_rows_t_suffix_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(user_store.db, "DB_PATH", tmp_path / "t.db")
    with user_store.db.connect() as conn:
        conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)", ("lowuser2", "x", "now"))
        uid = conn.execute("SELECT id FROM users WHERE username='lowuser2'").fetchone()[0]
    user_store.save_target_rows(uid, [{"ticker": "7203.T", "entry_price": 100, "target_pct": 10, "shares": 5}])
    rows = user_store.get_target_rows(uid)
    assert rows[0]["ticker"] == "7203"


def test_failed_fundamentals_not_cached(monkeypatch):
    calls = {"n": 0}

    class _BoomTicker:
        def __init__(self, *_a, **_k):
            calls["n"] += 1
            raise RuntimeError("network down")

    import sys

    monkeypatch.setitem(sys.modules, "yfinance", type("yf", (), {"Ticker": _BoomTicker}))
    loader = YFinanceDataLoader()
    res = loader.fetch_fundamentals("9999")
    assert res["pe"] is None
    assert loader._read_fund_cache("9999") is None
    assert calls["n"] == 1
    res2 = loader.fetch_fundamentals("9999")
    assert res2["pe"] is None
    assert calls["n"] == 2


# --- NaN-price defense regressions (loader / strategies / backtest) ---


def test_fetch_ohlcv_drops_nan_close_rows(monkeypatch):
    import sys

    df = _make_ohlcv([1000.0 + i for i in range(10)])
    df.loc[df.index[3], "Close"] = float("nan")
    df.loc[df.index[3], "Volume"] = float("nan")

    class _Yf:
        @staticmethod
        def download(*_a, **_k):
            return df

    monkeypatch.setitem(sys.modules, "yfinance", type("yf", (), {"download": _Yf.download}))
    loader = YFinanceDataLoader()
    out = loader.fetch_ohlcv("7203", "2026-01-01", "2026-01-10")
    assert out["Close"].isna().sum() == 0
    assert len(out) == 9


def test_corrupt_cache_refetches(monkeypatch):
    import sys

    loader = YFinanceDataLoader()
    norm = loader.normalize_ticker("7203")
    start, end, interval = "2026-01-01", "2026-08-17", "1d"
    path = loader._cache_path(norm, start, end, interval)
    path.write_text("GARBAGE-NOT-PARQUET")

    class _Yf:
        @staticmethod
        def download(*_a, **_k):
            return _make_ohlcv([1000.0 + i for i in range(12)])

    monkeypatch.setitem(sys.modules, "yfinance", type("yf", (), {"download": _Yf.download}))
    out = loader.fetch_ohlcv("7203", start, end, interval)
    assert len(out) == 12
    assert path.exists() and path.read_bytes().startswith(b"PAR1")
    assert not path.with_suffix(".parquet.tmp").exists()


def test_volume_breakout_never_emits_nan_price(monkeypatch):
    n = 230
    df = pd.DataFrame(
        {
            "Open": [1000 + i for i in range(n)],
            "High": [1000.1 + i for i in range(n)],
            "Low": [999 + i for i in range(n)],
            "Adj Close": [1000 + i for i in range(n)],
            "Close": [1000 + i for i in range(n)],
            "Volume": [1_000_000 * (1.05 ** i) for i in range(n)],
        },
        index=pd.date_range("2026-01-01", periods=n, freq="B"),
    )
    engine = TechnicalEngine()
    enriched = engine.enrich(df)
    enriched.loc[enriched.index[210], "Close"] = float("nan")
    enriched.loc[enriched.index[210], "Volume"] = float("nan")

    signals = VolumeBreakoutStrategy().generate_signals(enriched, "7203")
    assert signals
    assert all(s.price == s.price for s in signals), "NaN-priced signal emitted"
    assert not any(pd.isna(s.price) for s in signals)


def test_backtest_nan_final_close_no_nan_pnl(monkeypatch):
    n = 30
    df = pd.DataFrame(
        {
            "Open": [1000 + i for i in range(n)],
            "High": [1000.1 + i for i in range(n)],
            "Low": [999 + i for i in range(n)],
            "Adj Close": [1000 + i for i in range(n)],
            "Close": [1000 + i for i in range(n)],
            "Volume": [1_000_000] * n,
        },
        index=pd.date_range("2026-01-01", periods=n, freq="B"),
    )
    df.loc[df.index[-1], "Close"] = float("nan")
    sig_date = df.index[5].to_pydatetime()
    signals = [_buy_signal("7203", price=float(df["Close"].iloc[4]), sl=900.0)]
    signals[0].date = sig_date
    bt = Backtester(initial_capital=1_000_000, risk_per_trade=0.5)
    res = bt.run(df, signals, "7203", "VolumeBreakout")
    assert not pd.isna(res.final_capital)
    assert not pd.isna(res.total_return_pct)
    assert not any(pd.isna(t.pnl) for t in res.trades)


# --- Round 3: NaN guards in portfolio / risk / profit_target ---


def test_portfolio_update_price_rejects_nan():
    pf = PortfolioTracker()
    plan = PositionPlan(
        ticker="7203", entry_price=1000.0, stop_loss=950.0, shares=10,
        position_value=10_000.0, risk_amount=500.0, risk_pct=0.01,
        strategy="VolumeBreakout",
    )
    pf.add_position(plan)
    with pytest.raises(ValueError):
        pf.positions["7203"].update_price(float("nan"))
    with pytest.raises(ValueError):
        pf.close_position("7203", float("nan"))
    with pytest.raises(ValueError):
        pf.close_position("7203", 0.0)
    assert "7203" in pf.positions


def test_profit_target_rejects_nan_entry():
    from src.stock_screener.profit_target import TargetRow

    with pytest.raises(ValueError):
        TargetRow(ticker="7203", entry_price=float("nan"))


def test_risk_nan_price_no_false_trigger():
    from src.stock_screener.risk_management import TrailingStopManager
    from src.stock_screener.technical_engine import Signal, SignalType

    rm = RiskManager(total_capital=1_000_000)
    plan = rm.calculate_position(
        Signal(
            ticker="7203", signal_type=SignalType.BUY, strategy="VolumeBreakout",
            date=datetime.now(), price=1000.0, stop_loss=950.0,
        )
    )
    assert rm.check_stop_loss(plan, float("nan")) is None

    tsm = TrailingStopManager(trail_pct=0.05)
    tsm.open("7203", entry_price=1000.0, initial_stop=950.0)
    assert tsm.update("7203", float("nan")) == 950.0
    assert tsm.check_exit("7203", float("nan")) is None
