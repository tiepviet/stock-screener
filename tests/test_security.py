"""Security and persistence regression tests for the FastAPI boundary."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import security as security_module
from backend.main import create_app
from backend.security import resolve_client_ip
from src.stock_screener import auth, db, jwt_auth, portfolio
from src.stock_screener.risk_management import PositionPlan


@pytest.fixture
def secure_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "security.db")
    monkeypatch.setattr(jwt_auth, "_SECRET_PATH", tmp_path / "jwt.key")
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    monkeypatch.setattr(security_module, "_RATE_LIMITER", security_module._RateLimiter())
    monkeypatch.delenv("TSE_ADMIN_USER", raising=False)
    monkeypatch.delenv("TSE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("TSE_TRUSTED_PROXY_CIDRS", raising=False)
    with TestClient(create_app()) as client:
        yield client


def _token(client: TestClient, username: str = "alice") -> str:
    auth.create_user(username, "correct-password")
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "correct-password"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_malformed_percentage_aliases_are_validation_errors(secure_client: TestClient) -> None:
    token = _token(secure_client)
    headers = _headers(token)
    malformed_values = [None, True, {"nested": "value"}, "not-a-number", "NaN"]

    for field in (
        "risk_percent",
        "hard_stop_percent",
    ):
        for value in malformed_values:
            response = secure_client.post(
                "/api/v1/signals/scans",
                headers=headers,
                json={"tickers": ["7203"], field: value},
            )
            assert response.status_code == 422, (field, value)

    for field in ("min_roe_percent", "min_dividend_percent"):
        for value in malformed_values:
            response = secure_client.post(
                "/api/v1/screeners/fundamental",
                headers=headers,
                json={"tickers": ["7203"], field: value},
            )
            assert response.status_code == 422, (field, value)


def test_validation_errors_never_echo_passwords(secure_client: TestClient) -> None:
    secret = "super-secret-password-that-must-not-be-returned"
    too_long = secure_client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": secret * 20},
    )
    assert too_long.status_code == 422
    assert secret not in too_long.text
    assert "input" not in too_long.text

    extra_field = secure_client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": secret, "unexpected": True},
    )
    assert extra_field.status_code == 422
    assert secret not in extra_field.text


def test_security_headers_and_api_cache_policy(secure_client: TestClient) -> None:
    response = secure_client.get("/api/v1/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_rate_limited_security_response_keeps_cors_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cors.db")
    monkeypatch.setattr(jwt_auth, "_SECRET_PATH", tmp_path / "jwt.key")
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    monkeypatch.setattr(security_module, "_RATE_LIMITER", security_module._RateLimiter())
    monkeypatch.setenv("TSE_CORS_ORIGINS", "https://frontend.example")
    monkeypatch.setenv("TSE_PUBLIC_RATE_LIMIT", "1")
    for name in ("TSE_ADMIN_USER", "TSE_ADMIN_PASSWORD", "TSE_TRUSTED_PROXY_CIDRS"):
        monkeypatch.delenv(name, raising=False)
    origin = {"Origin": "https://frontend.example"}
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/health", headers=origin).status_code == 200
        preflight = client.options(
            "/api/v1/auth/login",
            headers={
                **origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        limited = client.get("/api/v1/health", headers=origin)
    assert preflight.status_code == 200
    assert preflight.headers["x-content-type-options"] == "nosniff"
    assert preflight.headers["access-control-allow-origin"] == "https://frontend.example"
    assert limited.status_code == 429
    assert limited.headers["access-control-allow-origin"] == "https://frontend.example"


def test_forwarded_for_requires_explicit_proxy_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    def request(peer: str, forwarded: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [(b"x-forwarded-for", forwarded.encode())],
                "client": (peer, 1234),
                "scheme": "http",
                "server": ("testserver", 80),
            }
        )

    assert resolve_client_ip(request("10.0.0.10", "203.0.113.9")) == "10.0.0.10"
    monkeypatch.setenv("TSE_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    assert resolve_client_ip(request("10.0.0.10", "203.0.113.9")) == "203.0.113.9"
    assert resolve_client_ip(request("10.0.0.10", "203.0.113.9, 10.0.0.11")) == "203.0.113.9"
    assert resolve_client_ip(request("192.0.2.10", "203.0.113.9")) == "192.0.2.10"
    monkeypatch.setenv("TSE_TRUSTED_PROXY_CIDRS", "0.0.0.0/0")
    assert resolve_client_ip(request("10.0.0.10", "203.0.113.9")) == "10.0.0.10"


def test_alert_delivery_is_admin_only_and_message_is_fixed(
    secure_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice_token = _token(secure_client, "alice")
    bob_token = _token(secure_client, "bob")
    monkeypatch.setenv("TSE_ADMIN_USER", "alice")
    auth.set_alert_capability(auth.get_by_username("alice").id, True)
    sent: list[str] = []

    class FakeTelegram:
        def send(self, message: str) -> bool:
            sent.append(message)
            return True

    monkeypatch.setattr("backend.main.TelegramSender", FakeTelegram)
    assert secure_client.post(
        "/api/v1/alerts/channels/telegram/test", headers=_headers(bob_token)
    ).status_code == 403

    custom = secure_client.post(
        "/api/v1/alerts/channels/telegram/test",
        headers=_headers(alice_token),
        json={"message": "send an arbitrary message"},
    )
    assert custom.status_code == 422
    assert not sent

    response = secure_client.post(
        "/api/v1/alerts/channels/telegram/test", headers=_headers(alice_token)
    )
    assert response.status_code == 200
    assert sent == ["Test alert from TSE Stock Screener"]


def test_alert_scan_rechecks_capability_after_provider_work(
    secure_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _token(secure_client, "alice")
    user = auth.get_by_username("alice")
    assert user is not None
    auth.set_alert_capability(user.id, True)
    delivered: list[str] = []

    class FakeScanner:
        def __init__(self, tickers: list[str], lookback_days: int) -> None:
            pass

        def scan(self) -> dict[str, list[object]]:
            auth.set_alert_capability(user.id, False)
            return {}

        def deliver_results(self, results, stop_event=None, can_deliver=None) -> bool:
            delivered.append("sent")
            return True

    monkeypatch.setattr("backend.services.AlertScanner", FakeScanner)
    response = secure_client.post(
        "/api/v1/alerts/scans",
        headers=_headers(token),
        json={"tickers": ["7203"], "send_alerts": True},
    )
    assert response.status_code == 403
    assert delivered == []


def test_corrupt_portfolio_is_quarantined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    legacy = portfolio._portfolio_path(7)
    legacy.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(portfolio.PortfolioStorageError):
        portfolio.PortfolioTracker(user_id=7)
    assert not legacy.exists()
    quarantine_files = list(legacy.parent.glob("portfolio_user_7.json.corrupt-*"))
    assert len(quarantine_files) == 1
    assert (legacy.parent / ".portfolio_user_7.json.unavailable").exists()


def test_quarantine_marker_failure_leaves_active_file_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    legacy = portfolio._portfolio_path(8)
    legacy.write_text("{not valid json", encoding="utf-8")
    original_write = portfolio._atomic_json_write

    def fail_marker(path: Path, payload: str) -> None:
        if path.name.endswith(".unavailable"):
            raise OSError("marker unavailable")
        original_write(path, payload)

    monkeypatch.setattr(portfolio, "_atomic_json_write", fail_marker)
    with pytest.raises(portfolio.PortfolioStorageError):
        portfolio.PortfolioTracker(user_id=8)
    assert legacy.exists()
    with pytest.raises(portfolio.PortfolioStorageError):
        portfolio.PortfolioTracker(user_id=8)


def test_portfolio_transactions_do_not_lose_concurrent_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")

    def add(ticker: str) -> None:
        tracker = portfolio.PortfolioTracker(user_id=12)
        plan = PositionPlan(
            ticker=ticker,
            entry_price=1000.0,
            stop_loss=900.0,
            shares=10,
            position_value=10_000.0,
            risk_amount=1_000.0,
            risk_pct=0.01,
            strategy="manual",
        )
        with tracker.transaction() as locked:
            locked.add_position(plan, sector="Test")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(add, ["7203", "6758"]))

    tracker = portfolio.PortfolioTracker(user_id=12)
    assert set(tracker.positions) == {"7203", "6758"}


def test_legacy_portfolio_migration_requires_explicit_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    legacy = {
        "positions": {},
        "closed_trades": [{"ticker": "7203", "pnl": 1.0}],
    }
    portfolio.PORTFOLIO_FILE.write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.delenv("TSE_LEGACY_PORTFOLIO_USER_ID", raising=False)
    assert portfolio.migrate_legacy_portfolio_to_user(4) is False
    assert not portfolio._portfolio_path(4).exists()

    monkeypatch.setenv("TSE_LEGACY_PORTFOLIO_USER_ID", "4")
    assert portfolio.migrate_legacy_portfolio_to_user(4) is True
    assert portfolio._portfolio_path(4).exists()
    assert portfolio.PORTFOLIO_FILE.exists()
    destination = portfolio._portfolio_path(4)
    destination.unlink()
    assert portfolio.migrate_legacy_portfolio_to_user(4) is False
    assert not destination.exists()
