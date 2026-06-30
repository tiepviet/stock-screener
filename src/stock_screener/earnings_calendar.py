"""
Earnings Calendar — avoid buying before earnings announcements.

Uses yfinance earnings_dates to check if a ticker has upcoming earnings
within a configurable window. Buying before earnings is high-risk due
to potential gap moves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from .data_loader import BaseDataLoader, YFinanceDataLoader

logger = logging.getLogger(__name__)


@dataclass
class EarningsInfo:
    """Earnings information for a single ticker."""

    ticker: str
    next_earnings_date: datetime | None = None
    days_until_earnings: int | None = None
    is_upcoming: bool = False
    last_report_date: datetime | None = None
    estimated_eps: float | None = None
    actual_eps: float | None = None


class EarningsCalendar:
    """Check upcoming earnings dates for tickers.

    Flags tickers that have earnings within N days (default 14).
    Use this to filter out high-risk trades before earnings.
    """

    def __init__(
        self,
        loader: BaseDataLoader | None = None,
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
            now = datetime.now()
            now_date = now.date()
            if isinstance(cal, dict):
                earn_date = cal.get("Earnings Date")
                if earn_date:
                    if isinstance(earn_date, list) and len(earn_date) > 0:
                        for ed in earn_date:
                            ts = pd.Timestamp(ed).to_pydatetime()
                            if ts.date() >= now_date:
                                info.next_earnings_date = ts
                                break
                            else:
                                if info.last_report_date is None or ts > info.last_report_date:
                                    info.last_report_date = ts
                    elif isinstance(earn_date, (datetime, pd.Timestamp)):
                        ts = pd.Timestamp(earn_date).to_pydatetime()
                        if ts.date() >= now_date:
                            info.next_earnings_date = ts
                        else:
                            info.last_report_date = ts

                info.estimated_eps = cal.get("Earnings Average")
                info.actual_eps = cal.get("Earnings Actual")

            elif isinstance(cal, pd.DataFrame):
                # Find next future earnings date
                for idx in cal.index:
                    try:
                        ed = pd.Timestamp(idx).to_pydatetime()
                        if ed.date() >= now_date:
                            info.next_earnings_date = ed
                            break
                        else:
                            if info.last_report_date is None or ed > info.last_report_date:
                                info.last_report_date = ed
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
        """Check earnings for multiple tickers (parallel).

        Args:
            tickers: List of raw tickers.

        Returns:
            Dict mapping ticker -> EarningsInfo.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: dict[str, EarningsInfo] = {}
        with ThreadPoolExecutor(max_workers=min(len(tickers), 8)) as pool:
            futures = {pool.submit(self.get_earnings, t): t for t in tickers}
            for future in as_completed(futures):
                t = futures[future]
                results[t] = future.result()
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
