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

import json
import logging
import os
import sys
import threading
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_js_eval import streamlit_js_eval

from src.stock_screener import auth, db, jwt_auth, user_store
from src.stock_screener.alert import AlertScanner, SlackSender, TelegramSender
from src.stock_screener.backtest import Backtester

# Local modules
from src.stock_screener.data_loader import YFinanceDataLoader
from src.stock_screener.earnings_calendar import EarningsCalendar
from src.stock_screener.fundamental_screener import Condition, FundamentalScreener
from src.stock_screener.multi_timeframe import MultiTimeframeConfirmer
from src.stock_screener.portfolio import PortfolioTracker
from src.stock_screener.profit_target import TargetRow, calculate_exit_price, summarize
from src.stock_screener.risk_management import RiskManager
from src.stock_screener.screen_chain import ScreenChainer
from src.stock_screener.technical_engine import (
    OverboughtReversalSellStrategy,
    PullbackMAStrategy,
    TechnicalEngine,
    TrendBreakdownSellStrategy,
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

def load_global_css() -> None:
    """Load and inject centralized CSS from style.css."""
    css_path = os.path.join(
        os.path.dirname(__file__),
        "src",
        "stock_screener",
        "assets",
        "style.css",
    )
    if os.path.exists(css_path):
        with open(css_path, encoding="utf-8") as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
    else:
        fallback_path = "src/stock_screener/assets/style.css"
        if os.path.exists(fallback_path):
            with open(fallback_path, encoding="utf-8") as f:
                st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)

load_global_css()

# ---------------------------------------------------------------------------
# Auth gate — must run before any other UI
# ---------------------------------------------------------------------------

# Auto-create DB + bootstrap admin from ENV (idempotent)
auth.bootstrap_admin_from_env()

if "auth_user" not in st.session_state:
    st.session_state.auth_user = None

if "sb_lang" not in st.session_state:
    st.session_state.sb_lang = "EN"

# Try silent auto-login from a previously-issued JWT in localStorage.
# On first render this returns None; the second pass picks it up.
if st.session_state.auth_user is None:
    _token = streamlit_js_eval(
        js_expressions="localStorage.getItem('tse_jwt')",
        key="_jwt_load",
    )
    if _token and _token not in (None, "", "null"):
        payload = jwt_auth.verify_token(_token)
        if payload is not None:
            # Cross-check user still exists in DB
            rec = auth.get_by_username(payload.get("username", ""))
            if rec is not None and str(rec.id) == str(payload.get("sub")):
                st.session_state.auth_user = rec
            else:
                # User deleted or username changed — purge stale token
                streamlit_js_eval(
                    js_expressions="localStorage.removeItem('tse_jwt')",
                    key="_jwt_clear_stale",
                )
        else:
            # Expired or invalid — wipe it so the next load is clean
            streamlit_js_eval(
                js_expressions="localStorage.removeItem('tse_jwt')",
                key="_jwt_clear_invalid",
            )


def _issue_token_and_login(rec: auth.UserRecord) -> None:
    """Store JWT in localStorage and mark session as authenticated."""
    token = jwt_auth.create_token(rec.id, rec.username)
    safe_token = json.dumps(token)
    streamlit_js_eval(
        js_expressions=f"localStorage.setItem('tse_jwt', {safe_token})",
        key="_jwt_issue",
    )
    st.session_state.auth_user = rec


