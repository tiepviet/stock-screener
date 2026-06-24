"""
Streamlit Dashboard — TSE Stock Screener & Signal Viewer.

Run: streamlit run app.py

Features:
  - Interactive candlestick chart (plotly)
  - Fundamental screener with configurable filters
  - Technical signal scanner (VolumeBreakout + PullbackMA)
  - Position sizing calculator
  - Backtesting engine
  - Portfolio tracker
  - Earnings calendar check
  - Multi-pass screen chaining
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.stock_screener.alert import AlertScanner, SlackSender, TelegramSender
from src.stock_screener.backtest import Backtester

# Local modules
from src.stock_screener.data_loader import YFinanceDataLoader
from src.stock_screener.earnings_calendar import EarningsCalendar
from src.stock_screener.fundamental_screener import Condition, FundamentalScreener
from src.stock_screener.multi_timeframe import MultiTimeframeConfirmer
from src.stock_screener.portfolio import PortfolioTracker
from src.stock_screener.risk_management import RiskManager
from src.stock_screener.screen_chain import ScreenChainer
from src.stock_screener.technical_engine import (
    PullbackMAStrategy,
    TechnicalEngine,
    VolumeBreakoutStrategy,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="TSE Stock Screener",
    page_icon="JP",
    layout="wide",
)

st.title("Tokyo Stock Exchange — Screener & Signal Dashboard")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

loader = YFinanceDataLoader()
engine = TechnicalEngine()

# ---------------------------------------------------------------------------
# Background auto-scan thread
# ---------------------------------------------------------------------------

_auto_scan_thread: threading.Thread | None = None
_auto_scan_stop = threading.Event()
_auto_scan_last: str = ""
_auto_scan_lock = threading.Lock()

DEFAULT_ALERT_TICKERS = [
    "7203", "6758", "9984", "8306", "6501",
    "7267", "9434", "6861", "8411", "7751",
]


def _auto_scan_worker(
    tickers: list[str],
    lookback_days: int,
    interval_min: int,
) -> None:
    """Background thread: scan tickers and send alerts every interval_min."""
    scanner = AlertScanner(tickers=tickers, lookback_days=lookback_days)
    logger.info("Auto-scan thread started (interval=%d min)", interval_min)

    while not _auto_scan_stop.is_set():
        try:
            results = scanner.scan_and_alert()
            total = sum(len(v) for v in results.values())
            now_str = datetime.now().strftime("%H:%M")
            with _auto_scan_lock:
                global _auto_scan_last  # noqa: PLW0602
                _auto_scan_last = f"{now_str} — {total} signals"
            logger.info("Auto-scan complete: %d signals", total)
        except Exception:
            logger.exception("Auto-scan failed")

        _auto_scan_stop.wait(interval_min * 60)


def start_auto_scan(
    tickers: list[str] | None = None,
    lookback_days: int = 365,
    interval_min: int = 30,
) -> None:
    """Start background auto-scan thread if not already running."""
    global _auto_scan_thread  # noqa: PLW0602
    if _auto_scan_thread and _auto_scan_thread.is_alive():
        logger.info("Auto-scan already running")
        return
    _auto_scan_stop.clear()
    _auto_scan_thread = threading.Thread(
        target=_auto_scan_worker,
        args=(tickers or DEFAULT_ALERT_TICKERS, lookback_days, interval_min),
        daemon=True,
    )
    _auto_scan_thread.start()


def stop_auto_scan() -> None:
    """Stop background auto-scan thread."""
    _auto_scan_stop.set()


# ---------------------------------------------------------------------------
# Sidebar — Global params
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Settings")
    capital = st.number_input("Total Capital (JPY)", value=10_000_000, step=1_000_000)
    risk_pct = st.slider("Risk per Trade (%)", 0.5, 5.0, 1.0, 0.1) / 100
    hard_stop = st.slider("Hard Stop-Loss (%)", 3.0, 10.0, 7.0, 0.5) / 100
    lookback_days = st.slider("Data Lookback (days)", 90, 730, 365, 30)

    st.markdown("---")
    st.markdown("### Alert Channels")
    tg_ok = bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))
    sl_ok = bool(os.environ.get("SLACK_WEBHOOK_URL"))
    st.markdown(f"- Telegram: {'✅' if tg_ok else '❌'}")
    st.markdown(f"- Slack: {'✅' if sl_ok else '❌'}")
    if not tg_ok and not sl_ok:
        st.caption("Set Streamlit Secrets or .env to enable alerts")

    st.markdown("---")
    st.markdown("### Auto Scan")
    auto_scan_on = st.checkbox("Auto Scan", value=False, key="auto_scan_toggle")
    scan_interval = st.selectbox("Interval", [15, 30, 60], index=1, key="scan_interval", disabled=not auto_scan_on)

    if auto_scan_on:
        running = _auto_scan_thread and _auto_scan_thread.is_alive()
        st.markdown(f"- Status: **{'🟢 Running' if running else '🔴 Starting...'}**")
        with _auto_scan_lock:
            if _auto_scan_last:
                st.caption(f"Last scan: {_auto_scan_last}")
        if not running:
            start_auto_scan(
                tickers=DEFAULT_ALERT_TICKERS,
                lookback_days=365,
                interval_min=scan_interval,
            )
            st.rerun()
    else:
        if _auto_scan_thread and _auto_scan_thread.is_alive():
            stop_auto_scan()


# ---------------------------------------------------------------------------
# Tab layout
# ---------------------------------------------------------------------------

tab_chart, tab_screen, tab_signals, tab_backtest, tab_portfolio, tab_earnings, tab_chain, tab_mtf, tab_alerts, tab_guide = st.tabs(
    ["Chart", "Screener", "Signals", "Backtest", "Portfolio", "Earnings", "Smart Screen", "MTF", "Alerts", "Guide"]
)


# ============================
# TAB 1: Chart
# ============================

with tab_chart:
    st.subheader("Candlestick Chart")

    col1, col2 = st.columns([3, 1])
    with col2:
        chart_ticker = st.text_input("Ticker", value="7203", key="chart_ticker")
        chart_interval = st.selectbox("Interval", ["1d", "1h", "1wk"], index=0)
        show_sma = st.checkbox("Show SMA", value=True)
        sma_period = st.selectbox("SMA Period", [20, 50, 200], index=0)
        show_vol = st.checkbox("Show Volume", value=True)

    with col1:
        if st.button("Load Chart", key="load_chart"):
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
            try:
                df = loader.fetch_ohlcv(chart_ticker, start, end, chart_interval)
                df = engine.enrich(df)

                fig = go.Figure()

                # Candlestick
                fig.add_trace(
                    go.Candlestick(
                        x=df.index,
                        open=df["Open"],
                        high=df["High"],
                        low=df["Low"],
                        close=df["Close"],
                        name="OHLC",
                    )
                )

                # SMA overlay
                if show_sma:
                    sma_col = f"SMA_{sma_period}"
                    if sma_col in df.columns:
                        fig.add_trace(
                            go.Scatter(
                                x=df.index,
                                y=df[sma_col],
                                name=f"SMA {sma_period}",
                                line=dict(width=1, dash="dot"),
                            )
                        )

                fig.update_layout(
                    title=f"{loader.normalize_ticker(chart_ticker)} — {chart_interval}",
                    xaxis_title="Date",
                    yaxis_title="Price (JPY)",
                    xaxis_rangeslider_visible=False,
                    height=550,
                    template="plotly_dark",
                )

                st.plotly_chart(fig, width='stretch')

                # Volume subplot
                if show_vol:
                    vol_fig = go.Figure()
                    vol_fig.add_trace(
                        go.Bar(
                            x=df.index,
                            y=df["Volume"],
                            name="Volume",
                            marker_color="rgba(100, 149, 237, 0.6)",
                        )
                    )
                    vol_fig.update_layout(
                        title="Volume",
                        height=200,
                        template="plotly_dark",
                        margin=dict(t=30, b=10),
                    )
                    st.plotly_chart(vol_fig, width='stretch')

                # Data table
                with st.expander("Raw Data"):
                    st.dataframe(df.tail(50).round(2))

            except Exception as e:
                st.error(f"Error loading data: {e}")
                logger.exception("Chart load failed")


# ============================
# TAB 2: Screener
# ============================

with tab_screen:
    st.subheader("Fundamental Screener")

    col_a, col_b = st.columns(2)
    with col_a:
        min_roe = st.number_input("Min ROE (%)", value=8.0, step=1.0) / 100
        max_pe = st.number_input("Max P/E", value=20.0, step=1.0)
    with col_b:
        max_pb = st.number_input("Max P/B", value=2.0, step=0.5)
        min_div = st.number_input("Min Dividend Yield (%)", value=1.0, step=0.5) / 100

    tickers_input = st.text_area(
        "Tickers (one per line)",
        value="7203\n6758\n9984\n8306\n6501\n7267\n9434\n6861\n8411\n7751",
        height=200,
    )

    if st.button("Run Screener", key="run_screen"):
        tickers = [t.strip() for t in tickers_input.strip().splitlines() if t.strip()]
        conditions = [
            Condition("roe", ">", min_roe),
            Condition("pe", "<", max_pe),
            Condition("pb", "<", max_pb),
            Condition("dividend_yield", ">", min_div),
            Condition("eps", ">", 0),
        ]

        with st.spinner("Screening..."):
            screener = FundamentalScreener(loader)
            results = screener.screen(tickers, conditions)

        if results.empty:
            st.warning("No stocks passed all filters.")
        else:
            st.success(f"{len(results)} stocks passed")
            st.dataframe(
                results.style.format({
                    "pe": "{:.1f}",
                    "pb": "{:.2f}",
                    "roe": "{:.1%}",
                    "eps": "{:.2f}",
                    "dividend_yield": "{:.2%}",
                    "market_cap": "¥{:,.0f}",
                }),
                width='stretch',
            )


# ============================
# TAB 3: Signals
# ============================

with tab_signals:
    st.subheader("Technical Signal Scanner")

    col_x, col_y = st.columns(2)
    with col_x:
        vb_lookback = st.slider("Breakout Lookback", 10, 50, 20)
        vb_vol_mult = st.slider("Volume Multiplier", 1.0, 3.0, 1.2, 0.1)
    with col_y:
        pm_rsi_max = st.slider("Pullback Max RSI", 40, 80, 60)

    scan_tickers_input = st.text_area(
        "Tickers to Scan (one per line)",
        value="7203\n6758\n9984\n8306\n6501",
        height=150,
        key="scan_tickers",
    )

    if st.button("Scan Signals", key="scan_signals"):
        tickers = [t.strip() for t in scan_tickers_input.strip().splitlines() if t.strip()]
        if not tickers:
            st.warning("No tickers to scan")
            st.stop()

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        vb_strategy = VolumeBreakoutStrategy(
            lookback=vb_lookback, volume_mult=vb_vol_mult
        )
        pm_strategy = PullbackMAStrategy(rsi_max=pm_rsi_max)

        all_signals = []
        progress = st.progress(0)
        errors = []

        def _scan_one(ticker: str) -> list:
            try:
                df = loader.fetch_ohlcv(ticker, start, end)
                df = engine.enrich(df)
                sigs = []
                sigs.extend(vb_strategy.generate_signals(df, ticker))
                sigs.extend(pm_strategy.generate_signals(df, ticker))
                return sigs
            except Exception as e:
                return [("error", ticker, str(e))]

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(len(tickers), 8)) as pool:
            futures = {pool.submit(_scan_one, t): t for t in tickers}
            done_count = 0
            for future in as_completed(futures):
                result = future.result()
                if result and isinstance(result[0], tuple) and result[0][0] == "error":
                    _, ticker, msg = result[0]
                    errors.append(f"{ticker}: {msg}")
                else:
                    all_signals.extend(result)
                done_count += 1
                progress.progress(done_count / len(tickers))

        progress.empty()

        for err in errors:
            st.warning(f"Failed: {err}")

        if not all_signals:
            st.info("No signals found in this period.")
        else:
            st.success(f"{len(all_signals)} signals found")

            # Signal log
            st.subheader("Signal Log")
            for sig in all_signals:
                st.code(str(sig), language=None)

            # Position sizing
            st.subheader("Position Sizing")
            rm = RiskManager(capital, risk_pct, hard_stop)
            plans = rm.batch_positions(all_signals)

            if plans:
                plan_df = pd.DataFrame([vars(p) for p in plans])
                st.dataframe(
                    plan_df.style.format({
                        "entry_price": "¥{:.2f}",
                        "stop_loss": "¥{:.2f}",
                        "position_value": "¥{:,.0f}",
                        "risk_amount": "¥{:,.0f}",
                        "risk_pct": "{:.2%}",
                    }),
                    width='stretch',
                )

                # Summary
                total_alloc = sum(p.position_value for p in plans)
                total_risk = sum(p.risk_amount for p in plans)
                st.metric("Total Allocation", f"¥{total_alloc:,.0f}")
                st.metric("Total Risk", f"¥{total_risk:,.0f}")
                st.metric("Positions", len(plans))

                # --- Export + Add to portfolio ---
                csv = plan_df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    "Download plans CSV",
                    data=csv,
                    file_name=f"position_plans_{datetime.now():%Y%m%d_%H%M}.csv",
                    mime="text/csv",
                    key="dl_plans",
                )

                pf = PortfolioTracker(total_capital=capital, max_sector_pct=0.30)
                if st.button("Add all to portfolio", key="add_all_pf"):
                    added = 0
                    for plan in plans:
                        try:
                            pf.add_position(plan)
                            added += 1
                        except Exception as e:
                            logger.exception("add_position failed for %s", plan.ticker)
                            st.warning(f"{plan.ticker}: {e}")
                    st.success(f"Added {added}/{len(plans)} positions")
            else:
                st.info("No positions within capital limit.")

            # --- Signals CSV export ---
            if all_signals:
                sig_df = pd.DataFrame([{
                    "ticker": s.ticker,
                    "signal_type": s.signal_type.value,
                    "strategy": s.strategy,
                    "date": s.date,
                    "price": s.price,
                    "stop_loss": s.stop_loss,
                } for s in all_signals])
                st.download_button(
                    "Download signals CSV",
                    data=sig_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"signals_{datetime.now():%Y%m%d_%H%M}.csv",
                    mime="text/csv",
                    key="dl_sigs",
                )

            # Strategy breakdown
            st.subheader("By Strategy")
            for strat_name in sorted({s.strategy for s in all_signals}):
                count = sum(1 for s in all_signals if s.strategy == strat_name)
                st.write(f"- **{strat_name}**: {count} signals")


# ============================
# TAB 4: Backtest
# ============================

with tab_backtest:
    st.subheader("Backtesting Engine")

    col_b1, col_b2 = st.columns(2)
    with col_b1:
        bt_ticker = st.text_input("Ticker", value="7203", key="bt_ticker")
        bt_strategy = st.selectbox("Strategy", ["VolumeBreakout", "PullbackMA"], key="bt_strat")
        bt_capital = st.number_input("Initial Capital (JPY)", value=10_000_000, step=1_000_000, key="bt_cap")
        bt_take_profit = st.number_input("Take Profit (%)", value=0.0, step=2.0, key="bt_tp") / 100
    with col_b2:
        bt_lookback = st.slider("Lookback (days)", 180, 1095, 730, 30, key="bt_look")
        bt_risk = st.slider("Risk/Trade (%)", 0.5, 5.0, 1.0, 0.1, key="bt_risk") / 100
        bt_max_hold = st.slider("Max Hold (days)", 10, 120, 60, 5, key="bt_hold")
        bt_commission = st.number_input("Commission (%)", value=0.0, step=0.05, key="bt_comm") / 100

    if st.button("Run Backtest", key="run_bt"):
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=bt_lookback)).strftime("%Y-%m-%d")
        try:
            df = loader.fetch_ohlcv(bt_ticker, start, end)

            if bt_strategy == "VolumeBreakout":
                strategy = VolumeBreakoutStrategy()
            else:
                strategy = PullbackMAStrategy()

            bt = Backtester(
                initial_capital=bt_capital,
                risk_per_trade=bt_risk,
                hard_stop_pct=hard_stop,
                max_holding_days=bt_max_hold,
                take_profit_pct=bt_take_profit,
                commission_pct=bt_commission,
            )
            result = bt.run_multi(df, strategy, bt_ticker)

            # Summary
            st.code(result.summary(), language=None)

            # Metrics
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total Return", f"{result.total_return_pct:.2%}")
            c2.metric("Win Rate", f"{result.win_rate:.1%}")
            c3.metric("Sharpe Ratio", f"{result.sharpe_ratio:.2f}")
            c4.metric("Max Drawdown", f"{result.max_drawdown_pct:.2%}")

            c5, c6, c7, c8 = st.columns(4)
            c5.metric("Total Trades", result.total_trades)
            c6.metric("Winners", result.winning_trades)
            c7.metric("Losers", result.losing_trades)
            c8.metric("Profit Factor", f"{result.profit_factor:.2f}")

            # Equity curve
            if not result.equity_curve.empty:
                st.subheader("Equity Curve")
                eq_fig = go.Figure()
                eq_fig.add_trace(go.Scatter(
                    y=result.equity_curve.values,
                    mode="lines",
                    name="Equity",
                    line=dict(color="lime", width=2),
                ))
                eq_fig.update_layout(
                    height=350,
                    template="plotly_dark",
                    yaxis_title="Capital (JPY)",
                )
                st.plotly_chart(eq_fig, width='stretch')

            # Trade log
            if result.trades:
                st.subheader("Trade Log")
                trades_df = pd.DataFrame([{
                    "Ticker": t.ticker,
                    "Strategy": t.strategy,
                    "Entry": t.entry_date.strftime("%Y-%m-%d") if t.entry_date else "",
                    "EntryPrice": t.entry_price,
                    "Shares": t.shares,
                    "StopLoss": t.stop_loss,
                    "Exit": t.exit_date.strftime("%Y-%m-%d") if t.exit_date else "",
                    "ExitPrice": t.exit_price if t.exit_price else 0.0,
                    "Pnl": t.pnl,
                    "PnlPct": t.pnl_pct,
                    "Reason": t.exit_reason,
                } for t in result.trades])
                st.dataframe(trades_df, width='stretch')
                st.download_button(
                    "Download trades CSV",
                    data=trades_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"backtest_trades_{bt_ticker}_{bt_strategy}_{datetime.now():%Y%m%d_%H%M}.csv",
                    mime="text/csv",
                    key="dl_bt_trades",
                )

        except Exception as e:
            st.error(f"Backtest failed: {e}")
            logger.exception("Backtest error")


# ============================
# TAB 5: Portfolio
# ============================

with tab_portfolio:
    st.subheader("Portfolio Tracker")

    portfolio = PortfolioTracker(total_capital=capital, max_sector_pct=0.30)

    # Refresh prices
    if st.button("Refresh Prices", key="refresh_prices"):
        with st.spinner("Updating prices..."):
            portfolio.update_prices()
        st.success("Prices updated")

    # Stats
    stats = portfolio.stats()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open Positions", stats["open_positions"])
    c2.metric("Market Value", f"¥{stats['total_market_value']:,.0f}")
    c3.metric("Unrealized P/L", f"¥{stats['total_unrealized_pnl']:,.0f}")
    c4.metric("Win Rate", f"{stats['win_rate']:.1%}" if stats['closed_trades'] > 0 else "N/A")

    # Sector exposure
    exposure = portfolio.sector_exposure()
    if exposure:
        st.subheader("Sector Exposure")
        exp_fig = go.Figure(data=[go.Pie(
            labels=list(exposure.keys()),
            values=list(exposure.values()),
            hole=0.3,
        )])
        exp_fig.update_layout(height=300, template="plotly_dark")
        st.plotly_chart(exp_fig, width='stretch')

        over = portfolio.overexposed_sectors()
        if over:
            for s, f in over.items():
                st.warning(f"Sector '{s}' at {f:.0%} — exceeds 30% limit")

    # Open positions
    pos_df = portfolio.summary_df()
    if not pos_df.empty:
        st.subheader("Open Positions")
        st.dataframe(pos_df.style.format({
            "entry_price": "¥{:.2f}",
            "current_price": "¥{:.2f}",
            "stop_loss": "¥{:.2f}",
            "unrealized_pnl": "¥{:,.0f}",
            "unrealized_pnl_pct": "{:.2%}",
        }), width='stretch')
        st.download_button(
            "Download open positions CSV",
            data=pos_df.to_csv(index=False).encode("utf-8"),
            file_name=f"open_positions_{datetime.now():%Y%m%d_%H%M}.csv",
            mime="text/csv",
            key="dl_open_pos",
        )
    else:
        st.info("No open positions.")


    # Closed trades
    closed_df = portfolio.closed_trades_df()
    if not closed_df.empty:
        st.subheader("Closed Trades")
        st.dataframe(closed_df, width='stretch')
        st.download_button(
            "Download closed trades CSV",
            data=closed_df.to_csv(index=False).encode("utf-8"),
            file_name=f"closed_trades_{datetime.now():%Y%m%d_%H%M}.csv",
            mime="text/csv",
            key="dl_closed",
        )


# ============================
# TAB 6: Earnings
# ============================

with tab_earnings:
    st.subheader("Earnings Calendar Check")

    earn_tickers = st.text_area(
        "Tickers to Check",
        value="7203\n6758\n9984\n8306\n6501",
        height=150,
        key="earn_tickers",
    )
    warn_days = st.slider("Warning Window (days)", 7, 30, 14, key="earn_warn")

    if st.button("Check Earnings", key="check_earn"):
        tickers = [t.strip() for t in earn_tickers.strip().splitlines() if t.strip()]
        cal = EarningsCalendar(loader=loader, warning_days=warn_days)

        with st.spinner("Checking earnings dates..."):
            results = cal.check_batch(tickers)

        for ticker, info in results.items():
            if info.is_upcoming:
                st.warning(
                    f"**{ticker}**: Earnings in {info.days_until_earnings} days "
                    f"({info.next_earnings_date.strftime('%Y-%m-%d') if info.next_earnings_date else '?'}) "
                    f"— HIGH RISK"
                )
            elif info.next_earnings_date:
                st.info(
                    f"**{ticker}**: Next earnings {info.next_earnings_date.strftime('%Y-%m-%d')} "
                    f"({info.days_until_earnings} days)"
                )
            else:
                st.success(f"**{ticker}**: No upcoming earnings data")

        safe, risky = cal.filter_safe(tickers)
        st.markdown("---")
        st.subheader("Result")
        if safe:
            st.success(f"Safe to trade ({len(safe)}): {', '.join(safe)}")
        if risky:
            st.warning(f"Risky — skip ({len(risky)}): {', '.join(risky)}")


# ============================
# TAB 7: Smart Screen
# ============================

with tab_chain:
    st.subheader("Smart Screen — Multi-Pass Screener")

    st.markdown("""
