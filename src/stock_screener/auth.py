"""
Authentication — bcrypt-hashed credentials stored in SQLite.

Single-tenant by design (the user requested one pre-created account).
Create the first user via CLI:

    python -m src.stock_screener.auth create-user <username> <password>

Or from inside Python:

    from src.stock_screener.auth import create_user, verify_user
    create_user("admin", "my-secret-pw")
    assert verify_user("admin", "my-secret-pw")
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import bcrypt

from . import db

# Load .env from project root so `python -m src.stock_screener.auth ...`
# and `streamlit run app.py` both see the same env vars. override=False
# so real process env always wins.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_dotenv_once() -> None:
    """Read .env from project root. No-op if python-dotenv missing."""
    try:
        from dotenv import load_dotenv

        load_dotenv(_PROJECT_ROOT / ".env", override=False)
    except ImportError:
        pass


_load_dotenv_once()

logger = logging.getLogger(__name__)

_MIN_PW_LEN = 8


@dataclass
class UserRecord:
    id: int
    username: str


def hash_password(plain: str) -> str:
    """Return a bcrypt hash as a UTF-8 string (safe to store in TEXT)."""
    if not plain or len(plain) < _MIN_PW_LEN:
        raise ValueError(f"Password must be at least {_MIN_PW_LEN} characters")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time bcrypt comparison. Returns False on any decode error."""
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_user(username: str, password: str) -> UserRecord:
    """Create a new user. Raises ValueError on duplicate username or weak pw."""
    username = (username or "").strip()
    if not username:
        raise ValueError("Username must be non-empty")
    if len(password or "") < _MIN_PW_LEN:
        raise ValueError(f"Password must be at least {_MIN_PW_LEN} characters")

    pw_hash = hash_password(password)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    try:
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES (?, ?, ?)",
                (username, pw_hash, now),
            )
            uid = int(cur.lastrowid)
    except Exception as e:
        if "UNIQUE" in str(e):
            raise ValueError(f"Username '{username}' already exists") from e
        raise
    logger.debug("Created user '%s' (id=%d)", username, uid)
    return UserRecord(id=uid, username=username)


def get_by_username(username: str) -> UserRecord | None:
    """Fetch a user record by username. Returns None if not found."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, username FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        return None
    return UserRecord(id=int(row["id"]), username=str(row["username"]))


def verify_user(username: str, password: str) -> UserRecord | None:
    """Verify credentials. Returns the UserRecord on success, None on failure."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        # Constant-time-ish: still hash a dummy to avoid timing leak on user enumeration
        verify_password(password, "$2b$12$" + "x" * 53)
        return None
    if not verify_password(password, str(row["password_hash"])):
        return None
    return UserRecord(id=int(row["id"]), username=str(row["username"]))


def change_password(user_id: int, new_password: str) -> None:
    """Update a user's password hash."""
    new_hash = hash_password(new_password)
    with db.connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hash, user_id),
        )


# ---------------------------------------------------------------------------
# ENV-based bootstrap
# ---------------------------------------------------------------------------

def bootstrap_admin_from_env(env: dict[str, str] | None = None) -> UserRecord | None:
    """Create the initial admin from environment variables.

    Reads TSE_ADMIN_USER and TSE_ADMIN_PASSWORD. Only fires when:
      - the users table is empty, AND
      - both variables are set and non-empty, AND
      - the password meets the minimum length.

    Subsequent starts are no-ops (the user already exists). The DB file is
    created on first call via `db.init_db()`.

    Returns the created UserRecord, or None if no bootstrap occurred.

    This avoids committing a pre-populated SQLite file to source control —
    credentials travel via .env (gitignored) instead.
    """
    db.init_db()  # idempotent; creates the file if missing
    if db.user_count() > 0:
        return None

    src = env if env is not None else {
        "TSE_ADMIN_USER": os.getenv("TSE_ADMIN_USER", ""),
        "TSE_ADMIN_PASSWORD": os.getenv("TSE_ADMIN_PASSWORD", ""),
    }
    username = (src.get("TSE_ADMIN_USER") or "").strip()
    password = src.get("TSE_ADMIN_PASSWORD") or ""

    if not username or not password:
        logger.debug("Bootstrap skipped: TSE_ADMIN_USER / TSE_ADMIN_PASSWORD not set")
        return None

    if len(password) < _MIN_PW_LEN:
        logger.warning(
            "Bootstrap skipped: TSE_ADMIN_PASSWORD must be at least %d characters",
            _MIN_PW_LEN,
        )
        return None

    try:
        rec = create_user(username, password)
        logger.info("Bootstrapped admin user '%s' from environment", username)
        return rec
    except ValueError as e:
        logger.warning("Bootstrap failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="User account management")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create-user", help="Create a new user")
    p_create.add_argument("username")
    p_create.add_argument("password")

    p_passwd = sub.add_parser("change-password", help="Change a user's password")
    p_passwd.add_argument("username")
    p_passwd.add_argument("password")

    args = parser.parse_args(argv)
    db.init_db()

    if args.cmd == "create-user":
        try:
            user = create_user(args.username, args.password)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"Created user '{user.username}' (id={user.id})")
        return 0

    if args.cmd == "change-password":
        user = get_by_username(args.username)
        if user is None:
            print(f"Error: user '{args.username}' not found", file=sys.stderr)
            return 1
        change_password(user.id, args.password)
        print(f"Password updated for '{user.username}'")
        return 0

    return 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(_cli(sys.argv[1:]))


if __name__ == "__main__":
    main()
