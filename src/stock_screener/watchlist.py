"""
Shared watchlist definitions — single source of truth for ticker lists.

Both the Streamlit dashboard (app.py) and the alert scanner (alert.py)
import from here instead of re-declaring their own copies, so the
watchlist can be edited in one place.
"""

USER_TICKERS = ["6232", "6227", "5801", "7974", "4661", "8001", "9433", "2962", "584A", "6327"]
AI_TICKERS = ["9984", "5803", "6857", "8035", "5016", "285A", "7735"]
DEFAULT_TICKERS = USER_TICKERS + AI_TICKERS