def _render_auth_page() -> None:
    """Login screen — the only thing the user sees until authenticated."""
    # Inject login-specific overrides (block-container padding, hide sidebar)
    st.markdown(
        """
        <style>
        /* Hide sidebar on login */
        section[data-testid="stSidebar"] { display: none !important; }
        .main > .block-container { padding-top: 0 !important; max-width: 480px !important; margin: 0 auto; }
        /* Background orbs */
        .login-orb {
            position: fixed; border-radius: 50%; filter: blur(100px);
            opacity: 0.07; pointer-events: none; z-index: 0;
        }
        .login-orb.o1 { width: 600px; height: 600px; background: #22C55E; top: -10%; left: -5%; }
        .login-orb.o2 { width: 500px; height: 500px; background: #0ea5e9; bottom: -5%; right: -5%; }
        </style>
        <div class="login-orb o1"></div>
        <div class="login-orb o2"></div>
        """,
        unsafe_allow_html=True,
    )

    # Vertical spacer to center card visually
    st.markdown("<div style='height: 12vh'></div>", unsafe_allow_html=True)

    # Language toggle
    _, col_lang = st.columns([4, 1])
    with col_lang:
        lang_choice = st.selectbox(
            "Lang",
            options=["EN", "VN"],
            index=0 if st.session_state.get("sb_lang", "EN") == "EN" else 1,
            key="login_lang_selector",
            label_visibility="collapsed",
        )
        if lang_choice != st.session_state.get("sb_lang", "EN"):
            st.session_state.sb_lang = lang_choice
            st.rerun()

    is_vn = st.session_state.get("sb_lang", "EN") == "VN"

    # Brand header
    tag_text = (
        "Sàn Giao dịch Chứng khoán Tokyo — Bảng Điều khiển Tín hiệu"
        if is_vn else "Tokyo Stock Exchange — Signal Dashboard"
    )
    st.markdown(
        f"""
        <div style="text-align:center; margin-bottom:1.5rem;">
            <div style="
                width:56px; height:56px; margin:0 auto 14px;
                display:flex; align-items:center; justify-content:center;
                font-size:1.75rem; background:#22C55E; border-radius:12px;
                box-shadow: 0 4px 15px rgba(34,197,94,0.3);
            ">📈</div>
            <h1 style="font-size:1.5rem; font-weight:700; color:#f8fafc; margin:0 0 6px;">TSE Screener</h1>
            <div style="color:#94a3b8; font-size:0.85rem;">
                {tag_text}
                <span style="
                    display:inline-block; margin-left:4px; padding:1px 6px;
                    background:rgba(34,197,94,0.15); color:#22C55E;
                    font-size:0.65rem; font-weight:600; border-radius:4px;
                    vertical-align:middle;
                ">JP</span>
            </div>
        </div>
        <div style="
            height:1px; margin:0 0 1.5rem;
            background:linear-gradient(90deg, transparent 0%, rgba(34,197,94,0.2) 20%, rgba(34,197,94,0.2) 80%, transparent 100%);
        "></div>
        """,
        unsafe_allow_html=True,
    )

    if db.user_count() == 0:
        st.info(
            "Chưa có tài khoản nào. Thiết lập `TSE_ADMIN_USER` / `TSE_ADMIN_PASSWORD` "
            "trong `.env` và khởi động lại."
            if is_vn else
            "No accounts yet. Set `TSE_ADMIN_USER` / `TSE_ADMIN_PASSWORD` "
            "in `.env` and restart.",
            icon="ℹ️",
        )

    with st.form("login_form", clear_on_submit=False):
        u = st.text_input(
            "Tên đăng nhập" if is_vn else "Username",
            key="login_u",
            autocomplete="username",
            placeholder="admin",
        )
        p = st.text_input(
            "Mật khẩu" if is_vn else "Password",
            type="password",
            key="login_p",
            autocomplete="current-password",
            placeholder="••••••••",
        )
        ok = st.form_submit_button(
            "Đăng nhập →" if is_vn else "Sign in →",
            type="primary",
        )
        if ok:
            if not u or not p:
                st.warning(
                    "Vui lòng nhập cả tên đăng nhập và mật khẩu."
                    if is_vn else "Enter both username and password."
                )
            else:
                rec = auth.verify_user(u, p)
                if rec is None:
                    st.error(
                        "Tên đăng nhập hoặc mật khẩu không chính xác."
                        if is_vn else "Invalid username or password."
                    )
                else:
                    _issue_token_and_login(rec)
                    st.rerun()

    st.markdown(
        f'<div style="text-align:center; color:#475569; font-size:0.72rem; margin-top:1rem;">'
        f'{"v1.0.0 · bảo mật bằng JWT (mã xác thực 30 ngày)" if is_vn else "v1.0.0 · secured by JWT (30-day token)"}'
        f'</div>',
        unsafe_allow_html=True,
    )


if st.session_state.auth_user is None:
    _render_auth_page()
    st.stop()

USER = st.session_state.auth_user
USER_ID = USER.id

# Logged in — show the rest of the app
with st.sidebar:
    st.caption(f"👤 **{USER.username}**")
    if st.button("Sign out", key="signout", width='stretch'):
        # Clear token from localStorage + session
        streamlit_js_eval(
            js_expressions="localStorage.removeItem('tse_jwt')",
            key="_jwt_clear_signout",
        )
        st.session_state.auth_user = None
        st.rerun()

st.title("Tokyo Stock Exchange — Screener & Signal Dashboard")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

loader = YFinanceDataLoader()
engine = TechnicalEngine()


def safe_fetch_ohlcv(
    ticker: str,
    start: str,
    end: str,
    interval: str = "1d",
) -> pd.DataFrame | None:
    """Fetch OHLCV with user-friendly error handling.

    Returns DataFrame on success, None on failure (with st.error message).
    """
    try:
        return loader.fetch_ohlcv(ticker, start, end, interval)
    except ValueError as e:
        st.error(f"No data for {ticker}: {e}")
        return None
    except Exception as e:
        st.error(f"Failed to fetch {ticker}: {type(e).__name__}: {e}")
        return None

# ---------------------------------------------------------------------------
# Background auto-scan thread
# ---------------------------------------------------------------------------

_auto_scan_thread: threading.Thread | None = None
_auto_scan_stop = threading.Event()
_auto_scan_last: str = ""
_auto_scan_lock = threading.Lock()

DEFAULT_TICKERS_5 = ["7203", "6758", "9984", "8306", "6501"]
DEFAULT_TICKERS_10 = DEFAULT_TICKERS_5 + ["7267", "9434", "6861", "8411", "7751"]
DEFAULT_TICKERS_15 = DEFAULT_TICKERS_10 + ["6752", "7974", "6178", "8316", "9831"]
DEFAULT_ALERT_TICKERS = DEFAULT_TICKERS_10


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

# Default sidebar settings (overridden by SQLite on first load)
SIDEBAR_DEFAULTS = {
    "sb_capital": 10_000_000,
    "sb_risk_pct": 1.0,
    "sb_hard_stop": 7.0,
    "sb_lookback": 365,
    "sb_lang": "EN",
}
# One-time load from server on first render of the session
if "sb_loaded" not in st.session_state:
    saved = user_store.get_setting(USER_ID, "sidebar", default={})
    for k, default in SIDEBAR_DEFAULTS.items():
        st.session_state[k] = saved.get(k, default)
    st.session_state.sb_loaded = True

