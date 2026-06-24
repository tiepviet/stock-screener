"""
Data Loader Module — TSE stock data ingestion with caching.

Abstract base class for pluggable data sources (yfinance, J-Quants, Rakuten).
Default implementation uses yfinance with .T suffix auto-append for JP tickers.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# Fundamentals cache: JSON files per ticker, 24h expiry
FUND_CACHE_DIR = CACHE_DIR / "fundamentals"
FUND_CACHE_DIR.mkdir(exist_ok=True)

# In-memory cache for fundamentals to avoid repeated disk reads
_fund_cache_mem: dict[str, tuple[datetime, dict]] = {}
_FUND_MEM_TTL = timedelta(hours=1)


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
    def fetch_fundamentals(self, ticker: str) -> dict[str, Any]:
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
    """yfinance implementation with local Parquet/JSON caching.

    Automatically appends '.T' suffix for raw JP tickers (e.g. '7203' -> '7203.T').
    Caches OHLCV to disk to avoid repeated API calls and rate-limit blocks.
    Caches fundamentals to disk + memory for 24h.
    """

    _SUFFIX = ".T"
    _CACHE_EXPIRY_HOURS = 24

    def normalize_ticker(self, ticker: str) -> str:
        """Append .T if not already present.

        Raises:
            ValueError: If ticker is empty or contains invalid characters.
        """
        if not ticker or not ticker.strip():
            raise ValueError("Ticker must be non-empty")
        ticker = ticker.strip().upper()
        if any(c in ticker for c in ("/", "\\", " ", "\n", "\t")):
            raise ValueError(f"Invalid ticker: {ticker!r}")
        if not ticker.endswith(self._SUFFIX):
            return ticker + self._SUFFIX
        return ticker

    # --- OHLCV cache ---

    def _cache_path(self, ticker: str, start: str, end: str, interval: str) -> Path:
        safe = ticker.replace(".", "_")
        return CACHE_DIR / f"{safe}_{start}_{end}_{interval}.parquet"

    def _is_cache_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        return datetime.now() - mtime < timedelta(hours=self._CACHE_EXPIRY_HOURS)

    def _evict_stale_cache(self) -> int:
        """Delete OHLCV cache files older than 7 days.

        Returns:
            Number of files deleted.
        """
        deleted = 0
        cutoff = datetime.now() - timedelta(days=7)
        for f in CACHE_DIR.glob("*.parquet"):
            # Skip fundamentals subdir files
            if f.parent != CACHE_DIR:
                continue
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                f.unlink(missing_ok=True)
                deleted += 1
        if deleted:
            logger.info("Evicted %d stale cache files", deleted)
        return deleted

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

        # Flatten multi-level columns from newer yfinance versions
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        data.to_parquet(cache)
        logger.info("Cached %d bars for %s", len(data), normalized)
        return data

    # --- Fundamentals cache (disk + memory) ---

    def _fund_cache_path(self, ticker: str) -> Path:
        safe = ticker.replace(".", "_")
        return FUND_CACHE_DIR / f"{safe}.json"

    def _read_fund_cache(self, ticker: str) -> dict[str, Any] | None:
        """Read fundamentals from memory, then disk cache."""
        # Memory cache
        if ticker in _fund_cache_mem:
            cached_at, data = _fund_cache_mem[ticker]
            if datetime.now() - cached_at < _FUND_MEM_TTL:
                return data

        # Disk cache
        path = self._fund_cache_path(ticker)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                cached_at = datetime.fromisoformat(data.get("_cached_at", ""))
                if datetime.now() - cached_at < timedelta(hours=self._CACHE_EXPIRY_HOURS):
                    result = {k: v for k, v in data.items() if not k.startswith("_")}
                    _fund_cache_mem[ticker] = (cached_at, result)
                    return result
            except Exception:
                logger.debug("Fund cache read failed for %s", ticker)

        return None

    def _write_fund_cache(self, ticker: str, data: dict[str, Any]) -> None:
        """Write fundamentals to disk + memory cache."""
        data_with_ts = {**data, "_cached_at": datetime.now().isoformat()}
        path = self._fund_cache_path(ticker)
        try:
            path.write_text(json.dumps(data_with_ts, default=str))
        except Exception:
            logger.debug("Fund cache write failed for %s", ticker)
        _fund_cache_mem[ticker] = (datetime.now(), data)

    def fetch_fundamentals(self, ticker: str) -> dict[str, Any]:
        """Fetch fundamentals via yfinance .info property (with caching).

        Args:
            ticker: Raw ticker.

        Returns:
            Dict of fundamental metrics. Missing keys -> None.
        """
        import yfinance as yf

        normalized = self.normalize_ticker(ticker)

        # Check cache first
        cached = self._read_fund_cache(normalized)
        if cached is not None:
            logger.info("Fundamentals cache hit: %s", normalized)
            return cached

        logger.info("Fetching fundamentals: %s", normalized)
        try:
            ticker_obj = yf.Ticker(normalized)
            info = ticker_obj.info
        except Exception:
            logger.exception("yfinance info failed for %s", normalized)
            info = None

        if info is None:
            result = {k: None for k in ("pe", "pb", "roe", "eps", "dividend_yield", "market_cap", "sector", "industry")}
        else:
            result = {
                "pe": info.get("trailingPE"),
                "pb": info.get("priceToBook"),
                "roe": info.get("returnOnEquity"),
                "eps": info.get("trailingEps"),
                "dividend_yield": info.get("dividendYield"),
                "market_cap": info.get("marketCap"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
            }

        self._write_fund_cache(normalized, result)
        return result

    def fetch_batch_fundamentals(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch fundamentals for multiple tickers (uses cache).

        Args:
            tickers: List of raw ticker strings.

        Returns:
            Dict mapping raw ticker -> fundamentals dict.
        """
        results: dict[str, dict[str, Any]] = {}
        for t in tickers:
            results[t] = self.fetch_fundamentals(t)
        return results
