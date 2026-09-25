"""
SQLite database layer — single file at data/screener.db.

Holds users (bcrypt-hashed credentials) and per-user data (settings,
target rows, watchlist). Server-side so all clients hitting the same
FastAPI deployment see the same state.

Schema is created on first import via `init_db()`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Project root: src/stock_screener/db.py -> ../../
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Load .env BEFORE reading env vars below. auth.py also loads it, but this
# module may be imported first — without the local load, TSE_DATA_DIR from
# .env would be invisible and state would land in repo root.
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass

# Allow overriding data directory and database path via environment variables
env_data_dir = os.environ.get("TSE_DATA_DIR")
if env_data_dir:
    DATA_DIR = Path(env_data_dir).resolve()
else:
    DATA_DIR = PROJECT_ROOT / "data"

env_db_path = os.environ.get("TSE_DB_PATH")
if env_db_path:
    DB_PATH = Path(env_db_path).resolve()
else:
    DB_PATH = DATA_DIR / "screener.db"

# Single-worker API deployment: a re-entrant lock keeps concurrent request
# threads from corrupting writes while the service is kept on one worker.
_db_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    token_version INTEGER NOT NULL DEFAULT 0,
    can_send_alerts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL,
    PRIMARY KEY (user_id, key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS auto_scan_state (
    user_id     INTEGER PRIMARY KEY,
    next_run_at REAL NOT NULL,
    last_run_at REAL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS target_rows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    position   INTEGER NOT NULL,
    ticker     TEXT    NOT NULL,
    entry_price REAL   NOT NULL,
    target_pct REAL    NOT NULL,
    shares     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_target_rows_user
    ON target_rows(user_id, position);

CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL,
    ticker  TEXT    NOT NULL,
    added_at TEXT   NOT NULL,
    PRIMARY KEY (user_id, ticker),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT    NOT NULL,
    ip           TEXT    NOT NULL,
    success      INTEGER NOT NULL DEFAULT 0,
    attempted_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_lookup
    ON login_attempts(username, ip, attempted_at);

CREATE INDEX IF NOT EXISTS idx_login_attempts_account
    ON login_attempts(username, attempted_at);
"""


def init_db(db_path: Path | None = None) -> Path:
    """Create data/ dir and apply schema. Idempotent. Returns DB path."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    # Serialize schema+migration work — two threads racing the very first
    # run would both ALTER TABLE and crash on "duplicate column name".
    with _db_lock:
        with sqlite3.connect(path, timeout=10.0) as conn:
            conn.executescript(_SCHEMA)
            _migrate(conn)
            conn.commit()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply incremental migrations to pre-existing databases.

    New installs get the full schema from `_SCHEMA`; old installs only
    receive the columns/tables added after their creation.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "token_version" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"
        )
    if "can_send_alerts" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN can_send_alerts INTEGER NOT NULL DEFAULT 0"
        )


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context-managed connection with row_factory + foreign keys enabled.

    Usage:
        with connect() as conn:
            conn.execute(...)
    """
    path = db_path or DB_PATH
    # Ensure schema + migrations exist. Idempotent and cheap:
    # executescript uses CREATE TABLE IF NOT EXISTS and _migrate() is a
    # single PRAGMA check + optional ALTER.
    init_db(path)
    with _db_lock:
        conn = sqlite3.connect(path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def user_count() -> int:
    """Number of registered users. Used to decide first-run setup."""
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"])
