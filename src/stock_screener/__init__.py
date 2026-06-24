"""
TSE Stock Screener — Algorithmic trading & analysis for Tokyo Stock Exchange.

Public API re-exports for `from src.stock_screener import ...`.
"""

__version__ = "1.0.0"
__author__ = "Your Name"

from .backtest import Backtester, BacktestResult, Trade
from .data_loader import BaseDataLoader, YFinanceDataLoader
from .earnings_calendar import EarningsCalendar, EarningsInfo
from .fundamental_screener import (
    Condition,
    FundamentalScreener,
    default_japan_value_conditions,
    growth_conditions,
)
from .multi_timeframe import ConfirmedSignal, MultiTimeframeConfirmer
from .portfolio import PortfolioPosition, PortfolioTracker
from .risk_management import PositionPlan, RiskManager
from .screen_chain import ScoredStock, ScreenChainer
from .technical_engine import (
    BaseStrategy,
    PullbackMAStrategy,
    Signal,
    SignalType,
    TechnicalEngine,
    VolumeBreakoutStrategy,
)

__all__ = [
    "Backtester",
    "BacktestResult",
    "BaseDataLoader",
    "BaseStrategy",
    "Condition",
    "ConfirmedSignal",
    "EarningsCalendar",
    "EarningsInfo",
    "FundamentalScreener",
    "MultiTimeframeConfirmer",
    "PortfolioPosition",
    "PortfolioTracker",
    "PositionPlan",
    "PullbackMAStrategy",
    "RiskManager",
    "ScoredStock",
    "ScreenChainer",
    "Signal",
    "SignalType",
    "TechnicalEngine",
    "Trade",
    "VolumeBreakoutStrategy",
    "YFinanceDataLoader",
    "default_japan_value_conditions",
    "growth_conditions",
]
