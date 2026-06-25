"""
SQLite database layer — single file at data/screener.db.

Holds users (bcrypt-hashed credentials) and per-user data (settings,
target rows, watchlist). Server-side so all clients hitting the same
Streamlit deployment see the same state.

Schema is created on first import via `init_db()`.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Project root: src/stock_screener/db.py -> ../../
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "screener.db"

# Single-process: Streamlit runs one Python process per session.
# A re-entrant lock keeps concurrent threads (Streamlit script runner
# + background scan thread) from corrupting writes.
_db_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL,
    PRIMARY KEY (user_id, key),
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
"""


def init_db(db_path: Path | None = None) -> Path:
    """Create data/ dir and apply schema. Idempotent. Returns DB path."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()
    return path


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context-managed connection with row_factory + foreign keys enabled.

    Usage:
        with connect() as conn:
            conn.execute(...)
    """
    path = db_path or DB_PATH
    # Ensure schema exists (cheap; uses CREATE TABLE IF NOT EXISTS)
    if not path.exists():
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
