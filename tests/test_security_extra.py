"""Additional migration regression tests kept separate for fast iteration."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import security as security_module
from backend.main import create_app
from backend.security import SecurityHeadersMiddleware
from src.stock_screener import auth, db, jwt_auth, portfolio
from src.stock_screener.risk_management import PositionPlan


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api.db")
    monkeypatch.setattr(jwt_auth, "_SECRET_PATH", tmp_path / "jwt.key")
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    monkeypatch.setattr(security_module, "_RATE_LIMITER", security_module._RateLimiter())
    for name in (
        "TSE_ADMIN_USER",
        "TSE_ADMIN_PASSWORD",
        "TSE_TRUSTED_PROXY_CIDRS",
        "TSE_LEGACY_PORTFOLIO_USER_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app()) as test_client:
        yield test_client


def _run_asgi_request(
    messages: list[dict],
    *,
    method: str = "POST",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, list[dict], bool]:
    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True
        request = Request(scope, receive)
        body = await request.body()
        response = JSONResponse({"length": len(body)})
        await response(scope, receive, send)

    async def run() -> list[dict]:
        sent: list[dict] = []
        queue = list(messages)

        async def receive():
            return queue.pop(0) if queue else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/probe",
            "raw_path": b"/probe",
            "query_string": b"",
            "root_path": "",
            "headers": headers or [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
        await SecurityHeadersMiddleware(downstream)(scope, receive, send)
        return sent

    sent = asyncio.run(run())
    status = next(
        message["status"] for message in sent if message["type"] == "http.response.start"
    )
    return status, sent, called


def test_body_limits_cover_unframed_get_and_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TSE_MAX_REQUEST_BYTES", "1024")
    monkeypatch.setattr(security_module, "_RATE_LIMITER", security_module._RateLimiter())
    status, _, called = _run_asgi_request(
        [{"type": "http.disconnect"}],
    )
    assert status == 400
    assert called is False

    status, _, called = _run_asgi_request(
        [{"type": "http.request", "body": b"x" * 1025, "more_body": False}],
        method="GET",
    )
    assert status == 413
    assert called is False


def test_content_length_mismatch_is_rejected_before_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TSE_MAX_REQUEST_BYTES", "1024")
    monkeypatch.setattr(security_module, "_RATE_LIMITER", security_module._RateLimiter())
    status, _, called = _run_asgi_request(
        [{"type": "http.request", "body": b"ab", "more_body": False}],
        headers=[(b"content-length", b"3")],
    )
    assert status == 400
    assert called is False


def login(client: TestClient, username: str = "alice", password: str = "correct-password") -> str:
    auth.create_user(username, password)
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_alias_conflicts_and_huge_values_are_rejected(client: TestClient) -> None:
    token = login(client)
    response = client.post(
        "/api/v1/signals/scans",
        headers=headers(token),
        json={"tickers": ["7203"], "risk_per_trade": 0.01, "risk_percent": []},
    )
    assert response.status_code == 422

    response = client.post(
        "/api/v1/signals/scans",
        headers={**headers(token), "Content-Type": "application/json"},
        content=b'{"tickers":["7203"],"risk_percent":1e10000}',
    )
    assert response.status_code == 422
    assert "input" not in response.text


def test_percentage_fields_have_unambiguous_units(client: TestClient) -> None:
    token = login(client)
    settings = client.put(
        "/api/v1/me/settings/sidebar",
        headers=headers(token),
        json={"risk_pct": 1.0, "hard_stop_pct": 0.07},
    )
    assert settings.status_code == 200
    assert settings.json()["risk_percent"] == 1.0
    assert settings.json()["hard_stop_percent"] == 7.0

    # 1 means one percent, not 100 percent of capital.
    from backend.models import SignalScanRequest

    parsed = SignalScanRequest.model_validate(
        {"tickers": ["7203"], "risk_percent": 1.0}
    )
    assert parsed.risk_per_trade == 0.01


def test_ticker_suffix_is_one_portfolio_identity(client: TestClient) -> None:
    token = login(client)
    payload = {
        "ticker": "7203.T",
        "shares": 10,
        "entry_price": 1000,
        "stop_loss": 900,
        "strategy": "manual",
        "sector": "Test",
    }
    first = client.post("/api/v1/portfolio/positions", headers=headers(token), json=payload)
    assert first.status_code == 201
    assert first.json()["positions"][0]["ticker"] == "7203"

    duplicate = client.post(
        "/api/v1/portfolio/positions",
        headers=headers(token),
        json={**payload, "ticker": "7203"},
    )
    assert duplicate.status_code == 409
    close = client.post(
        "/api/v1/portfolio/positions/7203/close",
        headers=headers(token),
        json={"exit_price": 1100, "reason": "MANUAL"},
    )
    assert close.status_code == 200
    assert close.json()["positions"] == []


def test_password_whitespace_is_preserved(client: TestClient) -> None:
    password = "  pass with spaces  "
    token = login(client, "alice", password)
    assert token
    response = client.post(
        "/api/v1/auth/change-password",
        headers=headers(token),
        json={"current_password": password, "new_password": "  next password  "},
    )
    assert response.status_code == 200
    new_token = response.json()["access_token"]
    assert client.get("/api/v1/auth/me", headers=headers(new_token)).status_code == 200


def test_direct_portfolio_mutators_are_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")

    def add(ticker: str) -> None:
        tracker = portfolio.PortfolioTracker(user_id=22)
        tracker.add_position(
            PositionPlan(
                ticker=ticker,
                entry_price=100,
                stop_loss=90,
                shares=1,
                position_value=100,
                risk_amount=10,
                risk_pct=0.01,
                strategy="manual",
            ),
            sector="Test",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(add, ["7203", "6758"]))
    assert set(portfolio.PortfolioTracker(user_id=22).positions) == {"7203", "6758"}


def test_alert_claims_are_per_channel_and_retry_failed_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    tracker = portfolio.PortfolioTracker(user_id=23)
    events = {"stop_losses": ["7203"], "trailing_stops": [], "take_profits": {}}
    claims = tracker.claim_alert_events(events, ("telegram", "slack"))
    assert set(claims) == {"telegram", "slack"}
    signature = claims["telegram"]
    tracker.record_alert_delivery(signature, "telegram", delivered=True)
    tracker.record_alert_delivery(signature, "slack", delivered=False)
    assert tracker.claim_alert_events(events, ("telegram", "slack")) == {
        "slack": signature
    }
    tracker.record_alert_delivery(signature, "slack", delivered=True)
    assert tracker.claim_alert_events(events, ("telegram", "slack")) == {}


def test_alert_claim_is_atomic_across_tracker_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(portfolio, "PORTFOLIO_FILE", tmp_path / "portfolio.json")
    events = {"stop_losses": ["7203"], "trailing_stops": [], "take_profits": {}}

    def claim() -> dict[str, str]:
        return portfolio.PortfolioTracker(user_id=24).claim_alert_events(
            events, ("telegram", "slack")
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    claimed_channels = [channel for result in results for channel in result]
    assert sorted(claimed_channels) == ["slack", "telegram"]


def test_oversized_request_body_is_rejected_before_parsing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TSE_MAX_REQUEST_BYTES", "1024")
    response = client.post(
        "/api/v1/auth/login",
        content=b"x" * 2048,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "Request body is too large"


def test_chunked_request_body_is_replayed_to_json_parser(client: TestClient) -> None:
    auth.create_user("alice", "correct-password")

    def body_chunks():
        yield b'{"username":"alice",'
        yield b'"password":"correct-password"}'

    response = client.post(
        "/api/v1/auth/login",
        content=body_chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_rate_policy_is_shared_across_provider_paths(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TSE_PROVIDER_RATE_LIMIT", "1")
    first = client.get("/api/v1/markets/AAA/ohlcv")
    second = client.get("/api/v1/markets/BBB/ohlcv")
    assert first.status_code in {401, 404}
    assert second.status_code == 429
    assert "retry-after" in {key.lower() for key in second.headers}