Pipeline: **Fundamental** (ROE, P/E, P/B, EPS) → **Technical** (SMA200 uptrend) → **Weighted Score** → Top N
    """)

    chain_tickers = st.text_area(
        "Ticker Universe",
        value="7203\n6758\n9984\n8306\n6501\n7267\n9434\n6861\n8411\n7751\n6752\n7974\n6178\n8316\n9831",
        height=200,
        key="chain_tickers",
    )

    col_c1, col_c2 = st.columns(2)
    with col_c1:
        chain_top_n = st.slider("Top N Results", 5, 30, 10, key="chain_top")
        chain_roe = st.number_input("Min ROE (%)", value=8.0, step=1.0, key="chain_roe") / 100
    with col_c2:
        chain_pe = st.number_input("Max P/E", value=20.0, step=1.0, key="chain_pe")
        chain_lookback = st.slider("Technical Lookback", 180, 730, 365, 30, key="chain_lb")

    if st.button("Run Smart Screen", key="run_chain"):
        tickers = [t.strip() for t in chain_tickers.strip().splitlines() if t.strip()]
        conditions = [
            Condition("roe", ">", chain_roe),
            Condition("pe", "<", chain_pe),
            Condition("pb", "<", 3.0),
            Condition("eps", ">", 0),
            Condition("dividend_yield", ">", 0.005),
        ]

        chainer = ScreenChainer(loader=loader)
        with st.spinner("Running multi-pass screen..."):
            results = chainer.run(
                tickers,
                fundamental_conditions=conditions,
                top_n=chain_top_n,
                lookback_days=chain_lookback,
            )

        if results:
            st.success(f"{len(results)} stocks ranked")
            rank_df = pd.DataFrame([{
                "Rank": s.rank,
                "Ticker": s.ticker,
                "Score": f"{s.score:.3f}",
                "Fund Score": f"{s.fundamental_score:.3f}",
                "Tech Score": f"{s.technical_score:.3f}",
                "Sector": s.sector,
            } for s in results])
            st.dataframe(rank_df, width='stretch')
        else:
            st.info("No results. Try relaxing filters.")


# ============================
# TAB 8: Multi-Timeframe
# ============================

with tab_mtf:
    st.subheader("Multi-Timeframe Confirmation")
    st.markdown("Daily signals confirmed by weekly trend. Higher confidence = stronger signal.")

    mtf_tickers = st.text_area(
        "Tickers (one per line)",
        value="7203\n6758\n9984\n8306\n6501",
        height=150,
        key="mtf_tickers",
    )
    mtf_min_conf = st.slider("Min confidence", 0.5, 1.0, 0.7, 0.05, key="mtf_conf")

    if st.button("Run MTF Scan", key="run_mtf"):
        tickers = [t.strip() for t in mtf_tickers.strip().splitlines() if t.strip()]
        if not tickers:
            st.warning("No tickers to scan")
            st.stop()
        confirmer = MultiTimeframeConfirmer(min_confidence=mtf_min_conf)
        with st.spinner("Fetching daily + weekly data..."):
            confirmed = confirmer.scan_tickers(tickers, lookback_days=545)
        if not confirmed:
            st.info("No signals passed the confidence threshold.")
        else:
            st.success(f"{len(confirmed)} confirmed signals")
            rows = [{
                "Ticker": c.ticker,
                "Strategy": c.strategy,
                "Entry": f"¥{c.entry_price:,.0f}",
                "SL": f"¥{c.stop_loss:,.0f}" if c.stop_loss else "—",
                "Confidence": f"{c.confidence:.0%}",
                "Weekly": "✓" if c.weekly_signal else "—",
            } for c in confirmed]
            st.dataframe(pd.DataFrame(rows), width='stretch')
            st.download_button(
                "Download CSV",
                data=pd.DataFrame(rows).to_csv(index=False).encode("utf-8"),
                file_name=f"mtf_signals_{datetime.now():%Y%m%d_%H%M}.csv",
                mime="text/csv",
                key="dl_mtf",
            )


# ============================
# TAB 9: Alerts
# ============================

with tab_alerts:
    st.subheader("Signal Alerts")

    col_a1, col_a2 = st.columns([1, 1])

    with col_a1:
        st.markdown("### Channels")
        tg_ok = bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))
        sl_ok = bool(os.environ.get("SLACK_WEBHOOK_URL"))
        st.markdown(f"- Telegram: **{'Configured ✅' if tg_ok else 'Not set ❌'}**")
        st.markdown(f"- Slack: **{'Configured ✅' if sl_ok else 'Not set ❌'}**")

        if st.button("Test Telegram", key="test_tg"):
            sender = TelegramSender()
            ok = sender.send("<b>Test</b> từ TSE Stock Screener ✅")
            st.success("Sent ✅") if ok else st.error("Failed ❌")

        if st.button("Test Slack", key="test_slack"):
            sender = SlackSender()
            ok = sender.send("*Test* từ TSE Stock Screener ✅")
            st.success("Sent ✅") if ok else st.error("Failed ❌")

    with col_a2:
        st.markdown("### Auto-Scan (GitHub Actions)")
        st.markdown("""
