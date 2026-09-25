"""
Alert Module — scan tickers and send signals via Telegram.

Usage:
    python alert.py                    # run once
    python alert.py --daemon           # run daily at 15:30 JST
    python alert.py --tickers 7203 6758 9984

Requires environment variables:
    TELEGRAM_BOT_TOKEN  — Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID    — Target chat/group ID
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
from collections.abc import Callable
from datetime import timedelta
from urllib.parse import urlsplit

import requests

from .data_loader import YFinanceDataLoader
from .technical_engine import (
    BaseStrategy,
    OverboughtReversalSellStrategy,
    PullbackMAStrategy,
    Signal,
    SignalType,
    TechnicalEngine,
    TrendBreakdownSellStrategy,
    VolumeBreakoutStrategy,
)
from .watchlist import DEFAULT_TICKERS

logger = logging.getLogger(__name__)

try:
    _ALERT_CONCURRENCY = max(1, min(int(os.getenv("TSE_ALERT_MAX_CONCURRENCY", "2")), 16))
except (TypeError, ValueError):
    _ALERT_CONCURRENCY = 2
_ALERT_SEMAPHORE = threading.BoundedSemaphore(_ALERT_CONCURRENCY)


# ---------------------------------------------------------------------------
# Telegram sender
# ---------------------------------------------------------------------------

class TelegramSender:
    """Send messages via Telegram Bot API."""

    def __init__(self, bot_token: str | None = None, chat_id: str | None = None) -> None:
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")

        if not self.bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN not set — messages will only be logged")
        if not self.chat_id:
            logger.warning("TELEGRAM_CHAT_ID not set — messages will only be logged")

    def send(self, text: str) -> bool:
        """Send a message to the configured chat.

        Args:
            text: Message text (supports HTML formatting).

        Returns:
            True if sent successfully, False otherwise.
        """
        if not self.bot_token or not self.chat_id:
            logger.info("Telegram not configured; message was not delivered")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            if not _ALERT_SEMAPHORE.acquire(timeout=10):
                return False
            try:
                resp = requests.post(url, json=payload, timeout=10, allow_redirects=False)
            finally:
                _ALERT_SEMAPHORE.release()
            resp.raise_for_status()
            logger.info("Telegram message sent")
            return True
        except requests.RequestException as exc:
            logger.warning(
                "Telegram delivery failed (%s)",
                type(exc).__name__,
            )
            return False
        except Exception as exc:
            logger.warning("Telegram delivery failed (%s)", type(exc).__name__)
            return False

    def _send_safely(self, text: str) -> bool:
        """Compatibility hook for callers that prefer a single send method."""

        return self.send(text)


# ---------------------------------------------------------------------------
# Slack sender
# ---------------------------------------------------------------------------

class SlackSender:
    """Send messages via Slack Webhook (Incoming Webhook)."""

    def __init__(self, webhook_url: str | None = None) -> None:
        configured = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")
        self.webhook_url = self._validate_webhook_url(configured)
        if not self.webhook_url:
            logger.warning("SLACK_WEBHOOK_URL is missing or invalid — delivery is disabled")

    @staticmethod
    def _validate_webhook_url(value: str) -> str:
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or not (
                hostname == "hooks.slack.com"
                or hostname.endswith(".slack.com")
                or hostname == "hooks.slack-gov.com"
                or hostname.endswith(".slack-gov.com")
            )
        ):
            return ""
        return value

    @staticmethod
    def _html_to_mrkdwn(text: str) -> str:
        """Convert Telegram HTML formatting to Slack mrkdwn.

        - <b>text</b>  → *text*
        - <code>text</code>  → `text`
        - strip all other tags
        """
        import re
        text = re.sub(r"<b>(.*?)</b>", r"*\1*", text)
        text = re.sub(r"<code>(.*?)</code>", r"`\1`", text)
        text = re.sub(r"<[^>]+>", "", text)
        return text

    def send(self, text: str) -> bool:
        """Send a message to the configured Slack channel.

        Args:
            text: Message text (HTML formatting auto-converted to Slack mrkdwn).

        Returns:
            True if sent successfully, False otherwise.
        """
        if not self.webhook_url:
            logger.info("Slack not configured; message was not delivered")
            return False

        try:
            payload = {"text": self._html_to_mrkdwn(text)}
            if not _ALERT_SEMAPHORE.acquire(timeout=10):
                return False
            try:
                resp = requests.post(
                    self.webhook_url,
                    json=payload,
                    timeout=10,
                    allow_redirects=False,
                )
            finally:
                _ALERT_SEMAPHORE.release()
            resp.raise_for_status()
            logger.info("Slack message sent")
            return True
        except requests.RequestException as exc:
            logger.warning(
                "Slack delivery failed (%s)",
                type(exc).__name__,
            )
            return False
        except Exception as exc:
            logger.warning("Slack delivery failed (%s)", type(exc).__name__)
            return False


# ---------------------------------------------------------------------------
# Alert formatter
# ---------------------------------------------------------------------------

def format_signal_alert(signals: list[Signal], scan_date: str) -> str:
    """Format a list of signals into a Telegram-friendly HTML message.

    Args:
        signals: List of Signal objects.
        scan_date: Date string of the scan.

    Returns:
        Formatted HTML string.
    """
    if not signals:
        return f"📊 <b>{scan_date}</b> — Không có tín hiệu mới."

    lines = [f"📊 <b>Tín hiệu giao dịch — {scan_date}</b>", ""]

    buy_signals = [s for s in signals if s.signal_type == SignalType.BUY]
    sell_signals = [s for s in signals if s.signal_type == SignalType.SELL]

    if buy_signals:
        lines.append("🟢 <b>MUA:</b>")
        for s in buy_signals:
            sl_str = f" | SL: ¥{s.stop_loss:,.0f}" if s.stop_loss else ""
            lines.append(
                f"  • <code>{s.ticker}</code> @ ¥{s.price:,.0f} ({s.strategy}){sl_str}"
            )
        lines.append("")

    if sell_signals:
        lines.append("🔴 <b>BÁN:</b>")
        for s in sell_signals:
            lines.append(f"  • <code>{s.ticker}</code> @ ¥{s.price:,.0f} ({s.strategy})")
        lines.append("")

    lines.append(f"📈 Tổng: {len(signals)} tín hiệu")
    return "\n".join(lines)


def format_summary_report(
    results: dict[str, list[Signal]],
    scan_date: str,
) -> str:
    """Format a summary report for all scanned tickers.

    Args:
        results: Dict mapping ticker -> list of signals.
        scan_date: Scan date string.

    Returns:
        Formatted HTML string.
    """
    total = sum(len(v) for v in results.values())
    tickers_with_signals = [t for t, v in results.items() if v]

    lines = [
        f"📋 <b>Báo cáo Scan — {scan_date}</b>",
        "",
        f"Đã scan: {len(results)} mã",
        f"Tín hiệu: {total}",
        "",
    ]

    if tickers_with_signals:
        lines.append("Có tín hiệu:")
        for t in tickers_with_signals:
            sigs = results[t]
            for s in sigs:
                sl = f" SL={s.stop_loss:,.0f}" if s.stop_loss else ""
                lines.append(f"  • <code>{t}</code> {s.signal_type.value} ¥{s.price:,.0f} ({s.strategy}){sl}")
    else:
        lines.append("Không có tín hiệu nào.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class AlertScanner:
    """Scan a list of tickers and send alerts via Telegram & Slack."""

    def __init__(
        self,
        tickers: list[str] | None = None,
        lookback_days: int = 365,
        strategies: list[BaseStrategy] | None = None,
        telegram_sender: TelegramSender | None = None,
        slack_sender: SlackSender | None = None,
    ) -> None:
        """Initialize scanner.

        Args:
            tickers: List of raw tickers. Defaults to DEFAULT_TICKERS.
            lookback_days: Days of historical data to fetch.
            strategies: List of strategies to run. Defaults to both built-in strategies.
            telegram_sender: TelegramSender instance.
            slack_sender: SlackSender instance.
        """
        self.tickers = tickers or DEFAULT_TICKERS
        self.lookback_days = lookback_days
        self.strategies = strategies or [
            VolumeBreakoutStrategy(),
            PullbackMAStrategy(),
            TrendBreakdownSellStrategy(),
            OverboughtReversalSellStrategy(),
        ]
        self.telegram_sender = telegram_sender or TelegramSender()
        self.slack_sender = slack_sender or SlackSender()
        self.last_delivery: dict[str, bool] = {"telegram": False, "slack": False}
        self.scan_errors: dict[str, str] = {}
        self.loader = YFinanceDataLoader()
        self.engine = TechnicalEngine()

    def _scan_one(self, ticker: str, start: str, end: str) -> tuple[str, list[Signal]]:
        """Scan a single ticker (used by parallel scan)."""
        try:
            df = self.loader.fetch_ohlcv(ticker, start, end)
            df = self.engine.enrich(df)
            signals: list[Signal] = []
            for strat in self.strategies:
                signals.extend(strat.generate_signals(df, ticker))
            return ticker, signals
        except Exception:
            self.scan_errors[ticker] = "data unavailable"
            logger.exception("Scan failed for %s", ticker)
            return ticker, []

    def scan(self) -> dict[str, list[Signal]]:
        """Scan all tickers and return signals (parallel).

        Returns:
            Dict mapping ticker -> list of signals.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        self.scan_errors = {}

        # JST market dates — UTC server would pick the wrong trading day
        from .data_loader import jst_now

        now = jst_now()
        end = now.strftime("%Y-%m-%d")
        start = (now - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")
        results: dict[str, list[Signal]] = {}

        with ThreadPoolExecutor(max_workers=min(len(self.tickers), 8)) as pool:
            futures = {pool.submit(self._scan_one, t, start, end): t for t in self.tickers}
            for future in as_completed(futures):
                ticker, signals = future.result()
                results[ticker] = signals

        return results

    def deliver_results(
        self,
        results: dict[str, list[Signal]],
        stop_event: threading.Event | None = None,
        can_deliver: Callable[[], bool] | None = None,
    ) -> bool:
        """Deliver an already-computed scan, with an optional stop gate."""

        from .data_loader import jst_now

        if self.tickers and len(self.scan_errors) == len(self.tickers):
            logger.warning("Skipping alert delivery because every ticker failed")
            return False
        if stop_event is not None and stop_event.is_set():
            logger.info("Scheduled alert delivery cancelled before dispatch")
            return False
        if can_deliver is not None and not can_deliver():
            logger.info("Alert delivery cancelled because capability was revoked")
            return False

        scan_date = jst_now().strftime("%Y-%m-%d %H:%M")
        summary = format_summary_report(results, scan_date)
        self.last_delivery = {"telegram": False, "slack": False}
        if (stop_event is None or not stop_event.is_set()) and (
            can_deliver is None or can_deliver()
        ):
            self.last_delivery["telegram"] = bool(self.telegram_sender.send(summary))
        if (stop_event is None or not stop_event.is_set()) and (
            can_deliver is None or can_deliver()
        ):
            self.last_delivery["slack"] = bool(self.slack_sender.send(summary))
        if not any(self.last_delivery.values()):
            logger.warning(
                "No alert channels delivered the report (%d signals, %d tickers) — "
                "check TELEGRAM/SLACK env config",
                sum(len(v) for v in results.values()),
                len(self.tickers),
            )
        return any(self.last_delivery.values())

    def scan_and_alert(self) -> dict[str, list[Signal]]:
        """Scan all tickers, format results, and send via Telegram & Slack."""

        results = self.scan()
        self.deliver_results(results)
        return results


# ---------------------------------------------------------------------------
# Daemon scheduler
# ---------------------------------------------------------------------------

def run_daemon(tickers: list[str], lookback: int = 365) -> None:
    """Run scanner on a daily schedule (15:30 JST after market close).

    Args:
        tickers: List of tickers to scan.
        lookback: Days of historical data.
    """
    try:
        import time

        import schedule
    except ImportError:
        logger.error("Install 'schedule': pip install schedule")
        return

    scanner = AlertScanner(tickers=tickers, lookback_days=lookback)

    schedule.every().day.at("15:30", "Asia/Tokyo").do(scanner.scan_and_alert)
    logger.info("Daemon started. Scanning at 15:30 JST daily.")
    logger.info("Watching: %s", ", ".join(tickers))

    while True:
        try:
            schedule.run_pending()
        except Exception:
            # A failing job must never kill the daemon — log and keep polling
            # so the next scheduled run still fires.
            logger.exception("Scheduled scan failed; daemon continues")
        time.sleep(60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="TSE Signal Alert Scanner")
    parser.add_argument("--daemon", action="store_true", help="Run daily at 15:30 JST")
    parser.add_argument("--tickers", nargs="*", default=DEFAULT_TICKERS, help="Tickers to scan")
    parser.add_argument("--lookback", type=int, default=365, help="Days of history")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.daemon:
        run_daemon(args.tickers, args.lookback)
    else:
        scanner = AlertScanner(tickers=args.tickers, lookback_days=args.lookback)
        results = scanner.scan_and_alert()
        from .data_loader import jst_now

        scan_date = jst_now().strftime("%Y-%m-%d %H:%M")
        summary = format_summary_report(results, scan_date)
        print(summary)


if __name__ == "__main__":
    main()
