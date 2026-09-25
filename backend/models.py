"""Pydantic v2 request models used by the HTTP API.

The domain modules intentionally remain plain dataclasses/functions.  This
module is the boundary that validates untrusted HTTP input before it reaches
those modules.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from src.stock_screener.watchlist import DEFAULT_TICKERS, canonical_ticker

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,19}$")
_MAX_TICKERS = 100
_MAX_LOOKBACK_DAYS = 3650


def _finite_number(value: Any, field_name: str) -> float:
    """Convert an alias value to a finite number without leaking 500 errors."""

    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    return number


def _percentage_to_fraction(value: Any, field_name: str) -> float:
    return _finite_number(value, field_name) / 100.0


def _apply_percentage_alias(
    data: dict[str, Any], source: str, targets: tuple[str, ...]
) -> None:
    """Validate a percentage alias even when a canonical alias is present."""

    raw_value = data.pop(source)
    fraction = _percentage_to_fraction(raw_value, source)
    for target in targets:
        if target not in data:
            continue
        canonical = _finite_number(data[target], target)
        if not math.isclose(canonical, fraction, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError(f"{source} conflicts with {target}")
        return
    data[targets[0]] = fraction


def _reject_control_chars(value: str, field_name: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field_name} contains control characters")
    return value


def _normalise_ticker(value: Any) -> Any:
    """Canonicalise a ticker before Pydantic applies its string rules."""

    if not isinstance(value, str):
        return value
    ticker = canonical_ticker(value)
    if ticker and not _TICKER_RE.fullmatch(ticker):
        raise ValueError("ticker must contain only letters, numbers, '.', '_' or '-'")
    return ticker


Ticker = Annotated[
    str,
    BeforeValidator(_normalise_ticker),
    Field(min_length=1, max_length=20),
]


class APIModel(BaseModel):
    """Base model with strict, JSON-safe input behavior."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class LoginRequest(APIModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("username")
    @classmethod
    def strip_username(cls, value: str) -> str:
        return _reject_control_chars(value.strip(), "username")