For daily automated scan, set up **GitHub Actions**:

1. Add these **GitHub Secrets**:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `SLACK_WEBHOOK_URL`

2. Push this repo — workflow at `.github/workflows/daily-scan.yml`
   runs Mon-Fri 15:30 JST automatically.
        """)

    st.markdown("---")
    st.markdown("### Manual Scan")

    alert_tickers = st.text_area(
        "Tickers (one per line)",
        value="7203\n6758\n9984\n8306\n6501\n7267\n9434\n6861\n8411\n7751",
        height=120,
        key="alert_tickers",
    )
    alert_lookback = st.slider("Lookback (days)", 90, 730, 365, 30, key="alert_lb")

    if st.button("Scan & Send Alerts", key="run_alerts"):
        tickers = [t.strip() for t in alert_tickers.strip().splitlines() if t.strip()]

        scanner = AlertScanner(
            tickers=tickers,
            lookback_days=alert_lookback,
        )
        with st.spinner("Scanning and sending alerts..."):
            results = scanner.scan_and_alert()

        total_signals = sum(len(v) for v in results.values())
        tickers_found = [t for t, v in results.items() if v]

        if total_signals == 0:
            st.info("No signals found.")
        else:
            st.success(f"{total_signals} signals from {len(tickers_found)} tickers")
            for t in tickers_found:
                for s in results[t]:
                    st.code(str(s), language=None)

        st.session_state.last_scan_results = results
        st.toast("Alerts sent!")

    # Show cached last scan if available
    if "last_scan_results" in st.session_state:
        with st.expander("Last Scan Results"):
            for ticker, sigs in st.session_state.last_scan_results.items():
                if sigs:
                    st.write(f"**{ticker}**: {len(sigs)} signals")
                    for s in sigs:
                        st.code(str(s), language=None)


# ============================
# TAB 9: Guide
# ============================

with tab_guide:
    st.subheader("Hướng dẫn Chỉ số Kỹ thuật & Chiến lược Giao dịch")

    # ---- Chỉ báo cơ bản ----
    st.markdown("---")
    st.markdown("## 1. Các Chỉ báo Kỹ thuật (Technical Indicators)")

    with st.expander("SMA — Simple Moving Average (Trung bình Trượt Đơn)", expanded=False):
        st.markdown("""
