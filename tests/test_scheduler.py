"""Regression tests for the durable single-worker auto-scan scheduler."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend import scheduler
from src.stock_screener import auth, db, user_store


class FakeScanner:
    """Records scan/delivery calls; delivery always succeeds."""

    calls: list[str] = []
    fail_on_scan = False

    def __init__(self, tickers: list[str], lookback_days: int) -> None:
        type(self).calls.append("init")

    def scan(self) -> dict[str, list[object]]:
        type(self).calls.append("scan")
        if type(self).fail_on_scan:
            raise RuntimeError("provider exploded")
        return {}

    def deliver_results(self, results, stop_event=None, can_deliver=None) -> bool:
        type(self).calls.append("deliver")
        return True


@pytest.fixture
def scheduler_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scheduler.db")
    db.init_db()
    FakeScanner.calls = []
    FakeScanner.fail_on_scan = False
    monkeypatch.setattr(scheduler, "AlertScanner", FakeScanner)


def enable_auto_scan(user_id: int, interval_minutes: int = 30) -> None:
    user_store.set_setting(
        user_id,
        "auto_scan",
        {
            "enabled": True,
            "interval_minutes": interval_minutes,
            "lookback_days": 365,
            "tickers": ["7203"],
        },
    )


def test_auto_scan_schedule_survives_process_state_restart(scheduler_db: None) -> None:
    user = auth.create_user("alice", "correct-password")
    auth.set_alert_capability(user.id, True)
    enable_auto_scan(user.id)

    assert scheduler.run_due_auto_scans({}, now=1_000.0) == 1
    assert FakeScanner.calls == ["init", "scan", "deliver"]

    # A fresh in-memory state map must still honor the persisted next run.
    assert scheduler.run_due_auto_scans({}, now=1_001.0) == 0
    assert FakeScanner.calls == ["init", "scan", "deliver"]
    assert scheduler.run_due_auto_scans({}, now=2_800.0) == 1
    assert FakeScanner.calls == ["init", "scan", "deliver", "init", "scan", "deliver"]


def test_concurrent_claims_only_broadcast_once(scheduler_db: None) -> None:
    user = auth.create_user("alice", "correct-password")
    auth.set_alert_capability(user.id, True)
    enable_auto_scan(user.id)

    def claim() -> int:
        return scheduler.run_due_auto_scans({}, now=1_000.0)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: claim(), range(4)))

    assert sum(results) == 1
    assert FakeScanner.calls.count("deliver") == 1


def test_failed_scan_rearms_the_slot(scheduler_db: None) -> None:
    user = auth.create_user("alice", "correct-password")
    auth.set_alert_capability(user.id, True)
    enable_auto_scan(user.id, interval_minutes=30)
    FakeScanner.fail_on_scan = True

    assert scheduler.run_due_auto_scans({}, now=1_000.0) == 0
    assert "deliver" not in FakeScanner.calls

    # The deferred slot is retried shortly, not a full interval later.
    FakeScanner.fail_on_scan = False
    assert scheduler.run_due_auto_scans({}, now=1_000.0 + scheduler._RETRY_SECONDS) == 1


def test_shutdown_before_delivery_rearms_the_slot(scheduler_db: None) -> None:
    user = auth.create_user("alice", "correct-password")
    auth.set_alert_capability(user.id, True)
    enable_auto_scan(user.id, interval_minutes=30)
    stop = threading.Event()
    stop.set()

    assert scheduler.run_due_auto_scans({}, now=1_000.0, stop_event=stop) == 0
    assert FakeScanner.calls == []
    assert scheduler.run_due_auto_scans({}, now=1_000.0 + scheduler._RETRY_SECONDS) == 1


def test_reset_auto_scan_schedule_overrides_in_memory_next_run(
    scheduler_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = auth.create_user("alice", "correct-password")
    auth.set_alert_capability(user.id, True)
    enable_auto_scan(user.id)
    monkeypatch.setattr(scheduler.time, "time", lambda: 1_000.0)
    scheduler.reset_auto_scan_schedule(user.id, enabled=True)
    state = {user.id: 2_000.0}
    assert scheduler.run_due_auto_scans(state, now=1_000.0) == 1


def test_disabled_schedule_is_not_due(scheduler_db: None) -> None:
    user = auth.create_user("alice", "correct-password")
    user_store.set_setting(
        user.id,
        "auto_scan",
        {
            "enabled": False,
            "interval_minutes": 30,
            "lookback_days": 365,
            "tickers": ["7203"],
        },
    )
    assert scheduler.run_due_auto_scans({}, now=1_000.0) == 0
