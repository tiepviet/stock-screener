"""
Earnings Calendar — avoid buying before earnings announcements.

Uses yfinance earnings_dates to check if a ticker has upcoming earnings
within a configurable window. Buying before earnings is high-risk due
to potential gap moves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from .data_loader import BaseDataLoader, YFinanceDataLoader

logger = logging.getLogger(__name__)


@dataclass
class EarningsInfo:
    """Earnings information for a single ticker."""

    ticker: str
    next_earnings_date: Optional[datetime] = None
    days_until_earnings: Optional[int] = None
    is_upcoming: bool = False
    last_report_date: Optional[datetime] = None
    estimated_eps: Optional[float] = None
    actual_eps: Optional[float] = None


class EarningsCalendar:
    """Check upcoming earnings dates for tickers.

    Flags tickers that have earnings within N days (default 14).
    Use this to filter out high-risk trades before earnings.
    """

    def __init__(
        self,
        loader: Optional[BaseDataLoader] = None,
        warning_days: int = 14,
    ) -> None:
        """Initialize earnings calendar.

        Args:
            loader: Data loader instance.
            warning_days: Days before earnings to flag as "upcoming" (default 14).
        """
        self.loader = loader or YFinanceDataLoader()
        self.warning_days = warning_days

    def get_earnings(self, ticker: str) -> EarningsInfo:
        """Fetch earnings dates for a single ticker.

        Args:
            ticker: Raw ticker.

        Returns:
            EarningsInfo with next earnings date and status.
        """
        import yfinance as yf

        normalized = self.loader.normalize_ticker(ticker)
        info = EarningsInfo(ticker=ticker)

        try:
            cal = yf.Ticker(normalized).calendar
            if cal is None or (isinstance(cal, pd.DataFrame) and cal.empty):
                logger.info("No earnings data for %s", normalized)
                return info

            # calendar can be a dict or DataFrame depending on yfinance version
            if isinstance(cal, dict):
                earn_date = cal.get("Earnings Date")
                if earn_date:
                    if isinstance(earn_date, list) and len(earn_date) > 0:
                        info.next_earnings_date = pd.Timestamp(earn_date[0]).to_pydatetime()
                    elif isinstance(earn_date, (datetime, pd.Timestamp)):
                        info.next_earnings_date = pd.Timestamp(earn_date).to_pydatetime()

                info.estimated_eps = cal.get("Earnings Average")
                info.actual_eps = cal.get("Earnings Actual")

            elif isinstance(cal, pd.DataFrame):
                # Find next future earnings date
                now = datetime.now()
                for idx in cal.index:
                    try:
                        ed = pd.Timestamp(idx).to_pydatetime()
                        if ed > now:
                            info.next_earnings_date = ed
                            break
                    except Exception:
                        continue

        except Exception:
            logger.exception("Failed to fetch earnings for %s", normalized)

        # Calculate days until earnings
        if info.next_earnings_date:
            delta = info.next_earnings_date - datetime.now()
            info.days_until_earnings = delta.days
            info.is_upcoming = 0 <= delta.days <= self.warning_days

        return info

    def check_batch(
        self,
        tickers: list[str],
    ) -> dict[str, EarningsInfo]:
        """Check earnings for multiple tickers.

        Args:
            tickers: List of raw tickers.

        Returns:
            Dict mapping ticker -> EarningsInfo.
        """
        results: dict[str, EarningsInfo] = {}
        for t in tickers:
            results[t] = self.get_earnings(t)
        return results

    def filter_safe(
        self,
        tickers: list[str],
    ) -> tuple[list[str], list[str]]:
        """Split tickers into safe (no upcoming earnings) and risky groups.

        Args:
            tickers: List of raw tickers.

        Returns:
            Tuple of (safe_tickers, risky_tickers).
        """
        safe: list[str] = []
        risky: list[str] = []

        earnings = self.check_batch(tickers)

        for t in tickers:
            info = earnings[t]
            if info.is_upcoming:
                risky.append(t)
                logger.info(
                    "%s: earnings in %d days (%s) — SKIPPING",
                    t, info.days_until_earnings or 0,
                    info.next_earnings_date.strftime("%Y-%m-%d") if info.next_earnings_date else "?",
                )
            else:
                safe.append(t)

        return safe, risky