with st.sidebar:
    st.header("Settings")
    capital = st.number_input(
        "Total Capital (JPY)",
        min_value=100_000,
        step=1_000_000,
        key="sb_capital",
    )
    risk_pct = st.slider(
        "Risk per Trade (%)", 0.5, 5.0, key="sb_risk_pct", step=0.1
    ) / 100
    hard_stop = st.slider(
        "Hard Stop-Loss (%)", 3.0, 10.0, key="sb_hard_stop", step=0.5
    ) / 100
    lookback_days = st.slider(
        "Data Lookback (days)", 90, 730, key="sb_lookback", step=30
    )
    lang = st.radio(
        "Language / Ngôn ngữ", ["EN", "VN"], key="sb_lang", horizontal=True
    )

    # Persist to server on every change
    user_store.set_setting(
        USER_ID,
        "sidebar",
        {
            "sb_capital": st.session_state.sb_capital,
            "sb_risk_pct": st.session_state.sb_risk_pct,
            "sb_hard_stop": st.session_state.sb_hard_stop,
            "sb_lookback": st.session_state.sb_lookback,
            "sb_lang": st.session_state.sb_lang,
        },
    )

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

    st.markdown("---")
    if st.button("🗑 Reset to defaults", key="sb_reset"):
        for k, v in SIDEBAR_DEFAULTS.items():
            st.session_state[k] = v
        user_store.set_setting(USER_ID, "sidebar", dict(SIDEBAR_DEFAULTS))
        user_store.save_target_rows(USER_ID, [])
        st.success("Reset sidebar + target rows to defaults.")
        st.rerun()


# ---------------------------------------------------------------------------
# Tab layout
# ---------------------------------------------------------------------------

tab_chart, tab_screen, tab_signals, tab_backtest, tab_portfolio, tab_earnings, tab_chain, tab_mtf, tab_target, tab_alerts, tab_guide = st.tabs(
    ["Chart", "Screener", "Signals", "Backtest", "Portfolio", "Earnings", "Smart Screen", "MTF", "Target", "Alerts", "Guide"]
)


# ============================
# TAB 1: Chart
# ============================