**Định nghĩa:**
Trung bình cộng của giá đóng cửa trong N phiên gần nhất.

**Công thức:**

```
SMA(N) = (C₁ + C₂ + ... + Cₙ) / N
```

Trong đó `Cᵢ` là giá đóng cửa tại phiên thứ i.

**Cách đọc:**
| Hiện tượng | Ý nghĩa |
|---|---|
| Giá cắt lên trên SMA | Tín hiệu tăng giá (bullish) |
| Giá cắt xuống dưới SMA | Tín hiệu giảm giá (bearish) |
| SMA ngắn cắt lên SMA dài (Golden Cross) | Xu hướng tăng mạnh |
| SMA ngắn cắt xuống SMA dài (Death Cross) | Xu hướng giảm mạnh |

**Ứng dụng trong hệ thống:**
- **SMA 20**: Xu hướng ngắn hạn. Giá trên SMA20 = đang tăng ngắn hạn.
- **SMA 50**: Xu hướng trung hạn. Dùng làm vùng hỗ trợ khi giá pullback.
- **SMA 200**: Xu hướng dài hạn. Giá > SMA200 = thị trường bullish dài hạn.

**Nhược điểm:** Lagging indicator (độ trễ) — phản ứng chậm so với giá thực tế.
        """)

    with st.expander("RSI — Relative Strength Index (Chỉ số Sức mạnh Tương đối)", expanded=False):
        st.markdown("""