class ChangePasswordRequest(APIModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class UserResponse(APIModel):
    id: int
    username: str
    can_send_alerts: bool = False


class LoginResponse(APIModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserResponse


class SetupStatusResponse(APIModel):
    has_users: bool
    needs_setup: bool


class TickerListRequest(APIModel):
    tickers: list[Ticker] = Field(min_length=1, max_length=_MAX_TICKERS)

    @field_validator("tickers")
    @classmethod
    def deduplicate(cls, value: list[str]) -> list[str]:
        # Preserve the caller's ordering while avoiding duplicate network
        # requests and duplicate portfolio/watchlist mutations.
        return list(dict.fromkeys(value))


class ConditionRequest(APIModel):
    metric: str = Field(
        min_length=1,
        max_length=40,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    operator: Literal[">", ">=", "<", "<=", "==", "!="]
    value: float

    @field_validator("metric")
    @classmethod
    def lower_metric(cls, value: str) -> str:
        return value.lower()


class FundamentalScreenRequest(TickerListRequest):
    conditions: list[ConditionRequest] | None = Field(default=None, max_length=30)
    min_roe: float | None = Field(
        default=None,
        ge=0,
        le=1,
        validation_alias=AliasChoices("min_roe", "minimum_roe"),
    )
    min_roe_percent: float | None = Field(default=None, ge=0, le=100, exclude=True)
    max_pe: float | None = Field(default=None, gt=0, le=1000)
    max_pb: float | None = Field(default=None, gt=0, le=1000)
    min_dividend_yield: float | None = Field(
        default=None,
        ge=0,
        le=1,
        validation_alias=AliasChoices("min_dividend_yield", "min_dividend"),
    )
    min_dividend_percent: float | None = Field(default=None, ge=0, le=100, exclude=True)
    watchlist_only: bool = Field(
        default=False,
        validation_alias=AliasChoices("watchlist_only", "only_watchlist"),
    )

    @model_validator(mode="before")
    @classmethod
    def accept_frontend_percent_fields(cls, value: Any) -> Any:
        """Accept both fractions (0.08) and UI percentages (8.0)."""

        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "min_roe_percent" in data:
            _apply_percentage_alias(data, "min_roe_percent", ("min_roe", "minimum_roe"))
        if "min_dividend_percent" in data:
            _apply_percentage_alias(
                data, "min_dividend_percent", ("min_dividend_yield", "min_dividend")
            )
        return data


class SmartScreenRequest(TickerListRequest):
    conditions: list[ConditionRequest] | None = Field(default=None, max_length=30)
    min_roe: float | None = Field(default=None, ge=0, le=1)
    min_roe_percent: float | None = Field(default=None, ge=0, le=100, exclude=True)
    max_pe: float | None = Field(default=None, gt=0, le=1000)
    max_pb: float | None = Field(default=None, gt=0, le=1000)
    min_dividend_yield: float | None = Field(
        default=None,
        ge=0,
        le=1,
        validation_alias=AliasChoices("min_dividend_yield", "min_dividend"),
    )
    min_dividend_percent: float | None = Field(default=None, ge=0, le=100, exclude=True)
    top_n: int = Field(default=20, ge=1, le=100)
    lookback_days: int = Field(default=365, ge=30, le=_MAX_LOOKBACK_DAYS)
    weights: dict[str, float] | None = Field(default=None, max_length=10)
    watchlist_only: bool = Field(
        default=False,
        validation_alias=AliasChoices("watchlist_only", "only_watchlist"),
    )

    @model_validator(mode="before")
    @classmethod
    def accept_frontend_percent_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "min_roe_percent" in data:
            _apply_percentage_alias(data, "min_roe_percent", ("min_roe",))
        if "min_dividend_percent" in data:
            _apply_percentage_alias(
                data, "min_dividend_percent", ("min_dividend_yield", "min_dividend")
            )
        return data

    @field_validator("weights")
    @classmethod
    def validate_weights(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is None:
            return value
        allowed = {"roe", "pe", "trend", "volume", "rsi", "dividend"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown weight(s): {', '.join(sorted(unknown))}")
        if any(weight < 0 for weight in value.values()):
            raise ValueError("weights must be non-negative")
        if not any(weight > 0 for weight in value.values()):
            raise ValueError("at least one weight must be greater than zero")
        return value


class SignalScanRequest(APIModel):
    tickers: list[Ticker] = Field(
        default_factory=lambda: list(DEFAULT_TICKERS),
        min_length=1,
        max_length=_MAX_TICKERS,
    )
    lookback_days: int = Field(default=365, ge=30, le=_MAX_LOOKBACK_DAYS)
    strategies: list[
        Literal[
            "VolumeBreakout",
            "PullbackMA",
            "TrendBreakdown",
            "OverboughtReversal",
        ]
    ] = Field(
        default_factory=lambda: [
            "VolumeBreakout",
            "PullbackMA",
            "TrendBreakdown",
            "OverboughtReversal",
        ],
        min_length=1,
        max_length=4,
    )
    include_sell_strategies: bool = True
    capital_jpy: float = Field(
        default=10_000_000,
        gt=0,
        le=1_000_000_000_000,
        validation_alias=AliasChoices("capital_jpy", "capital", "total_capital"),
    )
    risk_per_trade: float = Field(default=0.01, gt=0, lt=1)
    risk_percent: float | None = Field(default=None, gt=0, le=100, exclude=True)
    hard_stop_pct: float = Field(default=0.07, gt=0, lt=1)
    hard_stop_percent: float | None = Field(default=None, ge=0, le=100, exclude=True)
    volume_breakout_lookback: int = Field(default=20, ge=5, le=100)
    volume_multiplier: float = Field(default=1.2, ge=0.1, le=10)
    pullback_rsi_max: float = Field(default=60, ge=0, le=100)

    @model_validator(mode="before")
    @classmethod
    def accept_frontend_risk_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "risk_percent" in data:
            _apply_percentage_alias(data, "risk_percent", ("risk_per_trade",))
        if "hard_stop_percent" in data:
            _apply_percentage_alias(data, "hard_stop_percent", ("hard_stop_pct",))
        return data

    @field_validator("tickers")
    @classmethod
    def deduplicate(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("strategies")
    @classmethod
    def unique_strategies(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class MultiTimeframeRequest(APIModel):
    tickers: list[Ticker] = Field(
        default_factory=lambda: list(DEFAULT_TICKERS),
        min_length=1,
        max_length=_MAX_TICKERS,
    )
    lookback_days: int = Field(default=545, ge=200, le=_MAX_LOOKBACK_DAYS)
    min_confidence: float = Field(default=0.6, ge=0, le=1)

    @field_validator("tickers")
    @classmethod
    def deduplicate(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class BacktestRequest(APIModel):
    ticker: Ticker
    strategy: Literal["VolumeBreakout", "PullbackMA"] = "VolumeBreakout"
    lookback_days: int = Field(default=730, ge=30, le=_MAX_LOOKBACK_DAYS)
    initial_capital: float = Field(
        default=10_000_000,
        gt=0,
        le=1_000_000_000_000,
        validation_alias=AliasChoices("initial_capital", "capital_jpy", "capital"),
    )
    risk_per_trade: float = Field(default=0.01, gt=0, lt=1)
    risk_percent: float | None = Field(default=None, gt=0, le=100, exclude=True)
    hard_stop_pct: float = Field(default=0.07, gt=0, lt=1)
    hard_stop_percent: float | None = Field(default=None, ge=0, le=100, exclude=True)
    max_holding_days: int = Field(default=60, ge=0, le=3650)
    take_profit_pct: float = Field(default=0, ge=0, le=10)
    take_profit_percent: float | None = Field(default=None, ge=0, le=1000, exclude=True)
    commission_pct: float = Field(default=0.001, ge=0, lt=1)
    commission_percent: float | None = Field(default=None, ge=0, le=100, exclude=True)
    slippage_pct: float = Field(default=0.001, ge=0, lt=1)
    slippage_percent: float | None = Field(default=None, ge=0, le=100, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def accept_frontend_percent_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        percent_fields = {
            "risk_percent": ("risk_per_trade",),
            "hard_stop_percent": ("hard_stop_pct",),
            "take_profit_percent": ("take_profit_pct",),
            "commission_percent": ("commission_pct",),
            "slippage_percent": ("slippage_pct",),
        }
        for source, targets in percent_fields.items():
            if source in data:
                _apply_percentage_alias(data, source, targets)
        return data


class EarningsRequest(TickerListRequest):
    warning_days: int = Field(
        default=14,
        ge=1,
        le=90,
        validation_alias=AliasChoices("warning_days", "warn_days"),
    )


class PriceTargetRequest(APIModel):
    ticker: Ticker
    lookback_days: int = Field(default=365, ge=30, le=_MAX_LOOKBACK_DAYS)
    entry_price: float | None = Field(default=None, gt=0, le=1_000_000_000)
    stop_loss: float | None = Field(default=None, gt=0, le=1_000_000_000)
    swing_lookback: int = Field(default=20, ge=3, le=200)
    atr_period: int = Field(default=14, ge=2, le=100)
    atr_mult: float = Field(default=1.5, gt=0, le=20)
    hard_stop_pct: float = Field(default=0.07, gt=0, lt=1)
    hard_stop_percent: float | None = Field(default=None, ge=0, le=100, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def accept_frontend_stop_field(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "hard_stop_percent" in data:
            _apply_percentage_alias(data, "hard_stop_percent", ("hard_stop_pct",))
        return data

    @model_validator(mode="after")
    def validate_stop(self) -> PriceTargetRequest:
        if self.entry_price is not None and self.stop_loss is not None:
            if self.stop_loss >= self.entry_price:
                raise ValueError("stop_loss must be below entry_price")
        return self


class ProfitTargetRowRequest(APIModel):
    """A persisted UI row; zero prices/blank tickers are allowed as drafts."""

    ticker: str = Field(default="", max_length=20)
    entry_price: float = Field(default=0, ge=0, le=1_000_000_000)
    target_pct: float = Field(default=5, ge=-99.99, le=1000)
    shares: int = Field(default=0, ge=0, le=1_000_000_000)

    @field_validator("ticker")
    @classmethod
    def normalise_optional_ticker(cls, value: str) -> str:
        if not value:
            return ""
        # Keep the provider suffix in this draft response for compatibility;
        # persistence canonicalizes it before it is stored.
        _normalise_ticker(value)
        return value.strip().upper()


class TargetRowsRequest(APIModel):
    rows: list[ProfitTargetRowRequest] = Field(default_factory=list, max_length=100)


class ProfitTargetCalculateRequest(APIModel):
    entry_price: float = Field(gt=0, le=1_000_000_000)
    target_pct: float = Field(ge=-99.99, le=1000)


class ProfitTargetSummarizeRequest(APIModel):
    rows: list[ProfitTargetRowRequest] = Field(default_factory=list, max_length=100)


class WatchlistRequest(APIModel):
    tickers: list[Ticker] = Field(min_length=1, max_length=_MAX_TICKERS)

    @field_validator("tickers")
    @classmethod
    def deduplicate(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class SidebarSettingsRequest(APIModel):
    """Persisted dashboard settings, accepting legacy and API field names."""

    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        validate_assignment=False,
        allow_inf_nan=False,
    )

    capital: float = Field(
        default=10_000_000,
        gt=0,
        le=1_000_000_000_000,
        validation_alias=AliasChoices("capital", "total_capital", "sb_capital"),
    )
    risk_per_trade: float | None = Field(
        default=None,
        gt=0,
        lt=1,
        validation_alias=AliasChoices("risk_per_trade", "risk_fraction"),
    )
    risk_pct: float | None = Field(
        default=None,
        gt=0,
        le=100,
        validation_alias=AliasChoices("risk_pct", "sb_risk_pct"),
    )
    risk_percent: float | None = Field(default=None, gt=0, le=100, exclude=True)
    hard_stop_pct: float = Field(
        default=0.07,
        gt=0,
        lt=1,
        validation_alias=AliasChoices("hard_stop_pct", "hard_stop"),
    )
    hard_stop_percent: float | None = Field(default=None, ge=0, le=100, exclude=True)
    lookback_days: int = Field(
        default=365,
        ge=30,
        le=_MAX_LOOKBACK_DAYS,
        validation_alias=AliasChoices("lookback_days", "sb_lookback"),
    )
    language: Literal["EN", "VN"] = Field(
        default="EN",
        validation_alias=AliasChoices("language", "lang", "sb_lang"),
    )

    @model_validator(mode="before")
    @classmethod
    def accept_frontend_settings_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "risk_percent" in data:
            risk_percent = _finite_number(data.pop("risk_percent"), "risk_percent")
            if "risk_pct" in data:
                if not math.isclose(
                    _finite_number(data["risk_pct"], "risk_pct"),
                    risk_percent,
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                ):
                    raise ValueError("risk_percent conflicts with risk_pct")
            elif "risk_per_trade" in data:
                if not math.isclose(
                    _finite_number(data["risk_per_trade"], "risk_per_trade"),
                    risk_percent / 100.0,
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                ):
                    raise ValueError("risk_percent conflicts with risk_per_trade")
            else:
                data["risk_pct"] = risk_percent
        if "risk_pct" in data and "risk_per_trade" in data:
            if not math.isclose(
                _finite_number(data["risk_pct"], "risk_pct") / 100.0,
                _finite_number(data["risk_per_trade"], "risk_per_trade"),
                rel_tol=1e-9,
                abs_tol=1e-12,
            ):
                raise ValueError("risk_pct conflicts with risk_per_trade")
        if "hard_stop_percent" in data:
            _apply_percentage_alias(data, "hard_stop_percent", ("hard_stop_pct", "hard_stop"))
        # ``sb_hard_stop`` is a legacy percentage-point field, not a fraction.
        if "sb_hard_stop" in data:
            legacy_stop = _percentage_to_fraction(data.pop("sb_hard_stop"), "sb_hard_stop")
            if "hard_stop_pct" in data:
                if not math.isclose(
                    _finite_number(data["hard_stop_pct"], "hard_stop_pct"),
                    legacy_stop,
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                ):
                    raise ValueError("sb_hard_stop conflicts with hard_stop_pct")
            elif "hard_stop" in data:
                if not math.isclose(
                    _finite_number(data["hard_stop"], "hard_stop"),
                    legacy_stop,
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                ):
                    raise ValueError("sb_hard_stop conflicts with hard_stop")
            else:
                data["hard_stop_pct"] = legacy_stop
        return data

    @model_validator(mode="after")
    def fill_risk_values(self) -> SidebarSettingsRequest:
        if self.risk_per_trade is None and self.risk_pct is None:
            self.risk_per_trade = 0.01
            self.risk_pct = 1.0
        elif self.risk_per_trade is None:
            # ``risk_pct``/``sb_risk_pct`` are always percentage points.
            self.risk_per_trade = self.risk_pct / 100 if self.risk_pct is not None else 0.01
        elif self.risk_pct is None:
            self.risk_pct = self.risk_per_trade * 100
        return self

    def as_legacy_dict(self) -> dict[str, Any]:
        return {
            "sb_capital": self.capital,
            "sb_risk_pct": self.risk_pct,
            "sb_hard_stop": self.hard_stop_pct * 100,
            "sb_lookback": self.lookback_days,
            "sb_lang": self.language,
        }


class ChartSettingsRequest(APIModel):
    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        validate_assignment=False,
        allow_inf_nan=False,
    )

    ticker: Ticker = "7203"
    interval: Literal["1d", "1h", "1wk"] = "1d"
    sma_period: int = Field(default=20, ge=2, le=500)
    show_sma: bool = True
    show_volume: bool = True
    lookback_days: int = Field(default=365, ge=30, le=_MAX_LOOKBACK_DAYS)


class AutoScanSettingsRequest(APIModel):
    enabled: bool = False
    interval_minutes: int = Field(default=30, ge=1, le=1440)
    lookback_days: int = Field(default=365, ge=30, le=_MAX_LOOKBACK_DAYS)
    tickers: list[Ticker] = Field(
        default_factory=lambda: list(DEFAULT_TICKERS),
        min_length=1,
        max_length=_MAX_TICKERS,
    )

    @field_validator("tickers")
    @classmethod
    def deduplicate(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class PortfolioAddRequest(APIModel):
    ticker: Ticker
    shares: int = Field(gt=0, le=1_000_000_000)
    entry_price: float = Field(gt=0, le=1_000_000_000)
    stop_loss: float = Field(gt=0, le=1_000_000_000)
    strategy: str = Field(default="manual", min_length=1, max_length=80)
    sector: str = Field(default="", max_length=120)
    take_profit_levels: list[float] = Field(default_factory=list, max_length=10)
    trail_pct: float = Field(default=0, ge=0, lt=1)

    @field_validator("take_profit_levels")
    @classmethod
    def valid_targets(cls, value: list[float]) -> list[float]:
        if any(level <= 0 for level in value):
            raise ValueError("take-profit levels must be positive")
        return value

    @field_validator("strategy", "sector")
    @classmethod
    def reject_control_text(cls, value: str) -> str:
        return _reject_control_chars(value, "portfolio text")

    @model_validator(mode="after")
    def stop_below_entry(self) -> PortfolioAddRequest:
        if self.stop_loss >= self.entry_price:
            raise ValueError("stop_loss must be below entry_price")
        return self


class PortfolioCloseRequest(APIModel):
    exit_price: float = Field(gt=0, le=1_000_000_000)
    reason: str = Field(default="MANUAL", min_length=1, max_length=80)

    @field_validator("reason")
    @classmethod
    def reject_control_text(cls, value: str) -> str:
        return _reject_control_chars(value, "reason")


class TrailingStopRequest(APIModel):
    trail_pct: float = Field(gt=0, lt=1)


class PortfolioCheckRequest(APIModel):
    refresh_prices: bool = True
    send_alerts: bool = False


class AlertScanRequest(TickerListRequest):
    lookback_days: int = Field(default=365, ge=30, le=_MAX_LOOKBACK_DAYS)
    # Delivery is an explicit, opt-in side effect. The normal dashboard scan
    # is read-only; callers must consciously request a broadcast.
    send_alerts: bool = False
    deliver: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_frontend_delivery_field(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "deliver" in data:
            deliver = data.pop("deliver")
            if "send_alerts" not in data:
                data["send_alerts"] = deliver
        return data


class AlertChannelTestRequest(APIModel):
    message: str | None = Field(default=None, max_length=2000)


class TickerPathRequest(APIModel):
    ticker: Ticker


__all__ = [
    "AlertChannelTestRequest",
    "AlertScanRequest",
    "AutoScanSettingsRequest",
    "APIModel",
    "BacktestRequest",
    "ChangePasswordRequest",
    "ChartSettingsRequest",
    "ConditionRequest",
    "EarningsRequest",
    "FundamentalScreenRequest",
    "LoginRequest",
    "LoginResponse",
    "MultiTimeframeRequest",
    "PortfolioAddRequest",
    "PortfolioCheckRequest",
    "PortfolioCloseRequest",
    "PriceTargetRequest",
    "ProfitTargetCalculateRequest",
    "ProfitTargetRowRequest",
    "ProfitTargetSummarizeRequest",
    "SetupStatusResponse",
    "SidebarSettingsRequest",
    "SignalScanRequest",
    "SmartScreenRequest",
    "TargetRowsRequest",
    "Ticker",
    "TickerListRequest",
    "TrailingStopRequest",
    "UserResponse",
    "WatchlistRequest",
]