with tab_chart:
    st.subheader("Candlestick Chart")

    col1, col2 = st.columns([3, 1])
    with col2:
        if "chart_ticker" not in st.session_state:
            saved_t = user_store.get_setting(USER_ID, "chart_ticker", default="7203")
            st.session_state.chart_ticker = saved_t
        chart_ticker = st.text_input("Ticker", key="chart_ticker")
        chart_interval = st.selectbox("Interval", ["1d", "1h", "1wk"], index=0)
        show_sma = st.checkbox("Show SMA", value=True)
        sma_period = st.selectbox("SMA Period", [20, 50, 200], index=0)
        show_vol = st.checkbox("Show Volume", value=True)
        user_store.set_setting(USER_ID, "chart_ticker", st.session_state.chart_ticker)

    with col1:
        # Determine if we should load the chart
        should_load = False
        if "chart_df" not in st.session_state or st.session_state.get("loaded_ticker") != chart_ticker or st.session_state.get("loaded_interval") != chart_interval or st.session_state.get("loaded_lookback") != lookback_days:
            should_load = True

        if st.button("Load Chart", key="load_chart"):
            should_load = True

        if should_load:
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
            df = safe_fetch_ohlcv(chart_ticker, start, end, chart_interval)
            if df is not None:
                df = engine.enrich(df)
                st.session_state.chart_df = df
                st.session_state.loaded_ticker = chart_ticker
                st.session_state.loaded_interval = chart_interval
                st.session_state.loaded_lookback = lookback_days
            else:
                st.session_state.chart_df = None

        df = st.session_state.get("chart_df")
        if df is not None and not df.empty:
            try:
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
                        increasing_line_color="#22C55E",
                        decreasing_line_color="#EF5350",
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
                                line=dict(width=1, dash="dot", color="#F97316"),
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
                            marker_color="rgba(34, 197, 94, 0.35)",
                        )
                    )
                    vol_fig.update_layout(
                        title="Volume",
                        height=200,
                        template="plotly_dark",
                        margin=dict(t=30, b=10),
                    )
                    st.plotly_chart(vol_fig, width='stretch')

                # Raw Data expander
                with st.expander("Raw Data"):
                    st.dataframe(df.tail(50).round(2))

            except Exception as e:
                st.error(f"Error rendering chart: {e}")
                logger.exception("Chart render failed")


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
        value="\n".join(DEFAULT_TICKERS_10),
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
        value="\n".join(DEFAULT_TICKERS_5),
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
        td_strategy = TrendBreakdownSellStrategy()
        ob_strategy = OverboughtReversalSellStrategy()

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
                sigs.extend(td_strategy.generate_signals(df, ticker))
                sigs.extend(ob_strategy.generate_signals(df, ticker))
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
            sig_df = pd.DataFrame([{
                "Ticker": s.ticker,
                "Strategy": s.strategy,
                "Type": s.signal_type.value,
                "Price": f"¥{s.price:,.0f}",
                "Stop Loss": f"¥{s.stop_loss:,.0f}" if s.stop_loss else "—",
                "Date": s.date.strftime("%Y-%m-%d") if hasattr(s.date, "strftime") else str(s.date),
                "Vol Ratio": s.metadata.get("volume_ratio", "—") if hasattr(s, "metadata") and s.metadata else "—",
            } for s in all_signals])
            st.dataframe(sig_df, width='stretch', hide_index=True)

            # Position sizing
            st.subheader("Position Sizing")
            rm = RiskManager(capital, risk_pct, hard_stop)
            buy_signals = [s for s in all_signals if s.signal_type.value == "BUY"]
            plans = rm.batch_positions(buy_signals)

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
            strat_cols = st.columns(4)
            for i, strat_name in enumerate(sorted({s.strategy for s in all_signals})):
                count = sum(1 for s in all_signals if s.strategy == strat_name)
                strat_cols[i].metric(strat_name, str(count))


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
            c1.metric("Total Return", f"{result.total_return_pct:.2%}",
                      delta=f"{result.total_return_pct:.2%}", delta_color="normal")
            c2.metric("Win Rate", f"{result.win_rate:.1%}",
                      delta=f"{result.win_rate:.1%}pp", delta_color="normal")
            c3.metric("Sharpe Ratio", f"{result.sharpe_ratio:.2f}")
            c4.metric("Max Drawdown", f"{result.max_drawdown_pct:.2%}",
                      delta=f"{result.max_drawdown_pct:.2%}", delta_color="inverse")

            c5, c6, c7, c8 = st.columns(4)
            c5.metric("Total Trades", str(result.total_trades))
            c6.metric("Winners", str(result.winning_trades),
                      delta=f"{result.win_rate:.0f}%", delta_color="normal")
            c7.metric("Losers", str(result.losing_trades),
                      delta=f"{result.losing_rate:.0f}%" if hasattr(result, 'losing_rate') else "",
                      delta_color="inverse")
            c8.metric("Profit Factor", f"{result.profit_factor:.2f}",
                      delta="profitable" if result.profit_factor >= 1.5 else "marginal",
                      delta_color="normal" if result.profit_factor >= 1.5 else "inverse")

            # Equity curve
            if not result.equity_curve.empty:
                st.subheader("Equity Curve")
                eq_fig = go.Figure()
                eq_fig.add_trace(go.Scatter(
                    y=result.equity_curve.values,
                    mode="lines",
                    name="Equity",
                    line=dict(color="#22C55E", width=2),
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
    c1.metric("Open Positions", str(stats["open_positions"]))
    c2.metric("Market Value", f"¥{stats['total_market_value']:,.0f}")
    pnl = stats['total_unrealized_pnl']
    c3.metric("Unrealized P/L", f"¥{pnl:+,.0f}",
              delta=f"¥{pnl:+,.0f}", delta_color="normal")
    wr = stats['win_rate']
    c4.metric("Win Rate", f"{wr:.1%}" if stats['closed_trades'] > 0 else "N/A",
              delta=f"{wr:.0f}%" if stats['closed_trades'] > 0 else "",
              delta_color="normal" if wr >= 50 else "inverse")

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
        value="\n".join(DEFAULT_TICKERS_5),
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
        value="\n".join(DEFAULT_TICKERS_15),
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
        value="\n".join(DEFAULT_TICKERS_5),
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
# TAB 9: Profit Target
# ============================

with tab_target:
    is_vn = st.session_state.get("sb_lang", "EN") == "VN"

    if is_vn:
        st.subheader("Target lợi nhuận hôm nay")
        st.caption("Nhập mã, giá mua, số lượng. Kéo slider mỗi mã để xem giá bán + lợi nhuận kỳ vọng.")
    else:
        st.subheader("Profit Target Calculator")
        st.caption("Enter ticker, entry price, and share size. Drag target slider to view exit price & expected profit.")

    if "target_rows" not in st.session_state:
        saved_rows = user_store.get_target_rows(USER_ID)
        if saved_rows:
            st.session_state.target_rows = saved_rows
        else:
            st.session_state.target_rows = [
                {"ticker": "7203", "entry_price": 2000.0, "target_pct": 5.0, "shares": 100},
            ]



    hdr = st.columns([1.7, 1.2, 1.0, 3.0, 1.3, 0.5])
    hdr[0].markdown(f"**{'Mã CK' if is_vn else 'Ticker'}**")
    hdr[1].markdown(f"**{'Giá mua (¥)' if is_vn else 'Entry Price (¥)'}**")
    hdr[2].markdown(f"**{'SL' if is_vn else 'Shares'}**")
    hdr[3].markdown("**Target %**")
    hdr[4].markdown(f"**{'Giá bán (¥)' if is_vn else 'Exit Price (¥)'}**")
    hdr[5].markdown("")

    remove_idx: int | None = None
    rows_after: list[dict] = []

    for i, row in enumerate(st.session_state.target_rows):
        c1, c2, c3, c4, c5, c6 = st.columns([1.7, 1.2, 1.0, 3.0, 1.3, 0.5])
        ticker = c1.text_input(
            "Ticker",
            value=row.get("ticker", ""),
            key=f"tt_t_{i}",
            label_visibility="collapsed",
        ).strip().upper()
        entry = c2.number_input(
            "Entry Price",
            value=float(row.get("entry_price", 0) or 0),
            min_value=0.0,
            step=10.0,
            key=f"tt_e_{i}",
            label_visibility="collapsed",
            format="%.0f",
        )
        shares = c3.number_input(
            "Shares",
            value=int(row.get("shares", 0) or 0),
            min_value=0,
            step=100,
            key=f"tt_s_{i}",
            label_visibility="collapsed",
            format="%d",
            help="Số cổ phiếu (để 0 nếu chỉ muốn tính giá bán)" if is_vn else "Number of shares (set to 0 to only calculate exit price)",
        )
        target_pct = c4.slider(
            "Target %",
            min_value=0.0,
            max_value=100.0,
            value=float(row.get("target_pct", 5.0) or 5.0),
            step=0.5,
            key=f"tt_p_{i}",
            label_visibility="collapsed",
            help="Kéo để chỉnh % lợi nhuận mong muốn (0% → 100%)" if is_vn else "Drag to adjust desired profit percentage (0% → 100%)",
        )

        if entry > 0:
            exit_price = calculate_exit_price(entry, target_pct)
            color = "#22c55e" if exit_price >= entry else "#ef4444"
            sub_html = ""
            if shares > 0:
                cost = entry * shares
                tgt = exit_price * shares
                pnl = tgt - cost
                pnl_class = "pos" if pnl >= 0 else "neg"
                cost_label = "Vốn" if is_vn else "Cost"
                pnl_label = "Lãi" if is_vn else "Profit"
                sub_html = (
                    f"<div class='tt-sub {pnl_class}'>"
                    f"{cost_label}: <b>¥{cost:,.0f}</b> → "
                    f"<b>¥{tgt:,.0f}</b>"
                    f"<br/>{pnl_label}: <b>{'+' if pnl >= 0 else ''}¥{pnl:,.0f}</b>"
                    f"</div>"
                )
            c5.markdown(
                f"<div style='padding-top:6px;color:{color};font-weight:600;'>"
                f"¥{exit_price:,.0f}{sub_html}</div>",
                unsafe_allow_html=True,
            )
        else:
            c5.markdown(
                "<div style='padding-top:6px;color:#64748b;'>—</div>",
                unsafe_allow_html=True,
            )

        if c6.button("✕", key=f"tt_x_{i}", help="Xóa mã này" if is_vn else "Remove this position"):
            remove_idx = i

        rows_after.append({
            "ticker": ticker,
            "entry_price": entry,
            "target_pct": target_pct,
            "shares": shares,
        })

    if remove_idx is not None:
        st.session_state.target_rows.pop(remove_idx)
        st.rerun()

    st.session_state.target_rows = rows_after

    btn_a, btn_b, _ = st.columns([1.2, 1.6, 5])
    if btn_a.button("➕ Thêm cổ phiếu" if is_vn else "➕ Add Position", key="tt_add"):
        st.session_state.target_rows.append(
            {"ticker": "", "entry_price": 0.0, "target_pct": 5.0, "shares": 0}
        )
        st.rerun()
    if btn_b.button("🗑 Xóa tất cả" if is_vn else "🗑 Clear All", key="tt_clear"):
        st.session_state.target_rows = []
        st.rerun()

    valid_rows: list[TargetRow] = []
    for r in st.session_state.target_rows:
        if r["ticker"] and r["entry_price"] > 0:
            try:
                valid_rows.append(
                    TargetRow(
                        ticker=r["ticker"],
                        entry_price=float(r["entry_price"]),
                        target_pct=float(r["target_pct"]),
                        shares=int(r.get("shares", 0) or 0),
                    )
                )
            except ValueError as e:
                st.warning(f"{r['ticker']}: {e}")

    if not valid_rows:
        st.info("Nhập mã + giá mua để xem tổng kết." if is_vn else "Enter ticker + entry price to view summary.")
    else:
        s = summarize(valid_rows)
        total_shares = sum(r.shares for r in valid_rows)
        positions_with_shares = sum(1 for r in valid_rows if r.shares > 0)
        st.markdown("---")
        st.markdown(f"### {'Tổng kết' if is_vn else 'Summary'}")
        m1, m2, m3, m4, m5 = st.columns(5)
        with m1:
            st.markdown(
                f"<div class='metric-tile'><div class='label'>{'Số mã' if is_vn else 'Positions'}</div>"
                f"<div class='value'>{s.position_count}</div></div>",
                unsafe_allow_html=True,
            )
        with m2:
            st.markdown(
                f"<div class='metric-tile'><div class='label'>{'Tổng SL' if is_vn else 'Total Shares'}</div>"
                f"<div class='value'>{total_shares:,}</div></div>",
                unsafe_allow_html=True,
            )
        with m3:
            st.markdown(
                f"<div class='metric-tile'><div class='label'>{'Vốn (¥)' if is_vn else 'Total Cost (¥)'}</div>"
                f"<div class='value'>{s.total_invested:,.0f}</div></div>",
                unsafe_allow_html=True,
            )
        with m4:
            v_class = "pos" if s.total_target_value >= s.total_invested else "neg"
            st.markdown(
                f"<div class='metric-tile {v_class}'><div class='label'>Target Value (¥)</div>"
                f"<div class='value'>{s.total_target_value:,.0f}</div></div>",
                unsafe_allow_html=True,
            )
        with m5:
            pnl_class = "pos" if s.total_profit >= 0 else "neg"
            st.markdown(
                f"<div class='metric-tile {pnl_class}'><div class='label'>{'Lợi nhuận (¥)' if is_vn else 'Profit (¥)'}</div>"
                f"<div class='value'>{s.total_profit:+,.0f}</div></div>",
                unsafe_allow_html=True,
            )

        if total_shares > 0:
            sub_line = (
                f"{'Target trung bình gánh vốn (theo tiền): ' if is_vn else 'Weighted average target (by capital): '}"
                f"<b style='color:#38bdf8;'>+{s.weighted_target_pct:.2f}%</b>"
            )
        else:
            sub_line = (
                f"{'Target trung bình đơn giản (chưa nhập SL): ' if is_vn else 'Simple average target (no shares): '}"
                f"<b style='color:#38bdf8;'>+{s.weighted_target_pct:.2f}%</b>"
            )
        st.markdown(
            f"<div style='text-align:center;color:#94a3b8;margin-top:10px;'>{sub_line}</div>",
            unsafe_allow_html=True,
        )

        with st.expander("Chi tiết" if is_vn else "Detail"):
            detail = pd.DataFrame([{
                "Mã" if is_vn else "Ticker": r.ticker,
                "SL" if is_vn else "Shares": r.shares,
                "Giá mua" if is_vn else "Entry Price": f"¥{r.entry_price:,.0f}",
                "Vốn" if is_vn else "Cost": f"¥{r.position_value:,.0f}" if r.shares > 0 else "—",
                "Target %": f"{r.target_pct:+.1f}%",
                "Giá bán" if is_vn else "Exit Price": f"¥{r.exit_price:,.0f}",
                "Target Value": f"¥{r.target_value:,.0f}" if r.shares > 0 else "—",
                "Lãi (¥)" if is_vn else "Profit (¥)": (
                    f"{'+' if r.target_value - r.position_value >= 0 else ''}"
                    f"¥{r.target_value - r.position_value:,.0f}"
                ) if r.shares > 0 else "—",
            } for r in valid_rows])
            st.dataframe(detail, width='stretch', hide_index=True)
            st.download_button(
                "Download CSV",
                data=detail.to_csv(index=False).encode("utf-8"),
                file_name=f"profit_targets_{datetime.now():%Y%m%d_%H%M}.csv",
                mime="text/csv",
                key="dl_targets",
            )

    # Persist rows to SQLite on every render
    user_store.save_target_rows(USER_ID, st.session_state.target_rows)


# ============================
# TAB 10: Alerts
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
        value="\n".join(DEFAULT_TICKERS_10),
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
    is_vn = st.session_state.get("sb_lang", "EN") == "VN"

    if is_vn:
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

    else:
        st.subheader("Technical Indicators & Trading Strategies Guide")

        # ---- Technical Indicators ----
        st.markdown("---")
        st.markdown("## 1. Technical Indicators")

        with st.expander("SMA — Simple Moving Average", expanded=False):
            st.markdown("""
**Definition:**
The arithmetic mean of closing prices over the last N periods.

**Formula:**

```
SMA(N) = (C₁ + C₂ + ... + Cₙ) / N
```

Where `Cᵢ` is the closing price at period i.

**How to Read:**
| Pattern | Meaning |
|---|---|
| Price crosses above SMA | Bullish signal |
| Price crosses below SMA | Bearish signal |
| Short SMA crosses above Long SMA (Golden Cross) | Strong uptrend |
| Short SMA crosses below Long SMA (Death Cross) | Strong downtrend |

**Application in System:**
- **SMA 20**: Short-term trend indicator. Price > SMA20 represents short-term bullishness.
- **SMA 50**: Medium-term trend. Used as dynamic support during pullbacks.
- **SMA 200**: Long-term trend. Price > SMA200 signifies long-term bull market.

**Drawback:** Lagging indicator — reacts slower compared to real-time price action.
            """)

        with st.expander("RSI — Relative Strength Index", expanded=False):
            st.markdown("""
**Definition:**
Momentum oscillator measuring the speed and change of price movements. Ranges from 0 to 100.

**Formula:**

```
RS = Avg Gain(N) / Avg Loss(N)
RSI = 100 - (100 / (1 + RS))
```

N typically = 14 periods.

**How to Read:**
| Value | Meaning |
|---|---|
| RSI > 70 | Overbought — potential downward reversal |
| RSI < 30 | Oversold — potential upward reversal |
| RSI 40-60 | Neutral zone |
| RSI > 80 | Extremely overbought — exercise caution when buying |

**Application in System:**
- PullbackMA strategy uses `RSI < 60` as a filter — avoiding buying when prices are overextended.
- Combine RSI with SMA: low RSI + price touching SMA = high-probability buy zone.

**Note:** RSI can stay overbought/oversold for extended periods during strong trends. Do not rely on RSI in isolation.
            """)

        with st.expander("ATR — Average True Range", expanded=False):
            st.markdown("""
**Definition:**
Measures the average price volatility over N periods. Expressed in absolute price value (e.g., JPY).

**Formula:**

```
True Range = max(
    High - Low,
    |High - Previous Close|,
    |Low - Previous Close|
)
ATR = SMA(True Range, N)
```

N typically = 14 periods.

**How to Read:**
| ATR Value | Meaning |
|---|---|
| High ATR | High volatility — requires wider stop-loss distance |
| Low ATR | Low volatility — consolidation or sideway market |
| Rising ATR | Volatility is expanding; trend is strengthening |
| Falling ATR | Volatility is contracting; trend is weakening |

**Application in System:**
- **ATR-based Stop-loss**: `SL = Entry - 2 × ATR` — sets stop loss based on actual asset volatility.
- **Position Sizing**: Higher ATR -> higher risk per share -> reduced share size -> capital protection.

**Note:** ATR measures price volatility, not price direction.
            """)

        with st.expander("Bollinger Bands", expanded=False):
            st.markdown("""
**Definition:**
Volatility bands placed above and below a simple moving average.

**Formula:**

```
Middle Band = SMA(Close, 20)
Upper Band  = Middle + 2 × StdDev(Close, 20)
Lower Band  = Middle - 2 × StdDev(Close, 20)
```

**How to Read:**
| Pattern | Meaning |
|---|---|
| Price touches Upper Band | Price is relatively high — potential reversion to mean |
| Price touches Lower Band | Price is relatively low — potential bounce upwards |
| Bands Squeeze | Low volatility — usually precedes strong breakouts |
| Bands Expand | High volatility — trend is strengthening |

**Application:**
- Spot consolidation periods (squeeze) before major breakouts.
- Price breaking through the Upper Band in a strong trend = continuation buy signal.
            """)

        with st.expander("Volume & Volume SMA", expanded=False):
            st.markdown("""
**Definition:**
- **Volume**: Total number of shares traded in a session.
- **Volume SMA(20)**: 20-period average of volume.

**Formula:**

```
Volume Ratio = Current Volume / Volume SMA(20)
```

**How to Read:**
| Volume Ratio | Meaning |
|---|---|
| > 1.5 | Unusual volume (1.5x) — potential breakout/breakdown |
| > 2.0 | High conviction volume — confirms breakout |
| < 0.5 | Dry liquidity — lack of market interest |

**Application in System:**
- **VolumeBreakout** strategy requires `Volume > 1.5 × Volume SMA(20)` to confirm breakout validity.
- Breakout without volume is considered a false breakout.

**Note:** Volume is a leading indicator — it often peaks before price.
            """)

        # ---- Trading Strategies ----
        st.markdown("---")
        st.markdown("## 2. Trading Strategies")

        with st.expander("Volume Breakout Strategy", expanded=False):
            st.markdown("""
**Goal:** Catch price breakouts out of consolidation zones backed by heavy buying volume.

**Trigger Conditions (All 3 must be met):**
1. **Price Breakout**: Close > highest High of the last N periods (default 20).
2. **Heavy Volume**: Volume > 1.5 × Volume SMA(20).
3. **Uptrend**: Close > SMA(20).

**Stop-loss:**
- Default: `Entry - 2 × ATR(14)`
- Hard stop: Maximum 7% from entry price (whichever is tighter).

**Real-world Example:**
```
Ticker: 7203 (Toyota)
Close Price: ¥2,800
20-day Highest High: ¥2,750
Volume: 50,000 shares
Volume SMA(20): 25,000 shares → Ratio = 2.0 ✅
SMA(20): ¥2,720 → Close > SMA ✅
ATR(14): ¥45 → SL = 2,800 - 2×45 = ¥2,710
```

**Pros:**
- High success rate when breakouts are backed by strong volume.
- Flexible ATR-based stop-loss adapts to asset volatility.

**Cons:**
- Prone to false breakouts in choppy, range-bound markets.
- Requires historical data (≥ 20 days) for calculations.
            """)

        with st.expander("Pullback MA Strategy", expanded=False):
            st.markdown("""
**Goal:** Buy the dip when price pulls back to the SMA20 or SMA50 in a long-term uptrend.

**Trigger Conditions (All 4 must be met):**
1. **Long-term Uptrend**: Close > SMA(200).
2. **Pullback to MA**: Previous Close < SMA(20/50), Current Close ≥ SMA(20/50) (bullish crossover).
3. **Not Overheated**: RSI(14) < 60.
4. **Clean Pullback**: Price recently traded below the MA before crossing back above.

**Stop-loss:**
- Default: `Entry - 2 × ATR(14)`
- Hard stop: Maximum 7%.

**Real-world Example:**
```
Ticker: 6758 (Sony Group)
Close Price: ¥12,500
SMA(200): ¥11,800 → Close > SMA200 ✅ (Long-term uptrend)
SMA(50): ¥12,480 → Prev Day: ¥12,400 < ¥12,480 ✅
                    Current Day: ¥12,500 ≥ ¥12,480 ✅ (Bullish crossover)
RSI(14): 55 → < 60 ✅
ATR(14): ¥180 → SL = 12,500 - 2×180 = ¥12,140
```

**Pros:**
- Buying support in strong uptrends yields favorable risk-to-reward ratios.
- Lower risk compared to chasing breakouts.
- Suitable for medium-term holding (2-8 weeks).

**Cons:**
- Fails if the primary long-term trend reverses (price breaks below SMA200).
- Requires close monitoring of price action at the moving averages.
            """)

        # ---- Risk Management ----
        st.markdown("---")
        st.markdown("## 3. Risk Management")

        with st.expander("Position Sizing — The 1% Rule", expanded=False):
            st.markdown("""
**Principle:**
Never risk more than **1% of your total capital** on any single trade.

**Formula:**

```
Risk Amount = Total Capital × 1%
Risk per Share = Entry Price - Stop Loss
Shares = Risk Amount / Risk per Share
Position Value = Shares × Entry Price
```

**Example:**
```
Total Capital: ¥10,000,000
Risk per trade: 1% = ¥100,000
Entry Price: ¥2,800
Stop-loss: ¥2,710
Risk per share: ¥90

Shares = 100,000 / 90 = 1,111 shares (rounded down)
Position value = 1,111 × 2,800 = ¥3,110,800 (31% of capital)
```

**Why it is critical:**
- Even after 10 consecutive losses, you only lose ~10% of capital.
- Protects account from "death spirals" (large drawdowns).
- Promotes disciplined trading since risk is pre-calculated.
            """)

        with st.expander("Hard Stop-Loss 7%", expanded=False):
            st.markdown("""
**Rule:**
Every trade must have a stop-loss. Sell immediately if price drops > 7% from entry.

**Formula:**

```
Hard Stop = Entry Price × (1 - 7%)
Final Stop = max(ATR-based SL, Hard Stop)
```

**Why 7%?**
- Historically on TSE, stocks dropping > 7% tend to continue falling.
- Acceptable threshold for short/medium-term swing trading.
- Combined with the 1% risk rule, it ensures absolute capital safety.

**Common Mistakes:**
| Mistake | Consequence |
|---|---|
| Trading without stop-loss | Catastrophic losses (can lose 30-50%) |
| Lowering stop-loss mid-trade | "Hopium" trading -> larger drawdowns |
| Exiting immediately at SL | Correct — discipline is the key |
            """)

        # ---- Fundamental Analysis ----
        st.markdown("---")
        st.markdown("## 4. Fundamental Ratios")

        with st.expander("P/E — Price to Earnings Ratio", expanded=False):
            st.markdown("""
**Formula:**

```
P/E = Stock Price / EPS (Earnings Per Share)
```

**How to Read:**
| P/E Ratio | Meaning |
|---|---|
| < 10 | Cheap — potentially undervalued, or business is in decline |
| 10-15 | Fair value — price reflects current earnings |
| 15-25 | Expensive — market expects strong future growth |
| > 30 | Very expensive — requires high growth to justify valuation |

**Application:** Filter for stocks trading at a reasonable multiple. P/E < 20 is a standard benchmark.
            """)

        with st.expander("P/B — Price to Book Ratio", expanded=False):
            st.markdown("""
**Formula:**

```
P/B = Stock Price / Book Value per Share
     = Market Cap / Net Tangible Assets
```

**How to Read:**
| P/B Ratio | Meaning |
|---|---|
| < 1 | Discount to book value — potentially undervalued |
| 1-2 | Fair valuation for banking & manufacturing sectors |
| > 3 | Expensive — common for high-growth tech & service sectors |

**Application:** P/B < 3 is widely used for value investing screens.
            """)

        with st.expander("ROE — Return on Equity", expanded=False):
            st.markdown("""
**Formula:**

```
ROE = Net Income / Shareholders' Equity
```

**How to Read:**
| ROE | Meaning |
|---|---|
| < 5% | Poor efficiency in capital usage |
| 5-10% | Average performance |
| 10-20% | Good — efficient capital management |
| > 20% | Outstanding — indicates strong moat/competitive advantage |

**Application:** ROE > 10% is preferred to select high-quality growth stocks.
            """)

        with st.expander("EPS — Earnings per Share", expanded=False):
            st.markdown("""
**Formula:**

```
EPS = Net Income / Outstanding Shares
```

**Meaning:**
- EPS > 0: Profitable company.
- Growing EPS: Expanding operations and health.
- Negative EPS: Operating at a loss.

**Application:** Exclude loss-making companies by requiring positive EPS.
            """)

        with st.expander("Dividend Yield", expanded=False):
            st.markdown("""
**Formula:**

```
Dividend Yield = Dividend per Share / Stock Price
```

**How to Read:**
| Yield | Meaning |
|---|---|
| < 1% | Low — focus on capital reinvestment |
| 1-3% | Average — balanced growth and yield |
| 3-5% | High — great passive income source |
| > 5% | Very high — check dividend safety and payout ratio |

**Application:** Yield > 1% is useful to screen for dividend-paying companies.
            """)

        with st.expander("Market Capitalization", expanded=False):
            st.markdown("""
**Formula:**

```
Market Cap = Stock Price × Outstanding Shares
```

**TSE Categorization:**
| Market Cap | Category | Characteristics |
|---|---|---|
| > ¥1,000B | Large Cap | Stable, highly liquid, slower growth |
| ¥100B - ¥1,000B | Mid Cap | Balanced growth and stability |
| < ¥100B | Small Cap | High growth potential, high risk, lower liquidity |

**Application:** Filter by market capitalization to match your personal risk profile.
            """)