**Định nghĩa:**
Chỉ báo động lượng đo tốc độ và biên độ thay đổi giá. Dao động từ 0 đến 100.

**Công thức:**

```
RS = Avg Gain(N) / Avg Loss(N)
RSI = 100 - (100 / (1 + RS))
```

N通常 = 14 phiên.

**Cách đọc:**
| Giá trị | Ý nghĩa |
|---|---|
| RSI > 70 | Quá mua (overbought) — có thể đảo chiều giảm |
| RSI < 30 | Quá bán (oversold) — có thể đảo chiều tăng |
| RSI 40-60 | Vùng trung tính |
| RSI > 80 | Quá mua cực mạnh — cẩn thận khi mua vào |

**Ứng dụng trong hệ thống:**
- Strategy PullbackMA dùng `RSI < 60` làm bộ lọc — tránh mua khi giá đã quá nóng.
- Kết hợp RSI với SMA: RSI thấp + giá chạm SMA = cơ hội mua tốt.

**Lưu ý:** RSI có thể ở vùng quá mua/quá bán lâu trong trend mạnh. Không nên dùng RSI đơn lẻ.
        """)

    with st.expander("ATR — Average True Range (Biên độ Dao động Trung bình)", expanded=False):
        st.markdown("""
**Định nghĩa:**
Đo biên độ biến động giá trung bình trong N phiên. Giá trị tuyệt đối (đơn vị: JPY).

