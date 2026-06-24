"""
Data Loader Module — TSE stock data ingestion with caching.

Abstract base class for pluggable data sources (yfinance, J-Quants, Rakuten).
Default implementation uses yfinance with .T suffix auto-append for JP tickers.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)


class BaseDataLoader(ABC):
    """Abstract interface for stock data providers.

    Subclasses must implement fetch_ohlcv and fetch_fundamentals.
    This allows swapping yfinance for J-Quants / Rakuten without touching
    downstream code (screener, technical engine, risk, app).
    """

    @abstractmethod
    def fetch_ohlcv(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Fetch OHLCV data.

        Args:
            ticker: Raw ticker symbol (e.g. '7203' or '7203.T').
            start: Start date string 'YYYY-MM-DD'.
            end: End date string 'YYYY-MM-DD'.
            interval: Bar interval ('1d', '1h', '1wk', etc.).

        Returns:
            DataFrame with columns [Open, High, Low, Close, Volume].
        """
        ...

    @abstractmethod
    def fetch_fundamentals(self, ticker: str) -> dict:
        """Fetch fundamental data for a single ticker.

        Args:
            ticker: Raw ticker symbol.

        Returns:
            Dict with keys: pe, pb, roe, eps, dividend_yield, market_cap.
            Values are float or None if unavailable.
        """
        ...

    @abstractmethod
    def normalize_ticker(self, ticker: str) -> str:
        """Normalize ticker to the provider's expected format."""
        ...


class YFinanceDataLoader(BaseDataLoader):
    """yfinance implementation with local CSV/Parquet caching.

    Automatically appends '.T' suffix for raw JP tickers (e.g. '7203' -> '7203.T').
    Caches OHLCV to disk to avoid repeated API calls and rate-limit blocks.
    """

    _SUFFIX = ".T"
    _CACHE_EXPIRY_HOURS = 24

    def normalize_ticker(self, ticker: str) -> str:
        """Append .T if not already present."""
        ticker = ticker.strip().upper()
        if not ticker.endswith(self._SUFFIX):
            return ticker + self._SUFFIX
        return ticker

    def _cache_path(self, ticker: str, start: str, end: str, interval: str) -> Path:
        safe = ticker.replace(".", "_")
        return CACHE_DIR / f"{safe}_{start}_{end}_{interval}.parquet"

    def _is_cache_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        return datetime.now() - mtime < timedelta(hours=self._CACHE_EXPIRY_HOURS)

    def fetch_ohlcv(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Fetch OHLCV via yfinance with disk caching.

        Args:
            ticker: Raw ticker (e.g. '7203').
            start: Start date 'YYYY-MM-DD'.
            end: End date 'YYYY-MM-DD'.
            interval: Bar interval.

        Returns:
            OHLCV DataFrame indexed by date.

        Raises:
            ValueError: If returned DataFrame is empty (bad ticker / no data).
        """
        import yfinance as yf

        normalized = self.normalize_ticker(ticker)
        cache = self._cache_path(normalized, start, end, interval)

        if self._is_cache_fresh(cache):
            logger.info("Cache hit: %s", normalized)
            return pd.read_parquet(cache)

        logger.info("Fetching OHLCV: %s [%s -> %s]", normalized, start, end)
        try:
            data = yf.download(
                normalized, start=start, end=end, interval=interval, progress=False
            )
        except Exception:
            logger.exception("yfinance download failed for %s", normalized)
            raise

        if data is None or data.empty:
            raise ValueError(f"No data returned for {normalized}")

        data.to_parquet(cache)
        logger.info("Cached %d bars for %s", len(data), normalized)
        return data

    def fetch_fundamentals(self, ticker: str) -> dict:
        """Fetch fundamentals via yfinance .info property.

        Args:
            ticker: Raw ticker.

        Returns:
            Dict of fundamental metrics. Missing keys -> None.
        """
        import yfinance as yf

        normalized = self.normalize_ticker(ticker)
        logger.info("Fetching fundamentals: %s", normalized)

        try:
            info = yf.Ticker(normalized).info
        except Exception:
            logger.exception("yfinance info failed for %s", normalized)
            return {k: None for k in ("pe", "pb", "roe", "eps", "dividend_yield", "market_cap", "sector", "industry")}

        return {
            "pe": info.get("trailingPE"),
            "pb": info.get("priceToBook"),
            "roe": info.get("returnOnEquity"),
            "eps": info.get("trailingEps"),
            "dividend_yield": info.get("dividendYield"),
            "market_cap": info.get("marketCap"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
        }

    def fetch_batch_fundamentals(self, tickers: list[str]) -> dict[str, dict]:
        """Fetch fundamentals for multiple tickers.

        Args:
            tickers: List of raw ticker strings.

        Returns:
            Dict mapping raw ticker -> fundamentals dict.
        """
        results: dict[str, dict] = {}
        for t in tickers:
            results[t] = self.fetch_fundamentals(t)
        return results
