"""
Screen Chaining — multi-pass screener with weighted scoring.

Pass 1: Fundamental filter (reduce universe from ~3000 to ~200).
Pass 2: Technical filter (reduce to ~20-30).
Pass 3: Weighted score ranking (pick top N).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from .data_loader import BaseDataLoader, YFinanceDataLoader
from .fundamental_screener import Condition, FundamentalScreener
from .technical_engine import (
    TechnicalEngine,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Score model
# ---------------------------------------------------------------------------

@dataclass
class ScoredStock:
    """A stock with a composite weighted score."""

    ticker: str
    score: float
    rank: int = 0
    fundamental_score: float = 0.0
    technical_score: float = 0.0
    sector: str = ""
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"#{self.rank} {self.ticker} score={self.score:.2f} ({self.sector})"


# ---------------------------------------------------------------------------
# Screener chain
# ---------------------------------------------------------------------------

class ScreenChainer:
    """Multi-pass screener: fundamental -> technical -> weighted ranking.

    Default weights:
      - ROE: 25%
      - P/E: 20%
      - Trend (above SMA200): 20%
      - Volume confirmation: 15%
      - RSI position: 10%
      - Dividend yield: 10%
    """

    def __init__(
        self,
        loader: BaseDataLoader | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        """Initialize chainer.

        Args:
            loader: Data loader instance.
            weights: Dict of factor -> weight (must sum to 1.0).
        """
        self.loader = loader or YFinanceDataLoader()
        self.engine = TechnicalEngine()
        self.fundamental_screener = FundamentalScreener(self.loader)

        self.weights = weights or {
            "roe": 0.25,
            "pe": 0.20,
            "trend": 0.20,
            "volume": 0.15,
            "rsi": 0.10,
            "dividend": 0.10,
        }

    def run(
        self,
        tickers: list[str],
        fundamental_conditions: list[Condition] | None = None,
        top_n: int = 20,
        lookback_days: int = 365,
    ) -> list[ScoredStock]:
        """Run the full screening pipeline.

        Args:
            tickers: Full universe of tickers.
            fundamental_conditions: Pass 1 filters. Defaults to standard value screen.
            top_n: Number of top stocks to return.
            lookback_days: Days of technical data.

        Returns:
            List of ScoredStock ranked by composite score.
        """
        # --- Pass 1: Fundamental filter ---
        if fundamental_conditions is None:
            fundamental_conditions = [
                Condition("roe", ">", 0.08),
                Condition("pe", "<", 20.0),
                Condition("pb", "<", 2.0),
                Condition("eps", ">", 0),
                Condition("dividend_yield", ">", 0.005),
            ]

        logger.info("Pass 1: Fundamental filter on %d tickers", len(tickers))
        fundies = self.fundamental_screener.screen(tickers, fundamental_conditions)
        passed_fundamental = fundies["ticker"].tolist() if not fundies.empty else []
        logger.info("Pass 1 result: %d tickers", len(passed_fundamental))

        if not passed_fundamental:
            return []

        # --- Pass 2 + 3: Technical filter + scoring ---
        logger.info("Pass 2+3: Technical filter + scoring on %d tickers", len(passed_fundamental))
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        scored: list[ScoredStock] = []
        fundies_dict = fundies.set_index("ticker").to_dict("index") if not fundies.empty else {}

        for ticker in passed_fundamental:
            try:
                df = self.loader.fetch_ohlcv(ticker, start, end)
                df = self.engine.enrich(df)

                if len(df) < 200:
                    logger.debug("%s: insufficient data (%d bars)", ticker, len(df))
                    continue

                # Technical filter: must be above SMA200 (uptrend)
                if "SMA_200" not in df.columns:
                    logger.debug("%s: SMA_200 not available", ticker)
                    continue
                sma200 = df["SMA_200"].iloc[-1]
                close = df["Close"].iloc[-1]
                if pd.isna(sma200) or close < sma200:
                    logger.debug("%s: below SMA200 — filtered out", ticker)
                    continue

                # Compute scores
                fund_data = fundies_dict.get(ticker, {})
                tech_score = self._compute_technical_score(df)
                fund_score = self._compute_fundamental_score(fund_data)

                composite = sum(
                    self.weights.get(k, 0) * v
                    for k, v in {**fund_score, **tech_score}.items()
                )

                scored.append(
                    ScoredStock(
                        ticker=ticker,
                        score=round(composite, 4),
                        fundamental_score=round(sum(fund_score.values()) / max(len(fund_score), 1), 4),
                        technical_score=round(sum(tech_score.values()) / max(len(tech_score), 1), 4),
                        sector=fund_data.get("sector", ""),
                        details={**fund_data, **{k: round(v, 4) for k, v in {**fund_score, **tech_score}.items()}},
                    )
                )

            except Exception:
                logger.exception("Technical scoring failed for %s", ticker)

        # Sort by composite score, then ticker (stable)
        scored.sort(key=lambda s: (-s.score, s.ticker))
        for i, s in enumerate(scored):
            s.rank = i + 1

        return scored[:top_n]

    def _compute_fundamental_score(self, data: dict) -> dict[str, float]:
        """Normalize fundamental metrics to 0-1 scores.

        Args:
            data: Dict of fundamental metrics.

        Returns:
            Dict of normalized scores.
        """
        scores: dict[str, float] = {}

        # ROE: 0-30% -> 0-1
        roe = data.get("roe") or 0
        scores["roe"] = min(max(roe / 0.30, 0), 1)

        # P/E: 0-50 -> inverse (lower is better) -> 0-1
        pe = data.get("pe") or 50
        scores["pe"] = min(max(1 - pe / 50, 0), 1)

        # Dividend yield: 0-5% -> 0-1
        div = data.get("dividend_yield") or 0
        scores["dividend"] = min(max(div / 0.05, 0), 1)

        return scores

    def _compute_technical_score(self, df: pd.DataFrame) -> dict[str, float]:
        """Normalize technical indicators to 0-1 scores.

        Args:
            df: Enriched OHLCV DataFrame.

        Returns:
            Dict of normalized scores.
        """
        scores: dict[str, float] = {}
        last = df.iloc[-1]

        # Trend: distance above SMA200 (0-20% -> 0-1)
        if not pd.isna(last.get("SMA_200")) and last["SMA_200"] > 0:
            trend_dist = (last["Close"] - last["SMA_200"]) / last["SMA_200"]
            scores["trend"] = min(max(trend_dist / 0.20, 0), 1)

        # Volume: latest volume vs SMA20 (1-3x -> 0-1)
        if not pd.isna(last.get("VOL_SMA_20")) and last["VOL_SMA_20"] > 0:
            vol_ratio = last["Volume"] / last["VOL_SMA_20"]
            scores["volume"] = min(max((vol_ratio - 1) / 2, 0), 1)

        # RSI: 30-70 -> bell curve, peak at 50
        rsi = last.get("RSI_14")
        if not pd.isna(rsi):
            # Best at 45-55, worst at extremes
            scores["rsi"] = 1 - abs(rsi - 50) / 50

        return scores