**Công thức:**

```
True Range = max(
    High - Low,
    |High - Previous Close|,
    |Low - Previous Close|
)
ATR = SMA(True Range, N)
```

N通常 = 14 phiên.

**Cách đọc:**
| Giá trị ATR | Ý nghĩa |
|---|---|
| ATR cao | Biến động mạnh — cần khoảng cách stop-loss rộng hơn |
| ATR thấp | Biến động thấp — thị trường sideway hoặc tích lũy |
| ATR tăng dần | Xu hướng đang mạnh lên |
| ATR giảm dần | Xu hướng đang yếu đi |

**Ứng dụng trong hệ thống:**
- **Stop-loss ATR-based**: `SL = Entry - 2 × ATR` — cắt lỗ dựa trên biến động thực tế.
- **Position sizing**: ATR越大 → risk per share越大 → số lượng cổ phiếu giảm → bảo vệ vốn.

**Lưu ý:** ATR không cho biết hướng đi của giá, chỉ đo biên độ.
        """)

    with st.expander("Bollinger Bands (Dải Bollinger)", expanded=False):
        st.markdown("""
**Định nghĩa:**
Dải giá bao quanh SMA với độ rộng dựa trên độ lệch chuẩn (standard deviation).

**Công thức:**

```
Middle Band = SMA(Close, 20)
Upper Band  = Middle + 2 × StdDev(Close, 20)
Lower Band  = Middle - 2 × StdDev(Close, 20)
```

**Cách đọc:**
| Hiện tượng | Ý nghĩa |
|---|---|
| Giá chạm Upper Band | Giá ở mức cao so với trung bình — có thể hồi về |
| Giá chạm Lower Band | Giá ở mức thấp — có thể hồi lên |
| Dải co lại (squeeze) | Biến động thấp — sắp có breakout mạnh |
| Dải mở rộng | Biến động tăng — trend đang mạnh |

