"""
Portfolio Tracker — track open positions, P/L, sector exposure,
trailing stops, and take-profit alerts.

Maintains a local JSON portfolio file and provides real-time
unrealized P/L calculation and sector diversification checks.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import wraps
from pathlib import Path

try:  # POSIX advisory locks protect the JSON file across worker processes.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback uses the thread lock.
    fcntl = None  # type: ignore[assignment]

import pandas as pd

from . import db
from .data_loader import (
    ProviderBusyError,
    YFinanceDataLoader,
    jst_now,
    provider_io_slot,
)
from .risk_management import PositionPlan
from .watchlist import canonical_ticker

logger = logging.getLogger(__name__)

# Persist next to the SQLite DB — survives redeploys on Render paid tier
# (TSE_DATA_DIR) and keeps state colocated instead of polluting repo root.
PORTFOLIO_FILE = db.DATA_DIR / "portfolio.json"
_PORTFOLIO_PATH_LOCKS: dict[str, threading.RLock] = {}
_PORTFOLIO_PATH_LOCKS_GUARD = threading.Lock()


class PortfolioStorageError(RuntimeError):
    """Raised when persisted portfolio state cannot be safely read or moved."""


def _path_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _PORTFOLIO_PATH_LOCKS_GUARD:
        return _PORTFOLIO_PATH_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _portfolio_file_lock(path: Path) -> Iterator[None]:
    """Lock one portfolio file for a complete read/modify/write operation."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PortfolioStorageError("portfolio storage directory is unavailable") from exc
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    lock_path = path.with_name(f".{path.name}.lock")
    with _path_lock(path):
        try:
            handle = lock_path.open("a+")
        except OSError as exc:
            raise PortfolioStorageError("portfolio storage lock is unavailable") from exc
        try:
            try:
                lock_path.chmod(0o600)
            except OSError:
                pass
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise PortfolioStorageError("portfolio storage lock is unavailable") from exc
            yield
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


def migrate_legacy_portfolio_to_user(user_id: int) -> bool:
    """Copy the legacy global portfolio to one explicitly chosen user.

    The copy is intentionally opt-in through ``TSE_LEGACY_PORTFOLIO_USER_ID``.
    A global file has no reliable owner in a multi-user deployment, so silently
    assigning it to whichever account logs in first would be unsafe.  The
    source remains untouched as a rollback copy.
    """

    configured = os.getenv("TSE_LEGACY_PORTFOLIO_USER_ID", "").strip()
    if not configured:
        return False
    try:
        configured_id = int(configured)
    except ValueError:
        logger.warning("TSE_LEGACY_PORTFOLIO_USER_ID must be an integer")
        return False
    if configured_id != int(user_id):
        return False

    source = Path(PORTFOLIO_FILE)
    destination = _portfolio_path(int(user_id))
    migration_marker = destination.with_name(f".{destination.name}.legacy-migrated")
    # Source and destination are distinct for authenticated users. Keep the
    # source intact and recheck destination existence while holding its lock.
    with _portfolio_file_lock(source):
        with _portfolio_file_lock(destination):
            if migration_marker.exists() or not source.exists() or destination.exists():
                return False
            try:
                if source.stat().st_size > MAX_PORTFOLIO_BYTES:
                    logger.warning("Legacy portfolio migration skipped: file exceeds size limit")
                    return False
                raw = source.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("legacy portfolio root must be an object")
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Legacy portfolio migration skipped: %s", type(exc).__name__)
                return False
            _atomic_json_write(destination, raw)
            _atomic_json_write(
                migration_marker,
                json.dumps(
                    {
                        "source": source.name,
                        "user_id": int(user_id),
                        "migrated_at": datetime.now().isoformat(),
                    },
                    indent=2,
                ),
            )
    logger.info("Migrated legacy portfolio to user %d", int(user_id))
    return True


def _fsync_directory(path: Path) -> None:
    """Best-effort durability barrier for atomic file replacement."""

    if fcntl is None:
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_json_write(path: Path, payload: str) -> None:
    """Write JSON through a unique same-directory temporary file."""

    temporary_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
        temporary_name = None
    except OSError as exc:
        raise PortfolioStorageError("portfolio state could not be persisted") from exc
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _portfolio_path(user_id: int | None) -> Path:
    """Return the legacy global path or an isolated per-user path.

    ``user_id=None`` deliberately retains the historical ``portfolio.json``
    behavior so existing callers/tests remain compatible.  Authenticated API
    callers always provide an id and therefore cannot read or overwrite one
    another's positions.
    """

    base = Path(PORTFOLIO_FILE)
    if user_id is None:
        return base
    if int(user_id) < 0:
        raise ValueError("user_id must be non-negative")
    return base.with_name(f"{base.stem}_user_{int(user_id)}{base.suffix}")


# ---------------------------------------------------------------------------
# Position model
# ---------------------------------------------------------------------------


