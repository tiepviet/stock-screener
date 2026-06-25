"""Tests for db, auth, jwt_auth, user_store — all server-side persistence.

Each test uses a tmp_path fixture to keep the dev data/screener.db
untouched. The db module uses a module-level path; we patch it per-test.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest

from src.stock_screener import auth, db, jwt_auth, user_store


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect DB to a per-test file. Clears the in-process lock."""
    test_db = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    # data/ in jwt_auth points at module-level; redirect there too
    monkeypatch.setattr(jwt_auth, "_SECRET_PATH", tmp_path / "jwt.key")
    db.init_db(test_db)


# --- db ---


def test_init_db_creates_file(_isolated_db: None) -> None:
    p = db.init_db()
    assert p.exists()


def test_init_db_is_idempotent(_isolated_db: None) -> None:
    db.init_db()
    db.init_db()  # should not raise
    assert db.user_count() == 0


def test_user_count_starts_zero(_isolated_db: None) -> None:
    assert db.user_count() == 0


def test_connect_creates_schema_if_missing(tmp_path: Path) -> None:
    p = tmp_path / "auto.db"
    assert not p.exists()
    with db.connect(p) as conn:
        conn.execute("SELECT COUNT(*) FROM users").fetchone()
    assert p.exists()


# --- auth ---


def test_create_user_basic(_isolated_db: None) -> None:
    rec = auth.create_user("alice", "secret123")
    assert rec.id > 0
    assert rec.username == "alice"
    assert db.user_count() == 1


def test_create_user_duplicate_raises(_isolated_db: None) -> None:
    auth.create_user("bob", "secret123")
    with pytest.raises(ValueError, match="already exists"):
        auth.create_user("bob", "secret123")


def test_create_user_weak_password_rejected(_isolated_db: None) -> None:
    with pytest.raises(ValueError, match="at least 8"):
        auth.create_user("x", "short")


def test_create_user_empty_username_rejected(_isolated_db: None) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        auth.create_user("", "secret123")
    with pytest.raises(ValueError, match="non-empty"):
        auth.create_user("   ", "secret123")


def test_verify_user_success(_isolated_db: None) -> None:
    auth.create_user("alice", "secret123")
    rec = auth.verify_user("alice", "secret123")
    assert rec is not None
    assert rec.username == "alice"


def test_verify_user_wrong_password(_isolated_db: None) -> None:
    auth.create_user("alice", "secret123")
    assert auth.verify_user("alice", "wrong-pw") is None


def test_verify_user_unknown(_isolated_db: None) -> None:
    # Must not raise; should return None
    assert auth.verify_user("ghost", "anything") is None


def test_get_by_username(_isolated_db: None) -> None:
    auth.create_user("alice", "secret123")
    rec = auth.get_by_username("alice")
    assert rec is not None
    assert auth.get_by_username("nobody") is None


def test_change_password(_isolated_db: None) -> None:
    auth.create_user("alice", "oldpass1")
    rec = auth.get_by_username("alice")
    auth.change_password(rec.id, "newpass2")
    assert auth.verify_user("alice", "oldpass1") is None
    assert auth.verify_user("alice", "newpass2") is not None


def test_password_is_hashed_not_plain(_isolated_db: None) -> None:
    auth.create_user("alice", "secret123")
    with db.connect() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE username='alice'").fetchone()
    assert "secret123" not in str(row["password_hash"])
    assert str(row["password_hash"]).startswith("$2b$")  # bcrypt


# --- jwt_auth ---


def test_create_and_verify_token(_isolated_db: None) -> None:
    token = jwt_auth.create_token(42, "alice")
    payload = jwt_auth.verify_token(token)
    assert payload is not None
    assert payload["sub"] == "42"
    assert payload["username"] == "alice"


def test_verify_expired_token(_isolated_db: None) -> None:
    # Issue a token that's already expired
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "1", "username": "x", "iss": jwt_auth._ISSUER,
        "iat": int((now - timedelta(days=40)).timestamp()),
        "exp": int((now - timedelta(days=10)).timestamp()),
    }
    secret = jwt_auth._load_or_create_secret()
    token = jwt.encode(payload, secret, algorithm=jwt_auth._ALGO)
    assert jwt_auth.verify_token(token) is None