**Ứng dụng:**
- Phát hiện giai đoạn tích lũy (squeeze) trước khi breakout.
- Giá xuyên qua Upper Band trong trend mạnh = tiếp tục tăng.
        """)

    with st.expander("Volume & Volume SMA", expanded=False):
        st.markdown("""
**Định nghĩa:**
- **Volume**: Số lượng cổ phiếu giao dịch trong phiên.
- **Volume SMA(20)**: Trung bình volume trong 20 phiên.

**Công thức:**

```
Volume Ratio = Volume Hiện tại / Volume SMA(20)
```

**Cách đọc:**
| Volume Ratio | Ý nghĩa |
|---|---|
| > 1.5 | Khối lượng đột biến 1.5x — có thể có breakout/breakdown |
| > 2.0 | Đột biến mạnh — xác nhận breakout |
| < 0.5 | Thanh khoản thấp — thiếu quan tâm từ thị trường |

**Ứng dụng trong hệ thống:**
- Strategy **VolumeBreakout** yêu cầu `Volume > 1.5 × Volume SMA(20)` — xác nhận breakout có khối lượng hỗ trợ.
- Breakout không có volume = false breakout (breakout giả).

**Lưu ý:** Volume là leading indicator — volume thường tăng trước giá.
        """)

    # ---- Chiến lược ----
    st.markdown("---")
    st.markdown("## 2. Các Chiến lược trong Hệ thống")

    with st.expander("Volume Breakout Strategy (Phá vỡ Khối lượng)", expanded=False):
        st.markdown("""
**Mục tiêu:** Phát hiện điểm giá vượt khỏi vùng tích lũy kèm khối lượng giao dịch đột biến.

**Điều kiện kích hoạt (cả 3 phải thỏa):**
1. **Giá phá đỉnh**: Close > highest High của N phiên trước (mặc định 20).
2. **Khối lượng đột biến**: Volume > 1.5 × Volume SMA(20).
3. **Xu hướng tăng**: Close > SMA(20).

**Stop-loss:**
- Mặc định: `Entry - 2 × ATR(14)`
- Hard stop: Tối đa 7% từ giá vào lệnh (cái nào chặt hơn lấy cái đó).

**Ví dụ thực tế:**
```
Ticker: 7203 (Toyota)
Giá đóng cửa: ¥2,800
Highest High 20 phiên: ¥2,750
Volume: 50,000 shares
Volume SMA(20): 25,000 shares → Ratio = 2.0 ✅
SMA(20): ¥2,720 → Close > SMA ✅
ATR(14): ¥45 → SL = 2,800 - 2×45 = ¥2,710
```

**Ưu điểm:**
- Breakout có volume xác nhận có tỷ lệ thành công cao.
- ATR-based stop-loss linh hoạt theo biến động.

**Nhược điểm:**
- Có thể bắt false breakout nếu thị trường sideway.
- Cần datos lịch sử đủ dài (≥ 20 phiên) để tính chính xác.
        """)

    with st.expander("Pullback MA Strategy (Mua khi Hồi về Đường trung bình)", expanded=False):
        st.markdown("""
**Mục tiêu:** Mua khi giá hồi về SMA20 hoặc SMA50 trong xu hướng tăng dài hạn.

**Điều kiện kích hoạt (cả 4 phải thỏa):**
1. **Xu hướng dài tăng**: Close > SMA(200).
2. **Hồi về MA**: Phiên trước Close < SMA(20/50), phiên này Close ≥ SMA(20/50) (cắt lên).
3. **Chưa quá nóng**: RSI(14) < 60.
4. **Có pullback rõ ràng**: Giá vừa xuống dưới MA rồi cắt lên lại.

**Stop-loss:**
- Mặc định: `Entry - 2 × ATR(14)`
- Hard stop: Tối đa 7%.

**Ví dụ thực tế:**
```
Ticker: 6758 (Sony Group)
Giá đóng cửa: ¥12,500
SMA(200): ¥11,800 → Close > SMA200 ✅ (uptrend dài hạn)
SMA(50): ¥12,480 → Phiên trước: ¥12,400 < ¥12,480 ✅
                    Phiên này:  ¥12,500 ≥ ¥12,480 ✅ (cutoff lên)
RSI(14): 55 → < 60 ✅
ATR(14): ¥180 → SL = 12,500 - 2×180 = ¥12,140
```

**Ưu điểm:**
- Mua tại vùng hỗ trợ (SMA) trong trend tăng → tỷ lệ thắng cao.
- Rủi ro thấp hơn mua ở đỉnh.
- Phù hợp với nhà đầu tư trung hạn (giữ 2-8 tuần).

**Nhược điểm:**
- Nếu trend đảo chiều (giá xuyên xuống SMA200), tín hiệu sẽ sai.
- Cần theo dõi sát diễn biến giá tại vùng MA.
        """)

    # ---- Risk Management ----
    st.markdown("---")
    st.markdown("## 3. Quản trị Rủi ro (Risk Management)")

    with st.expander("Position Sizing — Quy tắc 1% Tổng Vốn", expanded=False):
        st.markdown("""
**Nguyên tắc:**
Mỗi giao dịch chỉ được phép mất tối đa **1% tổng vốn**.

**Công thức:**

```
Risk Amount = Tổng Vốn × 1%
Risk per Share = Entry Price - Stop Loss
Số cổ phiếu = Risk Amount / Risk per Share
Position Value = Số cổ phiếu × Entry Price
```

**Ví dụ:**
```
Tổng vốn: ¥10,000,000
Risk per trade: 1% = ¥100,000
Entry: ¥2,800
Stop-loss: ¥2,710
Risk per share: ¥90

Số cổ phiếu = 100,000 / 90 = 1,111 shares (làm tròn xuống)
Position value = 1,111 × 2,800 = ¥3,110,800 (31% vốn)
```

