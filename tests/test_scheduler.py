"""Regression tests for the durable single-worker auto-scan scheduler."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import scheduler
from src.stock_screener import auth, db, user_store


@pytest.fixture
def scheduler_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scheduler.db")
    db.init_db()


def test_auto_scan_schedule_survives_process_state_restart(
    scheduler_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = auth.create_user("alice", "correct-password")
    auth.set_alert_capability(user.id, True)
    user_store.set_setting(
        user.id,
        "auto_scan",
        {
            "enabled": True,
            "interval_minutes": 30,
            "lookback_days": 365,
            "tickers": ["7203"],
        },
    )
    calls: list[str] = []

    class FakeScanner:
        def __init__(self, tickers: list[str], lookback_days: int) -> None:
            calls.append("init")

        def scan(self) -> dict[str, list[object]]:
            calls.append("scan")
            return {}

        def deliver_results(
            self,
            results: dict[str, list[object]],
            stop_event=None,
            can_deliver=None,
        ) -> bool:
            calls.append("deliver")
            return True

    monkeypatch.setattr(scheduler, "AlertScanner", FakeScanner)
    assert scheduler.run_due_auto_scans({}, now=1_000.0) == 1
    assert calls == ["init", "scan", "deliver"]

    # A fresh in-memory state map must still honor the persisted next run.
    assert scheduler.run_due_auto_scans({}, now=1_001.0) == 0
    assert calls == ["init", "scan", "deliver"]
    assert scheduler.run_due_auto_scans({}, now=2_800.0) == 1
    assert calls == ["init", "scan", "deliver", "init", "scan", "deliver"]


def test_reset_auto_scan_schedule_overrides_in_memory_next_run(
    scheduler_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = auth.create_user("alice", "correct-password")
    auth.set_alert_capability(user.id, True)
    user_store.set_setting(
        user.id,
        "auto_scan",
        {
            "enabled": True,
            "interval_minutes": 30,
            "lookback_days": 365,
            "tickers": ["7203"],
        },
    )
    monkeypatch.setattr(scheduler.time, "time", lambda: 1_000.0)
    scheduler.reset_auto_scan_schedule(user.id, enabled=True)
    state = {user.id: 2_000.0}

    class FakeScanner:
        def __init__(self, tickers: list[str], lookback_days: int) -> None:
            pass

        def scan(self) -> dict[str, list[object]]:
            return {}

        def deliver_results(
            self,
            results: dict[str, list[object]],
            stop_event=None,
            can_deliver=None,
        ) -> bool:
            return True

    monkeypatch.setattr(scheduler, "AlertScanner", FakeScanner)
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
