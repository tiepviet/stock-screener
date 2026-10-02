"""Regression tests for the final hardening pass.

Covers manual portfolio admission limits, alert-scan rate policy, canonical
forwarded-address keys, atomic watchlist batches, and legacy-migration identity
verification.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import security as security_module
from backend.main import create_app
from src.stock_screener import auth, db, jwt_auth, portfolio, user_store
from src.stock_screener.risk_management import PositionPlan


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hardening.db")
    monkeypatch.setattr(jwt_auth, "_SECRET_PATH", tmp_path / "jwt.key")
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    monkeypatch.setattr(security_module, "_RATE_LIMITER", security_module._RateLimiter())
    monkeypatch.setattr(user_store, "MAX_WATCHLIST", 3)
    for name in (
        "TSE_ADMIN_USER",
        "TSE_ADMIN_PASSWORD",
        "TSE_TRUSTED_PROXY_CIDRS",
        "TSE_LEGACY_PORTFOLIO_USER_ID",
        "TSE_LEGACY_PORTFOLIO_USERNAME",
    ):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app()) as test_client:
        yield test_client


def login(client: TestClient, username: str = "alice") -> str:
    auth.create_user(username, "correct-password")
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "correct-password"}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def plan(shares: int, entry: float, stop: float, ticker: str = "7203") -> PositionPlan:
    return PositionPlan(
        ticker=ticker,
        entry_price=entry,
        stop_loss=stop,
        shares=shares,
        position_value=entry * shares,
        risk_amount=(entry - stop) * shares,
        risk_pct=0.01,
        strategy="manual",
    )


# --- Portfolio admission limits ---------------------------------------------


def test_tracker_rejects_position_larger_than_capital(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    tracker = portfolio.PortfolioTracker(total_capital=1_000, risk_per_trade=0.01, user_id=1)
    with pytest.raises(ValueError, match="capital"):
        tracker.add_position(plan(shares=10, entry=500, stop=450), sector="Tech")
    assert tracker.positions == {}


def test_tracker_rejects_position_over_per_trade_risk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    tracker = portfolio.PortfolioTracker(total_capital=1_000_000, risk_per_trade=0.01, user_id=1)
    # 1% of capital is 10,000; this position risks 50,000.
    with pytest.raises(ValueError, match="risk"):
        tracker.add_position(plan(shares=100, entry=1_000, stop=500), sector="Tech")
    assert tracker.positions == {}


def test_tracker_enforces_aggregate_capital(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    tracker = portfolio.PortfolioTracker(
        total_capital=1_000, max_sector_pct=1.0, risk_per_trade=0.5, user_id=1
    )
    tracker.add_position(plan(shares=5, entry=100, stop=95, ticker="7203"), sector="Tech")
    with pytest.raises(ValueError, match="capital"):
        tracker.add_position(plan(shares=6, entry=100, stop=95, ticker="6758"), sector="Tech")
    assert set(tracker.positions) == {"7203"}


def test_tracker_enforces_sector_concentration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    tracker = portfolio.PortfolioTracker(
        total_capital=1_000, max_sector_pct=0.30, risk_per_trade=0.5, user_id=1
    )
    tracker.add_position(plan(shares=3, entry=100, stop=95, ticker="7203"), sector="Tech")
    with pytest.raises(ValueError, match="sector"):
        tracker.add_position(plan(shares=1, entry=100, stop=95, ticker="6758"), sector="Tech")
    assert set(tracker.positions) == {"7203"}


def test_api_rejects_over_capital_position_with_422(client: TestClient) -> None:
    token = login(client)
    client.put(
        "/api/v1/me/settings/sidebar",
        headers=headers(token),
        json={"capital": 100_000, "risk_percent": 1.0, "hard_stop_percent": 7},
    )
    response = client.post(
        "/api/v1/portfolio/positions",
        headers=headers(token),
        json={
            "ticker": "7203",
            "shares": 1_000,
            "entry_price": 1_000,
            "stop_loss": 900,
            "sector": "Tech",
        },
    )
    assert response.status_code == 422
    assert "risk" in response.text.lower() or "capital" in response.text.lower()


# --- Alert scan rate policy --------------------------------------------------


def test_alert_scan_uses_alert_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "alert-rate.db")
    monkeypatch.setattr(jwt_auth, "_SECRET_PATH", tmp_path / "jwt.key")
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    monkeypatch.setattr(security_module, "_RATE_LIMITER", security_module._RateLimiter())
    monkeypatch.setenv("TSE_ALERT_RATE_LIMIT", "1")
    for name in ("TSE_ADMIN_USER", "TSE_ADMIN_PASSWORD", "TSE_TRUSTED_PROXY_CIDRS"):
        monkeypatch.delenv(name, raising=False)

    auth.create_user("alice", "correct-password")
    with TestClient(create_app()) as test_client:
        token = test_client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "correct-password"},
        ).json()["access_token"]
        first = test_client.post(
            "/api/v1/alerts/scans",
            headers=headers(token),
            json={"tickers": ["7203"], "send_alerts": False},
        )
        second = test_client.post(
            "/api/v1/alerts/scans",
            headers=headers(token),
            json={"tickers": ["6758"], "send_alerts": False},
        )
    assert first.status_code != 429
    assert second.status_code == 429


# --- Forwarded address canonicalization -------------------------------------


def test_equivalent_ipv6_forms_collapse_to_one_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TSE_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")

    def request(forwarded: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [(b"x-forwarded-for", forwarded.encode())],
                "client": ("10.0.0.10", 1234),
                "scheme": "http",
                "server": ("testserver", 80),
            }
        )

    canonical = security_module.resolve_client_ip(request("2001:db8::1"))
    assert security_module.resolve_client_ip(request("2001:0db8:0:0:0:0:0:1")) == canonical
    assert security_module.resolve_client_ip(request("2001:DB8::1")) == canonical


# --- Atomic watchlist batch -------------------------------------------------


def test_watchlist_batch_does_not_partially_insert(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "watchlist.db")
    monkeypatch.setattr(user_store, "MAX_WATCHLIST", 2)
    user = auth.create_user("alice", "correct-password")
    user_store.add_to_watchlist(user.id, "7203")
    with pytest.raises(ValueError):
        user_store.add_watchlist_many(user.id, ["6758", "9984"])
    assert user_store.get_watchlist(user.id) == ["7203"]


def test_watchlist_batch_inserts_within_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "watchlist.db")
    monkeypatch.setattr(user_store, "MAX_WATCHLIST", 3)
    user = auth.create_user("alice", "correct-password")
    user_store.add_watchlist_many(user.id, ["7203", "6758.T", "9984", "7203"])
    assert set(user_store.get_watchlist(user.id)) == {"7203", "6758", "9984"}


# --- Legacy migration identity ----------------------------------------------


def test_legacy_migration_requires_matching_username(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "identity.db")
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    alice = auth.create_user("alice", "correct-password")
    portfolio.PORTFOLIO_FILE.write_text(
        '{"positions": {}, "closed_trades": []}', encoding="utf-8"
    )
    monkeypatch.setenv("TSE_LEGACY_PORTFOLIO_USER_ID", str(alice.id))
    monkeypatch.setenv("TSE_LEGACY_PORTFOLIO_USERNAME", "bob")
    assert portfolio.migrate_legacy_portfolio_to_user(alice.id) is False
    monkeypatch.setenv("TSE_LEGACY_PORTFOLIO_USERNAME", "alice")
    assert portfolio.migrate_legacy_portfolio_to_user(alice.id) is True


def test_legacy_migration_rejects_unknown_user(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "identity2.db")
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    portfolio.PORTFOLIO_FILE.write_text(
        '{"positions": {}, "closed_trades": []}', encoding="utf-8"
    )
    monkeypatch.setenv("TSE_LEGACY_PORTFOLIO_USER_ID", "4242")
    assert portfolio.migrate_legacy_portfolio_to_user(4242) is False
