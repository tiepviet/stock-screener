"""
Backtesting Engine — simulate strategy performance on historical data.

Simulates trades using Signal outputs, tracks P/L, and computes
performance metrics: win rate, Sharpe ratio, max drawdown, CAGR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from .technical_engine import BaseStrategy, Signal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trade model
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    """A completed (closed) trade."""

    ticker: str
    strategy: str
    entry_date: datetime
    entry_price: float
    stop_loss: float = 0.0
    exit_date: datetime | None = None
    exit_price: float | None = None
    shares: int = 0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.exit_date is None

    def close(self, exit_date: datetime, exit_price: float, reason: str = "") -> None:
        self.exit_date = exit_date
        self.exit_price = exit_price
        self.exit_reason = reason
        self.pnl = (exit_price - self.entry_price) * self.shares
        self.pnl_pct = (exit_price - self.entry_price) / self.entry_price


# ---------------------------------------------------------------------------
# Backtest result
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    """Aggregated results from a backtest run."""

    ticker: str
    strategy: str
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    sharpe_ratio: float
    max_drawdown: float
    max_drawdown_pct: float
    cagr: float
    total_return_pct: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    def summary(self) -> str:
        """Return a formatted summary string."""
        lines = [
            f"=== Backtest: {self.ticker} ({self.strategy}) ===",
            f"Period:        {self.start_date} -> {self.end_date}",
            f"Capital:       ¥{self.initial_capital:,.0f} -> ¥{self.final_capital:,.0f}",
            f"Total Return:  {self.total_return_pct:.2%}",
            f"CAGR:          {self.cagr:.2%}",
            f"Total Trades:  {self.total_trades}",
            f"Win Rate:      {self.win_rate:.1%}",
            f"Avg Win:       {self.avg_win:.2%}",
            f"Avg Loss:      {self.avg_loss:.2%}",
            f"Profit Factor: {self.profit_factor:.2f}",
            f"Sharpe Ratio:  {self.sharpe_ratio:.2f}",
            f"Max Drawdown:  {self.max_drawdown_pct:.2%} (¥{self.max_drawdown:,.0f})",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

class Backtester:
    """Run a strategy over historical data and simulate trades.

    Default rules:
      - Enter on BUY signal at next-bar open.
      - Exit on stop-loss hit or end of data.
      - 1 position at a time per ticker.
    """

    def __init__(
        self,
        initial_capital: float = 10_000_000,
        risk_per_trade: float = 0.01,
        hard_stop_pct: float = 0.07,
        max_holding_days: int = 60,
        take_profit_pct: float = 0.0,
        commission_pct: float = 0.0,
    ) -> None:
        """Initialize backtester.

        Args:
            initial_capital: Starting capital in JPY.
            risk_per_trade: Fraction of capital risked per trade.
            hard_stop_pct: Maximum loss before forced exit.
            max_holding_days: Max days to hold before forced exit (0 = no limit).
            take_profit_pct: Take-profit threshold from entry (0 = disabled).
            commission_pct: Commission per trade as fraction of position value (0 = none).
        """
        if take_profit_pct < 0:
            raise ValueError("take_profit_pct must be >= 0")
        if not 0 <= commission_pct < 1:
            raise ValueError("commission_pct must be in [0, 1)")
        self.initial_capital = initial_capital
        self.risk_per_trade = risk_per_trade
        self.hard_stop_pct = hard_stop_pct
        self.max_holding_days = max_holding_days
        self.take_profit_pct = take_profit_pct
        self.commission_pct = commission_pct

    def run(
        self,
        df: pd.DataFrame,
        signals: list[Signal],
        ticker: str,
        strategy_name: str,
    ) -> BacktestResult:
        """Run backtest on enriched OHLCV with pre-computed signals.

        Args:
            df: Enriched OHLCV DataFrame (must have Open, High, Low, Close).
            signals: List of BUY signals from strategy.
            ticker: Raw ticker.
            strategy_name: Strategy name for labeling.

        Returns:
            BacktestResult with metrics and trade log.
        """
        capital = self.initial_capital
        trades: list[Trade] = []
        open_trade: Trade | None = None
        equity = [capital]

        # Build signal lookup: date -> list[Signal] (multiple strategies per bar)
        signal_map: dict[datetime, list[Signal]] = {}
        for sig in signals:
            d = sig.date if isinstance(sig.date, datetime) else pd.Timestamp(sig.date).to_pydatetime()
            signal_map.setdefault(d, []).append(sig)

        for i in range(len(df)):
            row = df.iloc[i]
            current_date = df.index[i]
            if isinstance(current_date, pd.Timestamp):
                current_date = current_date.to_pydatetime()

            # --- Check open position ---
            if open_trade is not None:
                days_held = (current_date - open_trade.entry_date).days
                take_profit_price = (
                    open_trade.entry_price * (1 + self.take_profit_pct)
                    if self.take_profit_pct > 0
                    else float("inf")
                )

                # Take-profit (intra-bar: if high >= TP)
                if row["High"] >= take_profit_price:
                    open_trade.close(current_date, take_profit_price, "TAKE_PROFIT")
                    open_trade.pnl -= self.commission_pct * open_trade.entry_price * open_trade.shares
                    capital += open_trade.pnl
                    trades.append(open_trade)
                    open_trade = None

                # Stop-loss check (intra-bar: if low <= SL)
                elif row["Low"] <= open_trade.stop_loss:
                    open_trade.close(current_date, open_trade.stop_loss, "STOP_LOSS")
                    open_trade.pnl -= self.commission_pct * open_trade.entry_price * open_trade.shares
                    capital += open_trade.pnl
                    trades.append(open_trade)
                    open_trade = None

                # Max holding period
                elif self.max_holding_days > 0 and days_held >= self.max_holding_days:
                    open_trade.close(current_date, float(row["Close"]), "MAX_HOLD")
                    open_trade.pnl -= self.commission_pct * open_trade.entry_price * open_trade.shares
                    capital += open_trade.pnl
                    trades.append(open_trade)
                    open_trade = None

            # --- Check for new entry ---
            if open_trade is None and current_date in signal_map:
                sig = signal_map[current_date][0]
                entry_price = float(row["Open"])  # enter at next-bar open
                hard_stop = entry_price * (1 - self.hard_stop_pct)
                strategy_stop = sig.stop_loss if sig.stop_loss else hard_stop
                stop_loss = max(strategy_stop, hard_stop)

                risk_amount = capital * self.risk_per_trade
                risk_per_share = entry_price - stop_loss
                if risk_per_share <= 0:
                    equity.append(capital)
                    continue

                shares = max(1, int(risk_amount / risk_per_share))
                cost = shares * entry_price
                if cost > capital:
                    shares = max(1, int(capital / entry_price))
                    cost = shares * entry_price

                capital -= cost
                open_trade = Trade(
                    ticker=ticker,
                    strategy=strategy_name,
                    entry_date=current_date,
                    entry_price=entry_price,
                    shares=shares,
                    stop_loss=round(stop_loss, 2),
                )

            # Track equity
            if open_trade is not None:
                close = float(row["Close"])
                if pd.isna(close):
                    equity.append(capital)
                else:
                    unrealized = (close - open_trade.entry_price) * open_trade.shares
                    equity.append(capital + unrealized + open_trade.entry_price * open_trade.shares)
            else:
                equity.append(capital)

        # Close any remaining open position at last bar
        if open_trade is not None:
            last_date = df.index[-1]
            if isinstance(last_date, pd.Timestamp):
                last_date = last_date.to_pydatetime()
            open_trade.close(last_date, float(df["Close"].iloc[-1]), "END_OF_DATA")
            open_trade.pnl -= self.commission_pct * open_trade.entry_price * open_trade.shares
            capital += open_trade.pnl
            trades.append(open_trade)

        # --- Compute metrics ---
        equity_series = pd.Series(equity)
        return self._compute_metrics(
            trades=trades,
            equity_curve=equity_series,
            ticker=ticker,
            strategy_name=strategy_name,
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
        )

    def _compute_metrics(
        self,
        trades: list[Trade],
        equity_curve: pd.Series,
        ticker: str,
        strategy_name: str,
        start_date: str,
        end_date: str,
    ) -> BacktestResult:
        """Compute performance metrics from trade list and equity curve."""
        total = len(trades)
        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl <= 0]

        win_rate = len(winners) / total if total > 0 else 0
        avg_win = np.mean([t.pnl_pct for t in winners]) if winners else 0
        avg_loss = np.mean([t.pnl_pct for t in losers]) if losers else 0

        gross_profit = sum(t.pnl for t in winners)
        gross_loss = abs(sum(t.pnl for t in losers))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Sharpe ratio (annualized, assuming daily returns)
        returns = equity_curve.pct_change().dropna()
        if len(returns) > 1 and returns.std() > 0:
            sharpe = (returns.mean() / returns.std()) * np.sqrt(252)
        else:
            sharpe = 0.0

        # Max drawdown
        running_max = equity_curve.cummax()
        drawdown = equity_curve - running_max
        max_dd = abs(drawdown.min()) if len(drawdown) > 0 else 0
        max_dd_pct = (max_dd / running_max.max()) if running_max.max() > 0 else 0

        # CAGR
        final = float(equity_curve.iloc[-1]) if len(equity_curve) > 0 else self.initial_capital
        years = max(1, (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days / 365.25)
        ratio = final / self.initial_capital if self.initial_capital > 0 else 1.0
        # Power on negative base is complex; clamp to 0 (total loss)
        if ratio <= 0:
            cagr = -1.0
        else:
            cagr = ratio ** (1 / years) - 1

        total_return = (final - self.initial_capital) / self.initial_capital

        return BacktestResult(
            ticker=ticker,
            strategy=strategy_name,
            start_date=start_date,
            end_date=end_date,
            initial_capital=self.initial_capital,
            final_capital=round(final, 2),
            total_trades=total,
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=round(win_rate, 4),
            avg_win=round(float(avg_win), 4),
            avg_loss=round(float(avg_loss), 4),
            profit_factor=round(profit_factor, 2),
            sharpe_ratio=round(float(sharpe), 2),
            max_drawdown=round(max_dd, 2),
            max_drawdown_pct=round(float(max_dd_pct), 4),
            cagr=round(float(cagr), 4),
            total_return_pct=round(float(total_return), 4),
            trades=trades,
            equity_curve=equity_curve,
        )

    def run_multi(
        self,
        df: pd.DataFrame,
        strategy: BaseStrategy,
        ticker: str,
    ) -> BacktestResult:
        """Convenience: enrich df, generate signals, and run backtest.

        Args:
            df: Raw OHLCV DataFrame.
            strategy: Strategy instance with generate_signals().
            ticker: Raw ticker.

        Returns:
            BacktestResult.
        """
        from .technical_engine import TechnicalEngine

        engine = TechnicalEngine()
        enriched = engine.enrich(df)
        signals = strategy.generate_signals(enriched, ticker)
        return self.run(enriched, signals, ticker, strategy.name)