**Tại sao quan trọng:**
- Dù thua 10 lệnh liên tiếp, bạn chỉ mất 10% vốn.
- Bảo vệ tài khoản khỏi "death spiral" — thua lỗ nghiêm trọng.
- Cho phép giao dịch tự tin hơn vì rủi ro đã được kiểm soát.
        """)

    with st.expander("Stop-loss 7% — Cắt lỗ Cứng", expanded=False):
        st.markdown("""
**Quy tắc:**
Mọi vị thế bắt buộc phải có stop-loss. Nếu giá giảm > 7% từ giá vào → bán ngay.

**Công thức:**

```
Hard Stop = Entry Price × (1 - 7%)
Final Stop = max(ATR-based SL, Hard Stop)
```

**Tại sao là 7%?**
- Nghiên cứu lịch sử TSE: cổ phiếu giảm > 7% từ đỉnh thường tiếp tục giảm.
- 7% là mức chấp nhận được cho swing trading trên TSE.
- Kết hợp với 1% risk rule: max loss per trade = 1% portfolio.

**Lỗi thường gặp:**
| Lỗi | Hậu quả |
|---|---|
| Bỏ stop-loss | Thua lỗ nghiêm trọng, có thể mất 30-50% |
| Dời SL xuống dưới | "Hy vọng" giá phục hồi → thua nặng hơn |
| Bán ngay khi chạm SL | Đúng — discipline là chìa khóa |
        """)

    # ---- Chỉ số Phân tích Cơ bản ----
    st.markdown("---")
    st.markdown("## 4. Chỉ số Phân tích Cơ bản (Fundamental)")

    with st.expander("P/E — Price to Earnings Ratio (Tỷ suất Giá/Lợi nhuận)", expanded=False):
        st.markdown("""
**Công thức:**

```
P/E = Giá cổ phiếu / EPS (Lợi nhuận trên mỗi cổ phiếu)
```

**Cách đọc:**
| P/E | Ý nghĩa |
|---|---|
| < 10 | Rẻ — có thể undervalued hoặc doanh nghiệp gặp vấn đề |
| 10-15 | Hợp lý — giá phản ánh đúng giá trị |
| 15-25 | Đắt — thị trường kỳ vọng tăng trưởng |
| > 30 | Rất đắt — cần tăng trưởng mạnh để biện minh |

**Ứng dụng:** Lọc cổ phiếu có giá "hợp lý" so với lợi nhuận. P/E < 20 là điều kiện phổ biến.
        """)

    with st.expander("P/B — Price to Book Ratio (Tỷ suất Giá/Giá trị Sách)", expanded=False):
        st.markdown("""
**Công thức:**

```
P/B = Giá cổ phiếu / Book Value per Share
     = Market Cap / Tổng Tài sản ròng
```

**Cách đọc:**
| P/B | Ý nghĩa |
|---|---|
| < 1 | Giá thấp hơn giá trị sổ sách — có thể undervalued |
| 1-2 | Hợp lý cho ngành sản xuất / ngân hàng |
| > 3 | Đắt — thường gặp ở ngành công nghệ / dịch vụ |

**Ứng dụng:** P/B < 3 là bộ lọc phổ biến cho giá trị investing.
        """)

    with st.expander("ROE — Return on Equity (Tỷ suất Lợi nhuận Vốn chủ sở hữu)", expanded=False):
        st.markdown("""
**Công thức:**

```
ROE = Lợi nhuận ròng / Vốn chủ sở hữu
```

**Cách đọc:**
| ROE | Ý nghĩa |
|---|---|
| < 5% | Kém hiệu quả |
| 5-10% | Trung bình |
| 10-20% | Tốt — quản trị hiệu quả |
| > 20% | Xuất sắc — thường gặp ở doanh nghiệp có lợi thế cạnh tranh |

**Ứng dụng:** ROE > 10% là điều kiện cơ bản để chọn cổ phiếu tăng trưởng.
        """)

    with st.expander("EPS — Earnings per Share (Lợi nhuận trên Cổ phiếu)", expanded=False):
        st.markdown("""
**Công thức:**

```
EPS = Lợi nhuận ròng / Số cổ phiếu đang lưu hành
```

**Ý nghĩa:**
- EPS > 0: Doanh nghiệp có lãi.
- EPS tăng dần qua các năm: Doanh nghiệp tăng trưởng.
- EPS âm: Doanh nghiệp thua lỗ.

**Ứng dụng:** Lọc bỏ cổ phiếu có EPS âm (doanh nghiệp thua lỗ).
        """)

    with st.expander("Dividend Yield (Tỷ suất Cổ tức)", expanded=False):
        st.markdown("""
**Công thức:**

```
Dividend Yield = Cổ tức mỗi cổ phiếu / Giá cổ phiếu
```

**Cách đọc:**
| Yield | Ý nghĩa |
|---|---|
| < 1% | Thấp — tập trung tái đầu tư |
| 1-3% | Trung bình — cân bằng giữa tăng trưởng và cổ tức |
| 3-5% | Cao — thu nhập thụ động tốt |
| > 5% | Rất cao — cần kiểm tra tính bền vững |

**Ứng dụng:** Dividend Yield > 1% lọc cổ phiếu trả cổ tức — phù hợp chiến lược thu nhập.
        """)

    with st.expander("Market Capitalization (Vốn hóa Thị trường)", expanded=False):
        st.markdown("""
**Công thức:**

```
Market Cap = Giá cổ phiếu × Số cổ phiếu đang lưu hành
```

**Phân loại trên TSE:**
| Vốn hóa | Nhóm | Đặc điểm |
|---|---|---|
| > ¥1,000B | Large Cap | Ổn định, thanh khoản cao, tăng trưởng chậm |
| ¥100B-1,000B | Mid Cap | Cân bằng giữa tăng trưởng và ổn định |
| < ¥100B | Small Cap | Tăng trưởng cao, rủi ro cao, thanh khoản thấp |

**Ứng dụng:** Lọc theo vốn hóa để chọn nhóm cổ phiếu phù hợp với khẩu vị rủi ro.
        """)
