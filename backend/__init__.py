"""FastAPI backend for the Tokyo Stock Exchange screener.

The old ``app.py`` source is retained only as a migration reference and is
not loaded by this package. The backend exposes a versioned JSON API for the
web client.
"""

from __future__ import annotations

from typing import Any

__all__ = ["app", "create_app", "health"]


def __getattr__(name: str) -> Any:
    """Load the ASGI app lazily so helper modules remain independently usable."""

    if name in __all__:
        from .main import app, create_app, health

        return {"app": app, "create_app": create_app, "health": health}[name]
    raise AttributeError(name)