@dataclass
class PortfolioPosition:
    """An active position in the portfolio."""

    ticker: str
    shares: int
    entry_price: float
    entry_date: str
    stop_loss: float
    strategy: str
    position_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    sector: str = ""
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    peak_price: float = 0.0            # highest price seen since entry
    trail_pct: float = 0.0             # 0 = trailing disabled
    trailing_stop: float = 0.0         # current trailing stop price
    take_profit_levels: list[float] = field(default_factory=list)
    tp_hit: list[bool] = field(default_factory=list)  # which TPs already triggered

    def __post_init__(self) -> None:
        if not self.position_id:
            self.position_id = uuid.uuid4().hex
        if self.peak_price == 0:
            self.peak_price = self.entry_price
        if self.trailing_stop == 0:
            self.trailing_stop = self.stop_loss
        if not self.tp_hit:
            self.tp_hit = [False] * len(self.take_profit_levels)

    def update_price(self, price: float) -> None:
        """Update current price and recalculate P/L.

        Raises:
            ValueError: If price is negative or NaN.
        """
        if price < 0 or pd.isna(price):
            raise ValueError(f"Price must be a non-negative number, got {price}")
        self.current_price = price
        self.unrealized_pnl = (price - self.entry_price) * self.shares
        self.unrealized_pnl_pct = (price - self.entry_price) / self.entry_price
        if price > self.peak_price:
            self.peak_price = price

    @property
    def market_value(self) -> float:
        return self.current_price * self.shares

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.shares

    @property
    def pnl_since_peak(self) -> float:
        """Drawdown % from peak price."""
        if self.peak_price <= 0:
            return 0.0
        return (self.current_price - self.peak_price) / self.peak_price

    def enable_trailing_stop(self, trail_pct: float = 0.05) -> None:
        """Activate trailing stop at given % below highest price."""
        self.trail_pct = trail_pct
        self.trailing_stop = round(self.peak_price * (1 - trail_pct), 2)
        if self.trailing_stop < self.stop_loss:
            self.trailing_stop = self.stop_loss

    def update_trailing_stop(self) -> float | None:
        """Move trailing stop up if price rose. Returns new stop or None."""
        if self.trail_pct <= 0:
            return None
        candidate = round(self.peak_price * (1 - self.trail_pct), 2)
        if candidate > self.trailing_stop:
            self.trailing_stop = candidate
            logger.info(
                "%s: trailing stop raised to ¥%.2f (peak=¥%.2f)",
                self.ticker, self.trailing_stop, self.peak_price,
            )
        return self.trailing_stop

    def check_trailing_stop(self) -> bool:
        """Check if price hit trailing stop. True = should exit."""
        if self.trail_pct <= 0 or self.current_price <= 0:
            return False
        return self.current_price <= self.trailing_stop

    def check_take_profits(self) -> list[int]:
        """Return indices of newly hit take-profit levels."""
        hit = []
        for i, tp in enumerate(self.take_profit_levels):
            if not self.tp_hit[i] and self.current_price >= tp:
                self.tp_hit[i] = True
                hit.append(i)
        return hit

    @property
    def highest_tp_hit(self) -> int:
        """Highest TP index that has been hit (-1 = none)."""
        for i in range(len(self.tp_hit) - 1, -1, -1):
            if self.tp_hit[i]:
                return i
        return -1


