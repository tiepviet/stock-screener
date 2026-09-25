"""Single-worker background scheduler for persisted auto-scan settings."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from src.stock_screener import db
from src.stock_screener.alert import AlertScanner

from .models import AutoScanSettingsRequest

logger = logging.getLogger(__name__)
_RETRY_SECONDS = 300.0


def _schedule_row(user_id: int) -> float | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT next_run_at FROM auto_scan_state WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        value = float(row["next_run_at"])
    except (TypeError, ValueError):
        return None
    return value if value == value else None


def _write_schedule(user_id: int, next_run_at: float, last_run_at: float | None = None) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO auto_scan_state (user_id, next_run_at, last_run_at) "
            "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "next_run_at = excluded.next_run_at, last_run_at = excluded.last_run_at",
            (user_id, next_run_at, last_run_at),
        )


def reset_auto_scan_schedule(user_id: int, *, enabled: bool) -> None:
    """Persist a schedule reset when an account changes auto-scan settings."""

    if not enabled:
        with db.connect() as conn:
            conn.execute("DELETE FROM auto_scan_state WHERE user_id = ?", (user_id,))
        return
    _write_schedule(user_id, time.time())


def _can_send_alerts(user_id: int) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT can_send_alerts FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return row is not None and bool(row["can_send_alerts"])


def _claim_due(
    user_id: int,
    interval_seconds: float,
    current: float,
    state: dict[int, float],
) -> bool:
    persisted_next = _schedule_row(user_id)
    next_run = persisted_next if persisted_next is not None else state.get(user_id)
    if next_run is not None and next_run > current:
        state[user_id] = next_run
        return False

    next_due = current + interval_seconds
    _write_schedule(user_id, next_due, current)
    state[user_id] = next_due
    return True


def run_due_auto_scans(
    state: dict[int, float] | None = None,
    now: float | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Run due enabled settings once and return the number of scans started.

    ``next_run_at`` is persisted in SQLite, so a process restart does not make
    every enabled account immediately due again.  A short in-memory state map
    remains as a compatibility optimization for callers that already own one.
    """

    schedule_state = state if state is not None else {}
    current = time.time() if now is None else float(now)
    due: list[tuple[int, AutoScanSettingsRequest]] = []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT s.user_id, s.value, u.can_send_alerts "
            "FROM user_settings AS s JOIN users AS u ON u.id = s.user_id "
            "WHERE s.key = 'auto_scan'"
        ).fetchall()

    for row in rows:
        user_id = int(row["user_id"])
        try:
            raw = json.loads(row["value"])
            settings = AutoScanSettingsRequest.model_validate(raw)
        except Exception:
            logger.warning("Skipping invalid auto-scan settings for user %d", user_id)
            schedule_state[user_id] = current + _RETRY_SECONDS
            _write_schedule(user_id, current + _RETRY_SECONDS, current)
            continue
        if not settings.enabled or not bool(row["can_send_alerts"]):
            schedule_state[user_id] = current + _RETRY_SECONDS
            _write_schedule(user_id, current + _RETRY_SECONDS, current)
            continue
        if _claim_due(
            user_id,
            settings.interval_minutes * 60.0,
            current,
            schedule_state,
        ):
            due.append((user_id, settings))

    started = 0
    for user_id, settings in due:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            if not _can_send_alerts(user_id):
                continue
            scanner = AlertScanner(
                tickers=settings.tickers,
                lookback_days=settings.lookback_days,
            )
            results = scanner.scan()
            if stop_event is not None and stop_event.is_set():
                continue
            # Re-check after provider work, immediately before the irreversible
            # external delivery, so a revoked capability takes effect promptly.
            if not _can_send_alerts(user_id):
                continue
            scanner.deliver_results(
                results,
                stop_event=stop_event,
                can_deliver=lambda: _can_send_alerts(user_id),
            )
            started += 1
        except Exception:
            logger.exception("Scheduled auto-scan failed")
    return started


async def auto_scan_loop(stop: asyncio.Event) -> None:
    """Poll settings until the application lifespan requests shutdown."""

    state: dict[int, float] = {}
    worker_stop = threading.Event()
    try:
        while not stop.is_set():
            try:
                # Synchronous SQLite/provider work stays off the event loop.
                from starlette.concurrency import run_in_threadpool

                await run_in_threadpool(run_due_auto_scans, state, None, worker_stop)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Auto-scan scheduler iteration failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=15)
            except asyncio.TimeoutError:  # noqa: UP041
                continue
    finally:
        # If the await above is cancelled while a provider call is in a worker
        # thread, signal that worker not to start an external send.
        worker_stop.set()


__all__ = ["auto_scan_loop", "reset_auto_scan_schedule", "run_due_auto_scans"]
