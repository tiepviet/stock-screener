"""HTTP-level tests for authentication and per-user API state."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from src.stock_screener import auth, db, jwt_auth, portfolio


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create an isolated API client with a temporary database and portfolio."""

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api.db")
    monkeypatch.setattr(jwt_auth, "_SECRET_PATH", tmp_path / "jwt.key")
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    monkeypatch.delenv("TSE_ADMIN_USER", raising=False)
    monkeypatch.delenv("TSE_ADMIN_PASSWORD", raising=False)
    with TestClient(create_app()) as client:
        yield client


def _login(client: TestClient, username: str = "alice") -> str:
    auth.create_user(username, "correct-password")
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "correct-password"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_and_setup_status(api_client: TestClient) -> None:
    health = api_client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    setup = api_client.get("/api/v1/auth/setup-status")
    assert setup.status_code == 200
    assert setup.json() == {"has_users": False, "needs_setup": True}


def test_authentication_and_token_revocation(api_client: TestClient) -> None:
    token = _login(api_client)
    me = api_client.get("/api/v1/auth/me", headers=_headers(token))
    assert me.status_code == 200
    assert me.json()["username"] == "alice"
    assert me.json()["can_send_alerts"] is False

    logout = api_client.post("/api/v1/auth/logout", headers=_headers(token))
    assert logout.status_code == 200
    assert api_client.get("/api/v1/auth/me", headers=_headers(token)).status_code == 401


def test_user_state_round_trip_and_canonical_tickers(api_client: TestClient) -> None:
    token = _login(api_client)
    headers = _headers(token)

    watchlist = api_client.post(
        "/api/v1/me/watchlist",
        headers=headers,
        json={"tickers": ["7203", "6758", "7203.T"]},
    )
    assert watchlist.status_code == 200
    assert set(watchlist.json()) == {"6758", "7203"}

    rows = api_client.put(
        "/api/v1/me/profit-target-rows",
        headers=headers,
        json={
            "rows": [
                {
                    "ticker": "7203.T",
                    "entry_price": 2000,
                    "target_pct": 5,
                    "shares": 100,
                }
            ]
        },
    )
    assert rows.status_code == 200
    assert rows.json()["rows"][0]["ticker"] == "7203.T"
    saved_rows = api_client.get("/api/v1/me/profit-target-rows", headers=headers)
    assert saved_rows.json()["rows"][0]["ticker"] == "7203"

    settings = api_client.put(
        "/api/v1/me/settings/sidebar",
        headers=headers,
        json={
            "capital": 20_000_000,
            "risk_percent": 1.5,
            "hard_stop_percent": 8,
            "lookback_days": 730,
            "language": "VN",
        },
    )
    assert settings.status_code == 200
    assert settings.json()["capital"] == 20_000_000
    assert settings.json()["risk_percent"] == 1.5
    assert settings.json()["hard_stop_percent"] == 8


def test_protected_endpoint_rejects_missing_token(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/portfolio")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_user_portfolios_are_isolated(api_client: TestClient) -> None:
    alice_token = _login(api_client, "alice")
    bob_token = _login(api_client, "bob")

    created = api_client.post(
        "/api/v1/portfolio/positions",
        headers=_headers(alice_token),
        json={
            "ticker": "7203",
            "shares": 100,
            "entry_price": 2000,
            "stop_loss": 1800,
            "strategy": "manual",
            "sector": "Industrials",
        },
    )
    assert created.status_code == 201
    assert [p["ticker"] for p in created.json()["positions"]] == ["7203"]

    bob = api_client.get("/api/v1/portfolio", headers=_headers(bob_token))
    assert bob.status_code == 200
    assert bob.json()["positions"] == []


def test_portfolio_duplicate_and_missing_position_errors(api_client: TestClient) -> None:
    token = _login(api_client)
    payload = {
        "ticker": "7203",
        "shares": 100,
        "entry_price": 2000,
        "stop_loss": 1800,
        "strategy": "manual",
    }
    assert api_client.post(
        "/api/v1/portfolio/positions", headers=_headers(token), json=payload
    ).status_code == 201
    assert api_client.post(
        "/api/v1/portfolio/positions", headers=_headers(token), json=payload
    ).status_code == 409
    assert api_client.post(
        "/api/v1/portfolio/positions/6758/close",
        headers=_headers(token),
        json={"exit_price": 2000, "reason": "MANUAL"},
    ).status_code == 404