def _finite_json_number(value: object, field_name: str, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{field_name} must be finite")


def _validate_position_data(position_data: dict[str, object], ticker: str) -> None:
    for field_name in (
        "shares",
        "entry_price",
        "entry_date",
        "stop_loss",
        "strategy",
    ):
        if field_name not in position_data:
            raise ValueError(f"missing position field: {field_name}")
    if isinstance(position_data["shares"], bool) or not isinstance(position_data["shares"], int):
        raise ValueError("shares must be an integer")
    if position_data["shares"] <= 0:
        raise ValueError("shares must be positive")
    for field_name in (
        "entry_price",
        "stop_loss",
        "current_price",
        "unrealized_pnl",
        "unrealized_pnl_pct",
        "peak_price",
        "trail_pct",
        "trailing_stop",
    ):
        if field_name in position_data:
            _finite_json_number(position_data[field_name], field_name)
    _finite_json_number(position_data["entry_price"], "entry_price", positive=True)
    _finite_json_number(position_data["stop_loss"], "stop_loss", positive=True)
    if not isinstance(position_data["entry_date"], str) or not isinstance(
        position_data["strategy"], str
    ):
        raise ValueError("position text fields must be strings")
    if "sector" in position_data and not isinstance(position_data["sector"], str):
        raise ValueError("sector must be a string")
    if any(
        ord(char) < 32 or ord(char) == 127
        for char in f"{position_data['strategy']}{position_data['entry_date']}{position_data.get('sector', '')}"
    ):
        raise ValueError("position text fields contain control characters")
    levels = position_data.get("take_profit_levels", [])
    hits = position_data.get("tp_hit", [])
    if not isinstance(levels, list) or not isinstance(hits, list) or len(levels) != len(hits):
        raise ValueError("take-profit state is inconsistent")
    for level in levels:
        _finite_json_number(level, "take_profit_level", positive=True)
    if any(not isinstance(hit, bool) for hit in hits):
        raise ValueError("take-profit hits must be booleans")
    stored_ticker = position_data.get("ticker", ticker)
    if not isinstance(stored_ticker, str) or canonical_ticker(stored_ticker) != ticker:
        raise ValueError("position ticker does not match its key")
    position_id = position_data.get("position_id")
    if position_id is not None and (
        not isinstance(position_id, str)
        or not position_id
        or len(position_id) > 64
        or any(ord(char) < 32 or ord(char) == 127 for char in position_id)
    ):
        raise ValueError("position_id is invalid")


_ALERT_CHANNELS = ("telegram", "slack")
_ALERT_CLAIM_TTL_SECONDS = 15 * 60


def _alert_delivery_state_from_data(
    raw: object,
    legacy_signature: object,
) -> dict[str, object]:
    """Validate persisted per-channel delivery state with legacy fallback."""

    if raw is None:
        if legacy_signature in (None, ""):
            return {}
        if not isinstance(legacy_signature, str) or len(legacy_signature) > 131_072:
            raise ValueError("legacy alert signature is invalid")
        return {
            "signature": legacy_signature,
            "channels": {
                channel: {"status": "delivered"} for channel in _ALERT_CHANNELS
            },
        }
    if not isinstance(raw, dict):
        raise ValueError("alert delivery state must be an object")
    signature = raw.get("signature", "")
    if not isinstance(signature, str) or len(signature) > 131_072:
        raise ValueError("alert delivery signature is invalid")
    raw_channels = raw.get("channels", {})
    if not isinstance(raw_channels, dict):
        raise ValueError("alert delivery channels must be an object")
    channels: dict[str, dict[str, object]] = {}
    for channel in _ALERT_CHANNELS:
        entry = raw_channels.get(channel)
        if entry is None:
            continue
        if not isinstance(entry, dict) or entry.get("status") not in {
            "pending",
            "delivered",
        }:
            raise ValueError("alert delivery channel state is invalid")
        normalized: dict[str, object] = {"status": entry["status"]}
        if "claimed_at" in entry:
            claimed_at = entry["claimed_at"]
            if isinstance(claimed_at, bool) or not isinstance(claimed_at, (int, float)):
                raise ValueError("alert delivery claim timestamp is invalid")
            if not math.isfinite(float(claimed_at)):
                raise ValueError("alert delivery claim timestamp is invalid")
            normalized["claimed_at"] = float(claimed_at)
        channels[channel] = normalized
    if not signature:
        if channels:
            raise ValueError("alert delivery channels require a signature")
        return {}
    return {"signature": signature, "channels": channels}


def _atomic_mutation(method):
    """Make direct domain mutators use the same lock/reload/save contract."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if self._transaction_active:
            return method(self, *args, **kwargs)
        with self.transaction():
            return method(self, *args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# Portfolio manager
# ---------------------------------------------------------------------------


MAX_POSITIONS = 200
MAX_CLOSED_TRADES = 5_000
MAX_PORTFOLIO_BYTES = 10 * 1024 * 1024


class PortfolioTracker:
    """Manage portfolio positions, P/L tracking, and sector exposure.

    Persists to portfolio.json for cross-session continuity.
    """

    def __init__(
        self,
        total_capital: float = 10_000_000,
        max_sector_pct: float = 0.30,
        user_id: int | None = None,
    ) -> None:
        """Initialize tracker.

        Args:
            total_capital: Total portfolio capital for allocation checks.
            max_sector_pct: Max allowed exposure per sector (default 30%).
            user_id: Optional authenticated owner.  When supplied, state is
                persisted to a separate user-scoped JSON file; ``None``
                preserves the legacy global file behavior.
        """
        self.total_capital = total_capital
        self.max_sector_pct = max_sector_pct
        self.user_id = user_id
        self.portfolio_file = _portfolio_path(user_id)
        self.loader = YFinanceDataLoader()
        self.positions: dict[str, PortfolioPosition] = {}
        self.closed_trades: list[dict] = []
        self.recovered_corrupt_file: str | None = None
        self.storage_error: str | None = None
        self.last_alert_signature = ""
        self.alert_delivery_state: dict[str, object] = {}
        self._transaction_active = False
        self._load()

    # --- Persistence ---

    def _unavailable_marker(self) -> Path:
        return self.portfolio_file.with_name(f".{self.portfolio_file.name}.unavailable")

    def _quarantine(self, path: Path) -> None:
        """Move unreadable state aside without ever creating a fail-open gap.

        The recovery marker is committed before the active file is moved.  If
        the process stops between those operations, the marker still blocks new
        writes; if marker creation fails, the original file remains in place.
        """

        quarantine = path.with_name(
            f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        )
        marker = self._unavailable_marker()
        try:
            _atomic_json_write(
                marker,
                json.dumps(
                    {
                        "status": "unavailable",
                        "quarantined_file": quarantine.name,
                        "created_at": datetime.now().isoformat(),
                    },
                    indent=2,
                ),
            )
            os.replace(path, quarantine)
            _fsync_directory(path.parent)
        except (OSError, PortfolioStorageError) as exc:
            logger.exception("Could not quarantine unreadable portfolio file %s", path.name)
            raise PortfolioStorageError("portfolio state could not be quarantined") from exc
        self.recovered_corrupt_file = quarantine.name
        self.storage_error = "portfolio state was quarantined; operator recovery required"
        logger.warning("Quarantined unreadable portfolio file as %s", quarantine.name)

    def _load_unlocked(self) -> None:
        self.positions = {}
        self.closed_trades = []
        self.recovered_corrupt_file = None
        self.storage_error = None
        self.last_alert_signature = ""
        self.alert_delivery_state = {}
        path = self.portfolio_file
        marker = self._unavailable_marker()
        if marker.exists():
            self.storage_error = "portfolio state is unavailable; operator recovery required"
            raise PortfolioStorageError(self.storage_error)
        if not path.exists():
            orphan_quarantine = next(
                path.parent.glob(f"{path.name}.corrupt-*"),
                None,
            )
            if orphan_quarantine is not None and orphan_quarantine.is_file():
                self.storage_error = "portfolio state is unavailable; operator recovery required"
                raise PortfolioStorageError(self.storage_error)
            return
        try:
            if path.stat().st_size > MAX_PORTFOLIO_BYTES:
                raise ValueError("portfolio file exceeds size limit")
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.exception("Could not read portfolio file %s", path.name)
            raise PortfolioStorageError("portfolio state is unavailable") from exc
        except ValueError as exc:
            self._quarantine(path)
            raise PortfolioStorageError(
                "portfolio state was quarantined; operator recovery required"
            ) from exc
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("portfolio root must be an object")
            positions = data.get("positions", {})
            if not isinstance(positions, dict):
                raise ValueError("positions must be an object")
            if len(positions) > MAX_POSITIONS:
                raise ValueError("too many portfolio positions")
            loaded: dict[str, PortfolioPosition] = {}
            for ticker, position_data in positions.items():
                if not isinstance(ticker, str) or not isinstance(position_data, dict):
                    raise ValueError("invalid position entry")
                canonical = canonical_ticker(ticker)
                if not canonical or canonical in loaded:
                    raise ValueError("duplicate or invalid position ticker")
                _validate_position_data(position_data, canonical)
                position = PortfolioPosition(**position_data)
                position.ticker = canonical
                loaded[canonical] = position
            closed_trades = data.get("closed_trades", [])
            if not isinstance(closed_trades, list) or len(closed_trades) > MAX_CLOSED_TRADES or any(
                not isinstance(trade, dict) for trade in closed_trades
            ):
                raise ValueError("closed_trades must be a bounded list of objects")
            normalized_trades = []
            for trade in closed_trades:
                normalized = dict(trade)
                if not isinstance(normalized.get("ticker"), str):
                    raise ValueError("closed trade ticker is required")
                normalized["ticker"] = canonical_ticker(normalized["ticker"])
                if not normalized["ticker"]:
                    raise ValueError("closed trade ticker is invalid")
                _finite_json_number(normalized.get("pnl"), "closed trade pnl")
                if "pnl_pct" in normalized:
                    _finite_json_number(normalized["pnl_pct"], "closed trade pnl_pct")
                for field_name in ("reason", "strategy"):
                    if field_name in normalized:
                        if not isinstance(normalized[field_name], str):
                            raise ValueError(f"closed trade {field_name} must be text")
                        if any(ord(char) < 32 or ord(char) == 127 for char in normalized[field_name]):
                            raise ValueError(f"closed trade {field_name} contains control characters")
                normalized_trades.append(normalized)
            self.positions = loaded
            self.closed_trades = normalized_trades
            self.alert_delivery_state = _alert_delivery_state_from_data(
                data.get("alert_delivery"),
                data.get("last_alert_signature", ""),
            )
            self.last_alert_signature = str(
                self.alert_delivery_state.get("signature", "")
            )
        except (TypeError, ValueError, OverflowError, KeyError, json.JSONDecodeError) as exc:
            logger.warning("Portfolio file %s is invalid: %s", path.name, type(exc).__name__)
            self._quarantine(path)
            raise PortfolioStorageError(
                "portfolio state was quarantined; operator recovery required"
            ) from exc
        try:
            path.chmod(0o600)
        except OSError:
            pass
        logger.info("Loaded portfolio %s: %d positions", path.name, len(self.positions))

    def _load(self) -> None:
        with _portfolio_file_lock(self.portfolio_file):
            self._load_unlocked()

    def _save_unlocked(self) -> None:
        data = {
            "positions": {t: asdict(p) for t, p in self.positions.items()},
            "closed_trades": self.closed_trades,
            "last_alert_signature": self.last_alert_signature,
            "alert_delivery": self.alert_delivery_state,
            "updated_at": datetime.now().isoformat(),
        }
        _atomic_json_write(
            self.portfolio_file,
            json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False),
        )

    def _save(self) -> None:
        if self._transaction_active:
            return
        with _portfolio_file_lock(self.portfolio_file):
            self._save_unlocked()

    @contextmanager
    def transaction(self) -> Iterator[PortfolioTracker]:
        """Run a read/modify/write cycle under a process/file lock.

        The tracker is reloaded after acquiring the lock so a second request
        cannot overwrite a position added after its initial read.  A successful
        context saves once at the end; failures leave the on-disk state intact.
        """

        with _portfolio_file_lock(self.portfolio_file):
            self._load_unlocked()
            self._transaction_active = True
            try:
                yield self
            except Exception:
                raise
            else:
                self._save_unlocked()
            finally:
                self._transaction_active = False

    # --- Position management ---

    def add_position(
        self,
        plan: PositionPlan,
        sector: str = "",
        take_profit_levels: list[float] | None = None,
        trail_pct: float = 0.0,
    ) -> None:
        """Add a new position from a RiskManager plan.

        Args:
            plan: PositionPlan from risk_management module.
            sector: Sector classification string.
            take_profit_levels: Optional list of TP prices.
            trail_pct: Trailing stop % (0 = disabled).
        """
        if not self._transaction_active:
            if not sector:
                try:
                    fundies = self.loader.fetch_fundamentals(canonical_ticker(plan.ticker))
                    sector = fundies.get("sector") or ""
                except Exception:
                    logger.debug("Sector auto-fetch failed for %s", plan.ticker)
                if not sector:
                    sector = "Unknown"
            with self.transaction():
                return self.add_position(
                    plan,
                    sector=sector,
                    take_profit_levels=take_profit_levels,
                    trail_pct=trail_pct,
                )

        ticker = canonical_ticker(plan.ticker)
        if ticker in self.positions:
            logger.warning("Position for %s already exists — skipping", ticker)
            return
        if len(self.positions) >= MAX_POSITIONS:
            raise ValueError(f"portfolio cannot exceed {MAX_POSITIONS} open positions")

        # A 0-share plan (RiskManager's "no capital left" fallback) would
        # create a phantom position that skews sector exposure and stats.
        if plan.shares <= 0:
            logger.warning("Skipping %s: 0-share plan", ticker)
            return

        if not sector:
            sector = "Unknown"

        tps = take_profit_levels or []
        pos = PortfolioPosition(
            ticker=ticker,
            shares=plan.shares,
            entry_price=plan.entry_price,
            entry_date=jst_now().strftime("%Y-%m-%d"),
            stop_loss=plan.stop_loss,
            strategy=plan.strategy,
            sector=sector or "Unknown",
            peak_price=plan.entry_price,
            trailing_stop=plan.stop_loss,
            trail_pct=trail_pct,
            take_profit_levels=tps,
            tp_hit=[False] * len(tps),
        )
        self.positions[ticker] = pos
        self._save()
        logger.info("Added position: %s", pos)

    @_atomic_mutation
    def close_position(self, ticker: str, exit_price: float, reason: str = "") -> None:
        """Close a position and record the trade.

        Args:
            ticker: Ticker to close.
            exit_price: Exit price.
            reason: Reason for closing (STOP_LOSS, TRAILING_STOP, TAKE_PROFIT, MANUAL).
        """
        ticker = canonical_ticker(ticker)
        if ticker not in self.positions:
            logger.warning("No position for %s", ticker)
            return

        # Validate BEFORE mutating — popping first then raising on a bad
        # exit_price would lose the position in memory with nothing saved.
        if exit_price <= 0 or pd.isna(exit_price):
            raise ValueError(f"exit_price must be a positive number, got {exit_price}")
        if len(self.closed_trades) >= MAX_CLOSED_TRADES:
            # Keep the history bounded without preventing a user from closing
            # an open position. The oldest realized trade is archived out.
            del self.closed_trades[0]

        pos = self.positions.pop(ticker)
        pos.update_price(exit_price)
        pnl = pos.unrealized_pnl
        pnl_pct = pos.unrealized_pnl_pct

        self.closed_trades.append({
            "ticker": ticker,
            "shares": pos.shares,
            "entry_price": pos.entry_price,
            "entry_date": pos.entry_date,
            "exit_price": exit_price,
            "exit_date": jst_now().strftime("%Y-%m-%d"),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "strategy": pos.strategy,
            "reason": reason,
            "peak_price": pos.peak_price,
        })
        self._save()
        logger.info("Closed %s: P/L=¥%.0f (%.2f%%) [%s]", ticker, pnl, pnl_pct * 100, reason)

    def recalc_targets(self, ticker: str) -> list[float]:
        """Recalculate targets outside the write lock, then commit atomically."""

        ticker = canonical_ticker(ticker)
        if self._transaction_active:
            return self._recalc_targets_locked(ticker)
        with self.transaction() as snapshot:
            position = snapshot.positions.get(ticker)
            if position is None:
                return []
            entry_price = position.entry_price
            stop_loss = position.stop_loss
        try:
            tps = self._calculate_targets(ticker, entry_price, stop_loss)
        except ProviderBusyError:
            raise
        except Exception:
            logger.exception("recalc_targets failed for %s", ticker)
            return []
        with self.transaction() as locked:
            position = locked.positions.get(ticker)
            if position is None:
                return []
            position.take_profit_levels = tps
            position.tp_hit = [False] * len(tps)
        return tps

    def _calculate_targets(self, ticker: str, entry_price: float, stop_loss: float) -> list[float]:
        from datetime import timedelta

        from .price_target import PriceTargetEngine
        from .technical_engine import TechnicalEngine

        end = jst_now().strftime("%Y-%m-%d")
        start = (jst_now() - timedelta(days=365)).strftime("%Y-%m-%d")
        df = self.loader.fetch_ohlcv(ticker, start, end)
        df = TechnicalEngine().enrich(df)
        targets = PriceTargetEngine().compute_all(
            df, ticker, entry_price=entry_price, stop_loss=stop_loss
        )
        return targets.take_profits[:3] if targets.take_profits else []

    def _recalc_targets_locked(self, ticker: str) -> list[float]:
        position = self.positions.get(ticker)
        if position is None:
            return []
        try:
            tps = self._calculate_targets(ticker, position.entry_price, position.stop_loss)
            position.take_profit_levels = tps
            position.tp_hit = [False] * len(tps)
            return tps
        except ProviderBusyError:
            raise
        except Exception:
            logger.exception("recalc_targets failed for %s", ticker)
            return []

    @_atomic_mutation
    def enable_trailing(self, ticker: str, trail_pct: float = 0.05) -> bool:
        """Enable trailing stop for a position."""
        ticker = canonical_ticker(ticker)
        if ticker not in self.positions:
            return False
        self.positions[ticker].enable_trailing_stop(trail_pct)
        self._save()
        return True

    # --- Monitoring ---

    def _alert_signature(self, events: dict[str, object]) -> str:
        """Include position lifecycle IDs so a reopened position alerts anew."""

        lifecycle = {
            ticker: position.position_id
            for ticker, position in sorted(self.positions.items())
        }
        return json.dumps(
            {"events": events, "positions": lifecycle},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _claim_alert_events_unlocked(
        self,
        events: dict[str, object],
        channels: tuple[str, ...],
    ) -> dict[str, str]:
        if not events or not any(bool(value) for value in events.values()):
            return {}
        selected = tuple(dict.fromkeys(channels))
        if not selected or any(channel not in _ALERT_CHANNELS for channel in selected):
            return {}
        signature = self._alert_signature(events)
        if self.alert_delivery_state.get("signature") != signature:
            self.alert_delivery_state = {"signature": signature, "channels": {}}
            self.last_alert_signature = signature
        channel_state = self.alert_delivery_state.setdefault("channels", {})
        now = time.time()
        claims: dict[str, str] = {}
        for channel in selected:
            entry = channel_state.get(channel)
            if isinstance(entry, dict) and entry.get("status") == "delivered":
                continue
            if isinstance(entry, dict) and entry.get("status") == "pending":
                claimed_at = entry.get("claimed_at")
                if (
                    isinstance(claimed_at, (int, float))
                    and not isinstance(claimed_at, bool)
                    and now - float(claimed_at) < _ALERT_CLAIM_TTL_SECONDS
                ):
                    continue
            channel_state[channel] = {"status": "pending", "claimed_at": now}
            claims[channel] = signature
        if claims:
            self._save()
        return claims

    def claim_alert_events(
        self,
        events: dict[str, object],
        channels: tuple[str, ...] = _ALERT_CHANNELS,
    ) -> dict[str, str]:
        """Atomically claim delivery for each configured channel."""

        if self._transaction_active:
            return self._claim_alert_events_unlocked(events, channels)
        with self.transaction() as locked:
            return locked._claim_alert_events_unlocked(events, channels)

    def _record_alert_delivery_unlocked(
        self,
        signature: str,
        channel: str,
        delivered: bool,
    ) -> bool:
        if channel not in _ALERT_CHANNELS:
            return False
        if self.alert_delivery_state.get("signature") != signature:
            return False
        channel_state = self.alert_delivery_state.get("channels")
        if not isinstance(channel_state, dict) or channel not in channel_state:
            return False
        if delivered:
            channel_state[channel] = {"status": "delivered"}
        else:
            channel_state.pop(channel, None)
        self._save()
        return True

    def record_alert_delivery(
        self,
        signature: str,
        channel: str,
        *,
        delivered: bool,
    ) -> bool:
        """Record a channel result, allowing failed channels to retry."""

        if self._transaction_active:
            return self._record_alert_delivery_unlocked(signature, channel, delivered)
        with self.transaction() as locked:
            return locked._record_alert_delivery_unlocked(signature, channel, delivered)

    def new_alert_events(
        self, events: dict[str, object], *, mark: bool = True
    ) -> dict[str, object]:
        """Backward-compatible all-channel event deduplication helper."""

        signature = self._alert_signature(events)
        if signature == self.last_alert_signature:
            return {}
        if mark:
            self.alert_delivery_state = {
                "signature": signature,
                "channels": {
                    channel: {"status": "delivered"} for channel in _ALERT_CHANNELS
                },
            }
            self.last_alert_signature = signature
            self._save()
        return events

    def update_prices(self) -> None:
        """Refresh prices without holding the portfolio write lock on I/O."""

        if self._transaction_active:
            self._update_prices_locked()
            return

        with self.transaction() as snapshot:
            tickers = list(snapshot.positions)
        if not tickers:
            return

        from concurrent.futures import ThreadPoolExecutor

        import yfinance as yf

        def fetch_one(ticker: str) -> tuple[str, float | None]:
            try:
                normalized = self.loader.normalize_ticker(ticker)
                return ticker, self._fetch_latest_price(yf.Ticker(normalized))
            except ProviderBusyError:
                raise
            except Exception:
                logger.exception("Failed to fetch price for %s", ticker)
                return ticker, None

        updates: dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=min(len(tickers), 8)) as pool:
            futures = [pool.submit(fetch_one, ticker) for ticker in tickers]
            for future in futures:
                ticker, price = future.result()
                if price is not None and price > 0:
                    updates[ticker] = float(price)
                else:
                    logger.warning("%s: no price available — keeping last known", ticker)

        # Reacquire and reload so a close/add that happened during provider I/O
        # is preserved; only apply prices for positions that still exist.
        with self.transaction() as locked:
            for ticker, price in updates.items():
                position = locked.positions.get(ticker)
                if position is not None:
                    position.update_price(price)

    def _update_prices_locked(self) -> None:
        """Compatibility path for callers already inside a transaction."""

        items = list(self.positions.items())
        if not items:
            return
        from concurrent.futures import ThreadPoolExecutor

        import yfinance as yf

        def update_one(ticker: str, position) -> None:
            try:
                normalized = self.loader.normalize_ticker(ticker)
                price = self._fetch_latest_price(yf.Ticker(normalized))
                if price is not None and price > 0:
                    position.update_price(float(price))
            except ProviderBusyError:
                raise
            except Exception:
                logger.exception("Failed to update price for %s", ticker)

        with ThreadPoolExecutor(max_workers=min(len(items), 8)) as pool:
            futures = [pool.submit(update_one, ticker, position) for ticker, position in items]
            for future in futures:
                future.result()

    @staticmethod
    def _fetch_latest_price(t) -> float | None:
        """Try multiple price sources under the shared provider limit."""
        with provider_io_slot():
            return PortfolioTracker._fetch_latest_price_unlocked(t)

    @staticmethod
    def _fetch_latest_price_unlocked(t) -> float | None:
        """Try multiple price sources. Returns None if all fail."""
        try:
            fi = t.fast_info
            if hasattr(fi, "get"):
                price = fi.get("lastPrice") or fi.get("previousClose")
            else:
                price = getattr(fi, "last_price", None) or getattr(fi, "previous_close", None)
            if price:
                return float(price)
        except Exception:
            pass

        try:
            info = t.info
            if info:
                for key in ("currentPrice", "regularMarketPrice", "previousClose"):
                    val = info.get(key)
                    if val:
                        return float(val)
        except Exception:
            pass

        return None

    @_atomic_mutation
    def update_trailing_stops(self) -> list[tuple[str, float]]:
        """Move trailing stops up for all positions with trail enabled.

        Returns:
            List of (ticker, new_stop) for stops that moved.
        """
        moved = []
        for ticker, pos in self.positions.items():
            if pos.trail_pct > 0:
                old = pos.trailing_stop
                new = pos.update_trailing_stop()
                if new is not None and new != old:
                    moved.append((ticker, new))
        if moved:
            self._save()
        return moved

    def check_stop_losses(self) -> list[str]:
        """Check all positions against their stop-loss levels.

        Returns:
            List of tickers that hit stop-loss.
        """
        triggered: list[str] = []
        for ticker, pos in self.positions.items():
            if pos.current_price <= 0:
                logger.debug("%s: no current price — skipping SL check", ticker)
                continue
            if pos.current_price <= pos.stop_loss:
                triggered.append(ticker)
                logger.warning("STOP LOSS: %s @ %.2f (SL=%.2f)", ticker, pos.current_price, pos.stop_loss)
        return triggered

    def check_trailing_stops(self) -> list[tuple[str, float]]:
        """Check trailing stops for all positions.

        Returns:
            List of (ticker, current_stop) for triggered stops.
        """
        triggered = []
        for ticker, pos in self.positions.items():
            if pos.check_trailing_stop():
                triggered.append((ticker, pos.trailing_stop))
                logger.warning(
                    "TRAILING STOP: %s @ %.2f (stop=%.2f, peak=%.2f)",
                    ticker, pos.current_price, pos.trailing_stop, pos.peak_price,
                )
        return triggered

    @_atomic_mutation
    def check_take_profits(self) -> dict[str, list[int]]:
        """Check take-profit levels for all positions.

        Returns:
            Dict mapping ticker -> list of newly hit TP indices.
        """
        hit: dict[str, list[int]] = {}
        for ticker, pos in self.positions.items():
            if pos.current_price <= 0:
                continue
            new = pos.check_take_profits()
            if new:
                hit[ticker] = new
                for i in new:
                    logger.info(
                        "TAKE PROFIT %d: %s @ ¥%.2f (TP=¥%.2f)",
                        i + 1, ticker, pos.current_price, pos.take_profit_levels[i],
                    )
        if hit:
            self._save()
        return hit

    @_atomic_mutation
    def full_check(self) -> dict[str, list]:
        """Run all checks in one call. Returns dict of events."""
        events = {
            "stop_losses": [],
            "trailing_stops": [],
            "take_profits": {},
        }
        events["stop_losses"] = self.check_stop_losses()
        events["trailing_stops"] = [t for t, _ in self.check_trailing_stops()]
        events["take_profits"] = self.check_take_profits()
        self.update_trailing_stops()
        return events

    # --- Analytics ---

    def total_market_value(self) -> float:
        """Sum of all position market values."""
        return sum(p.market_value for p in self.positions.values())

    def total_unrealized_pnl(self) -> float:
        """Sum of unrealized P/L across all positions."""
        return sum(p.unrealized_pnl for p in self.positions.values())

    def sector_exposure(self) -> dict[str, float]:
        """Calculate exposure per sector as fraction of total market value.

        Returns:
            Dict mapping sector name -> fraction (0.0 to 1.0).
        """
        mv = self.total_market_value()
        if mv == 0:
            return {}

        exposure: dict[str, float] = {}
        for pos in self.positions.values():
            sector = pos.sector or "Unknown"
            exposure[sector] = exposure.get(sector, 0) + pos.market_value

        return {s: v / mv for s, v in exposure.items()}

    def overexposed_sectors(self) -> dict[str, float]:
        """Return sectors exceeding max_sector_pct.

        Returns:
            Dict mapping sector -> actual fraction (only over-limit).
        """
        exposure = self.sector_exposure()
        return {s: f for s, f in exposure.items() if f > self.max_sector_pct}

    def summary_df(self) -> pd.DataFrame:
        """Return positions as a DataFrame for display."""
        if not self.positions:
            return pd.DataFrame()
        rows = [asdict(p) for p in self.positions.values()]
        df = pd.DataFrame(rows)
        cols = [
            "ticker", "shares", "entry_price", "current_price", "stop_loss",
            "trailing_stop", "peak_price", "unrealized_pnl", "unrealized_pnl_pct",
            "take_profit_levels", "tp_hit", "sector", "strategy", "entry_date",
        ]
        return df[[c for c in cols if c in df.columns]]

    def closed_trades_df(self) -> pd.DataFrame:
        """Return closed trades as a DataFrame."""
        if not self.closed_trades:
            return pd.DataFrame()
        return pd.DataFrame(self.closed_trades)

    def stats(self) -> dict:
        """Portfolio statistics."""
        closed = self.closed_trades
        winners = [t for t in closed if t["pnl"] > 0]
        losers = [t for t in closed if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in closed)

        return {
            "open_positions": len(self.positions),
            "total_market_value": round(self.total_market_value(), 2),
            "total_unrealized_pnl": round(self.total_unrealized_pnl(), 2),
            "closed_trades": len(closed),
            "winning_trades": len(winners),
            "losing_trades": len(losers),
            "win_rate": len(winners) / len(closed) if closed else 0,
            "total_realized_pnl": round(total_pnl, 2),
            "overexposed_sectors": self.overexposed_sectors(),
        }