def test_verify_wrong_issuer(_isolated_db: None) -> None:
    payload = {
        "sub": "1", "username": "x", "iss": "evil",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    secret = jwt_auth._load_or_create_secret()
    token = jwt.encode(payload, secret, algorithm=jwt_auth._ALGO)
    assert jwt_auth.verify_token(token) is None


def test_verify_tampered_signature(_isolated_db: None) -> None:
    token = jwt_auth.create_token(1, "alice")
    tampered = token[:-4] + "ZZZZ"
    assert jwt_auth.verify_token(tampered) is None


def test_verify_empty_or_garbage(_isolated_db: None) -> None:
    assert jwt_auth.verify_token("") is None
    assert jwt_auth.verify_token(None) is None
    assert jwt_auth.verify_token("not.a.jwt") is None


def test_secret_persists_across_loads(_isolated_db: None, tmp_path: Path) -> None:
    """Two calls without recreating the file must return the same secret."""
    p = tmp_path / "persist.key"
    s1 = jwt_auth._load_or_create_secret(p)
    s2 = jwt_auth._load_or_create_secret(p)
    assert s1 == s2


def test_secret_file_has_restricted_perms(_isolated_db: None, tmp_path: Path) -> None:
    p = tmp_path / "perm.key"
    if not hasattr(p, "chmod"):
        pytest.skip("POSIX-only test")
    jwt_auth._load_or_create_secret(p)
    mode = p.stat().st_mode & 0o777
    # Owner-only — group/other must have no perms
    assert mode & 0o077 == 0, f"Secret file mode too loose: {octo(mode)}"


def octo(n: int) -> str:
    return oct(n)


def test_rotate_secret_invalidates_tokens(_isolated_db: None) -> None:
    token = jwt_auth.create_token(1, "alice")
    assert jwt_auth.verify_token(token) is not None
    jwt_auth.rotate_secret()
    assert jwt_auth.verify_token(token) is None


# --- user_store ---


def _uid() -> int:
    rec = auth.create_user("alice", "secret123")
    return rec.id


def test_get_setting_default(_isolated_db: None) -> None:
    uid = _uid()
    assert user_store.get_setting(uid, "missing", default={"x": 1}) == {"x": 1}


def test_set_and_get_setting(_isolated_db: None) -> None:
    uid = _uid()
    user_store.set_setting(uid, "k", {"a": [1, 2, 3]})
    assert user_store.get_setting(uid, "k") == {"a": [1, 2, 3]}


def test_set_setting_upserts(_isolated_db: None) -> None:
    uid = _uid()
    user_store.set_setting(uid, "k", 1)
    user_store.set_setting(uid, "k", 2)
    assert user_store.get_setting(uid, "k") == 2


def test_target_rows_roundtrip(_isolated_db: None) -> None:
    uid = _uid()
    rows = [
        {"ticker": "7203", "entry_price": 2000.0, "target_pct": 5.0, "shares": 100},
        {"ticker": "6758", "entry_price": 12000.0, "target_pct": 8.0, "shares": 0},
    ]
    user_store.save_target_rows(uid, rows)
    got = user_store.get_target_rows(uid)
    assert got == rows


def test_target_rows_replace(_isolated_db: None) -> None:
    uid = _uid()
    user_store.save_target_rows(uid, [
        {"ticker": "A", "entry_price": 100, "target_pct": 5, "shares": 0}
    ])
    user_store.save_target_rows(uid, [
        {"ticker": "B", "entry_price": 200, "target_pct": 10, "shares": 5},
        {"ticker": "C", "entry_price": 300, "target_pct": 15, "shares": 10},
    ])
    got = user_store.get_target_rows(uid)
    assert len(got) == 2
    assert [r["ticker"] for r in got] == ["B", "C"]


def test_target_rows_isolated_per_user(_isolated_db: None) -> None:
    rec1 = auth.create_user("alice", "secret123")
    rec2 = auth.create_user("bob", "secret123")
    user_store.save_target_rows(rec1.id, [
        {"ticker": "X", "entry_price": 1, "target_pct": 1, "shares": 0}
    ])
    assert user_store.get_target_rows(rec2.id) == []


def test_watchlist_add_remove(_isolated_db: None) -> None:
    uid = _uid()
    user_store.add_to_watchlist(uid, "7203")
    user_store.add_to_watchlist(uid, "6758")
    user_store.add_to_watchlist(uid, "7203")  # duplicate — should be ignored
    assert set(user_store.get_watchlist(uid)) == {"7203", "6758"}
    user_store.remove_from_watchlist(uid, "7203")
    assert user_store.get_watchlist(uid) == ["6758"]


def test_watchlist_normalizes_ticker(_isolated_db: None) -> None:
    uid = _uid()
    user_store.add_to_watchlist(uid, "  7203  ")
    user_store.add_to_watchlist(uid, "aapl")
    assert set(user_store.get_watchlist(uid)) == {"7203", "AAPL"}


def test_cascade_delete_user_removes_data(_isolated_db: None) -> None:
    uid = _uid()
    user_store.set_setting(uid, "k", "v")
    user_store.save_target_rows(uid, [
        {"ticker": "X", "entry_price": 1, "target_pct": 1, "shares": 0}
    ])
    with db.connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    assert user_store.get_setting(uid, "k") is None
    assert user_store.get_target_rows(uid) == []


def test_settings_preserve_unicode(_isolated_db: None) -> None:
    uid = _uid()
    payload = {"note": "Mã CK 株式会社 — 7203"}
    user_store.set_setting(uid, "jp", payload)
    assert user_store.get_setting(uid, "jp") == payload


# --- bootstrap_admin_from_env ---


def test_bootstrap_creates_admin_when_no_users(
    monkeypatch: pytest.MonkeyPatch, _isolated_db: None
) -> None:
    rec = auth.bootstrap_admin_from_env({
        "TSE_ADMIN_USER": "boss",
        "TSE_ADMIN_PASSWORD": "strongpw1",
    })
    assert rec is not None
    assert rec.username == "boss"
    assert auth.verify_user("boss", "strongpw1") is not None


def test_bootstrap_skips_when_users_exist(
    monkeypatch: pytest.MonkeyPatch, _isolated_db: None
) -> None:
    auth.create_user("existing", "pass1234")
    rec = auth.bootstrap_admin_from_env({
        "TSE_ADMIN_USER": "newone",
        "TSE_ADMIN_PASSWORD": "pass1234",
    })
    assert rec is None
    # Original user unchanged, new one not created
    assert auth.verify_user("existing", "pass1234") is not None
    assert auth.verify_user("newone", "pass1234") is None


def test_bootstrap_skips_when_env_missing(
    monkeypatch: pytest.MonkeyPatch, _isolated_db: None
) -> None:
    rec = auth.bootstrap_admin_from_env({
        "TSE_ADMIN_USER": "",
        "TSE_ADMIN_PASSWORD": "",
    })
    assert rec is None
    assert db.user_count() == 0


def test_bootstrap_skips_when_password_too_weak(
    monkeypatch: pytest.MonkeyPatch, _isolated_db: None
) -> None:
    rec = auth.bootstrap_admin_from_env({
        "TSE_ADMIN_USER": "boss",
        "TSE_ADMIN_PASSWORD": "short",
    })
    assert rec is None
    assert db.user_count() == 0


def test_bootstrap_strips_whitespace_in_username(
    monkeypatch: pytest.MonkeyPatch, _isolated_db: None
) -> None:
    rec = auth.bootstrap_admin_from_env({
        "TSE_ADMIN_USER": "  spaced  ",
        "TSE_ADMIN_PASSWORD": "strongpw1",
    })
    assert rec is not None
    assert rec.username == "spaced"


def test_bootstrap_creates_db_file_if_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bootstrap must work even when data/screener.db doesn't exist yet."""
    test_db = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    assert not test_db.exists()
    rec = auth.bootstrap_admin_from_env({
        "TSE_ADMIN_USER": "first",
        "TSE_ADMIN_PASSWORD": "pass1234",
    })
    assert rec is not None
    assert test_db.exists()


def test_bootstrap_idempotent(
    monkeypatch: pytest.MonkeyPatch, _isolated_db: None
) -> None:
    env = {"TSE_ADMIN_USER": "boss", "TSE_ADMIN_PASSWORD": "strongpw1"}
    rec1 = auth.bootstrap_admin_from_env(env)
    rec2 = auth.bootstrap_admin_from_env(env)  # second call: user already exists
    assert rec1 is not None
    assert rec2 is None
    assert db.user_count() == 1


# --- .env loading (real file on disk) ---


def test_env_loaded_from_project_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If user puts credentials in .env at project root, bootstrap picks them up
    even though the process env is clean."""
    import src.stock_screener.auth as auth_mod

    env_file = tmp_path / ".env"
    env_file.write_text(
        "TSE_ADMIN_USER=envuser\n"
        "TSE_ADMIN_PASSWORD=envpass1\n"
    )
    # Point auth at the tmp .env, clear process env, re-run loader
    monkeypatch.setattr(auth_mod, "_PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("TSE_ADMIN_USER", raising=False)
    monkeypatch.delenv("TSE_ADMIN_PASSWORD", raising=False)
    auth_mod._load_dotenv_once()

    test_db = tmp_path / "envtest.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(jwt_auth, "_SECRET_PATH", tmp_path / "envtest.key")

    rec = auth.bootstrap_admin_from_env()
    assert rec is not None
    assert rec.username == "envuser"
    assert auth.verify_user("envuser", "envpass1") is not None


def test_process_env_wins_over_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """override=False must let the real process env take precedence."""
    import src.stock_screener.auth as auth_mod

    env_file = tmp_path / ".env"
    env_file.write_text("TSE_ADMIN_USER=fromfile\nTSE_ADMIN_PASSWORD=filepw1\n")
    monkeypatch.setattr(auth_mod, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("TSE_ADMIN_USER", "fromenv")
    monkeypatch.setenv("TSE_ADMIN_PASSWORD", "envpwlive")
    auth_mod._load_dotenv_once()

    test_db = tmp_path / "envwins.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(jwt_auth, "_SECRET_PATH", tmp_path / "envwins.key")

    rec = auth.bootstrap_admin_from_env()
    assert rec is not None
    assert rec.username == "fromenv"
    # File's password wasn't used
    assert auth.verify_user("fromfile", "filepw1") is None


def test_load_dotenv_handles_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If .env doesn't exist, loader is a silent no-op."""
    import src.stock_screener.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_PROJECT_ROOT", tmp_path)  # no .env here
    monkeypatch.delenv("TSE_ADMIN_USER", raising=False)
    auth_mod._load_dotenv_once()  # must not raise
    assert os.getenv("TSE_ADMIN_USER") is None
