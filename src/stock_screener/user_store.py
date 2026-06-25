"""
Per-user data store — server-side replacement for browser localStorage.

Reads/writes are scoped to a user_id and persist in SQLite. Every device
hitting the same Streamlit server sees the same data (true cross-device
sync, not per-browser localStorage).

Public surface:
  - get_setting(user_id, key, default) -> str
  - set_setting(user_id, key, value)
  - get_target_rows(user_id) -> list[dict]
  - save_target_rows(user_id, rows)
  - get_watchlist(user_id) -> list[str]
  - add_to_watchlist / remove_from_watchlist
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from . import db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Generic settings (key/value JSON strings)
# ---------------------------------------------------------------------------

def get_setting(user_id: int, key: str, default: Any = None) -> Any:
    """Read a JSON-encoded setting. Returns `default` on miss."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
            (user_id, key),
        ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return default


def set_setting(user_id: int, key: str, value: Any) -> None:
    """Write a JSON-encoded setting (upsert)."""
    payload = json.dumps(value, ensure_ascii=False)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, payload),
        )


# ---------------------------------------------------------------------------
# Target rows (structured, ordered)
# ---------------------------------------------------------------------------

def get_target_rows(user_id: int) -> list[dict]:
    """Return the user's target rows, ordered by position."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT ticker, entry_price, target_pct, shares "
            "FROM target_rows WHERE user_id = ? ORDER BY position ASC, id ASC",
            (user_id,),
        ).fetchall()
    return [
        {
            "ticker": str(r["ticker"]),
            "entry_price": float(r["entry_price"]),
            "target_pct": float(r["target_pct"]),
            "shares": int(r["shares"]),
        }
        for r in rows
    ]


def save_target_rows(user_id: int, rows: list[dict]) -> None:
    """Replace the user's full row set. Atomic: delete + insert in one txn."""
    payload = []
    for i, r in enumerate(rows):
        payload.append((
            user_id,
            i,
            str(r.get("ticker", "")).strip().upper(),
            float(r.get("entry_price", 0) or 0),
            float(r.get("target_pct", 0) or 0),
            int(r.get("shares", 0) or 0),
        ))
    with db.connect() as conn:
        conn.execute("DELETE FROM target_rows WHERE user_id = ?", (user_id,))
        if payload:
            conn.executemany(
                "INSERT INTO target_rows "
                "(user_id, position, ticker, entry_price, target_pct, shares) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                payload,
            )


# ---------------------------------------------------------------------------
# Watchlist (simple ticker set)
# ---------------------------------------------------------------------------

def get_watchlist(user_id: int) -> list[str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY added_at ASC",
            (user_id,),
        ).fetchall()
    return [str(r["ticker"]) for r in rows]


def add_to_watchlist(user_id: int, ticker: str) -> None:
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (user_id, ticker, added_at) "
            "VALUES (?, ?, ?)",
            (user_id, ticker, now),
        )


def remove_from_watchlist(user_id: int, ticker: str) -> None:
    ticker = (ticker or "").strip().upper()
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?",
            (user_id, ticker),
        )
