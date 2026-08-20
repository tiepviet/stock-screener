"""
Screen Chaining — multi-pass screener with weighted scoring.

Pass 1: Fundamental filter (reduce universe from ~3000 to ~200).
Pass 2: Technical filter (reduce to ~20-30).
Pass 3: Weighted score ranking (pick top N).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from .data_loader import BaseDataLoader, YFinanceDataLoader, jst_now
from .fundamental_screener import Condition, FundamentalScreener
from .technical_engine import (
    TechnicalEngine,
)

logger = logging.getLogger(__name__)


def _as_float(value, default: float = 0.0) -> float:
    """Coerce a (possibly missing/NaN/None) metric to a float."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if pd.isna(f) else f


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

    Scores are normalized the same way everywhere (weighted average over the
    factor's weight bucket). Factors with missing data score 0 AND still count
    in the denominator, so incomplete data is penalized, never rewarded.
    """

    # Weight buckets for sub-scores. Keys must exist in `weights`.
    _FUND_FACTORS = ("roe", "pe", "dividend")
    _TECH_FACTORS = ("trend", "volume", "rsi")

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
                Condition("pb", "<", 3.0),
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
        end = jst_now().strftime("%Y-%m-%d")
        start = (jst_now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        scored: list[ScoredStock] = []
        fundies_dict = fundies.set_index("ticker").to_dict("index") if not fundies.empty else {}

        def _score_one(ticker: str) -> ScoredStock | None:
            try:
                df = self.loader.fetch_ohlcv(ticker, start, end)
                df = self.engine.enrich(df)

                if len(df) < 200:
                    logger.debug("%s: insufficient data (%d bars)", ticker, len(df))
                    return None

                # Technical filter: must be above SMA200 (uptrend)
                if "SMA_200" not in df.columns:
                    logger.debug("%s: SMA_200 not available", ticker)
                    return None
                sma200 = df["SMA_200"].iloc[-1]
                close = df["Close"].iloc[-1]
                if pd.isna(sma200) or pd.isna(close) or close < sma200:
                    logger.debug("%s: below SMA200 — filtered out", ticker)
                    return None

                # Compute scores
                fund_data = fundies_dict.get(ticker, {})
                tech_score = self._compute_technical_score(df)
                fund_score = self._compute_fundamental_score(fund_data)

                # Same normalization everywhere: weighted avg over the factor's
                # bucket; missing factors contribute 0 but keep their weight in
                # the denominator (penalty for incomplete data).
                fundamental_score = self._weighted_avg(fund_score, self._FUND_FACTORS)
                technical_score = self._weighted_avg(tech_score, self._TECH_FACTORS)
                composite = self._weighted_avg({**fund_score, **tech_score}, self.weights)

                return ScoredStock(
                    ticker=ticker,
                    score=round(composite, 4),
                    fundamental_score=round(fundamental_score, 4),
                    technical_score=round(technical_score, 4),
                    sector=fund_data.get("sector", ""),
                    details={**fund_data, **{k: round(v, 4) for k, v in {**fund_score, **tech_score}.items()}},
                )
            except Exception:
                logger.exception("Technical scoring failed for %s", ticker)
                return None

        # Pass 2 fetches one OHLCV per ticker (1-2s each over the network) —
        # run them in parallel like AlertScanner does, capped at 8 workers.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(len(passed_fundamental), 8)) as pool:
            for result in pool.map(_score_one, passed_fundamental):
                if result is not None:
                    scored.append(result)

        # Sort by composite score, then ticker (stable)
        scored.sort(key=lambda s: (-s.score, s.ticker))
        for i, s in enumerate(scored):
            s.rank = i + 1

        if top_n < 0:
            raise ValueError(f"top_n must be >= 0, got {top_n}")
        return scored[:top_n]

    def _weighted_avg(
        self,
        scores: dict[str, float],
        factor_keys: dict[str, float] | tuple[str, ...],
    ) -> float:
        """Weighted average of present scores over the factor bucket.

        Missing factors count as 0 with their weight still in the denominator,
        so incomplete data drags the score down instead of boosting it.
        """
        if isinstance(factor_keys, dict):
            keys = factor_keys.keys()
            weights = factor_keys
        else:
            keys = factor_keys
            weights = self.weights
        denom = sum(weights.get(k, 0) for k in keys)
        if denom <= 0:
            return 0.0
        num = 0.0
        for k in keys:
            v = _as_float(scores.get(k), 0.0)
            num += weights.get(k, 0) * v
        return num / denom

    def _compute_fundamental_score(self, data: dict) -> dict[str, float]:
        """Normalize fundamental metrics to 0-1 scores.

        Args:
            data: Dict of fundamental metrics.

        Returns:
            Dict of normalized scores.
        """
        scores: dict[str, float] = {}

        # ROE: 0-30% -> 0-1
        roe = _as_float(data.get("roe"), 0.0)
        scores["roe"] = min(max(roe / 0.30, 0), 1)

        # P/E: 0-50 -> inverse (lower is better) -> 0-1.
        # Non-positive P/E (missing, zero, or negative earnings) scores 0.
        pe = _as_float(data.get("pe"), 50.0)
        if pe <= 0:
            pe = 50.0
        scores["pe"] = min(max(1 - pe / 50, 0), 1)

        # Dividend yield: 0-5% -> 0-1
        div = _as_float(data.get("dividend_yield"), 0.0)
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

        # Volume: latest volume vs SMA20 (1-3x -> 0-1).
        # Guard BOTH sides — a NaN Volume (suspended day) would poison the
        # ratio and rank ordering with NaN scores.
        if (
            not pd.isna(last.get("VOL_SMA_20"))
            and last["VOL_SMA_20"] > 0
            and not pd.isna(last.get("Volume"))
            and last["Volume"] > 0
        ):
            vol_ratio = last["Volume"] / last["VOL_SMA_20"]
            scores["volume"] = min(max((vol_ratio - 1) / 2, 0), 1)

        # RSI: 30-70 -> bell curve, peak at 50. Clamped — a glitchy RSI > 100
        # must not produce a negative score. Missing RSI stays penalized
        # (no key added, weight still counts in the denominator).
        rsi = last.get("RSI_14")
        if rsi is not None and not pd.isna(rsi):
            scores["rsi"] = min(max(1 - abs(float(rsi) - 50) / 50, 0), 1)

        return scores
