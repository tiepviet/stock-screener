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
from datetime import datetime, timedelta

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

logger = logging.getLogger(__name__)

# Default TSE watchlist (Blue chips)
DEFAULT_TICKERS = [
    "7203",  # Toyota
    "6758",  # Sony
    "9984",  # SoftBank Group
    "8306",  # Mitsubishi UFJ
    "6501",  # Hitachi
    "7267",  # Honda
    "9434",  # SoftBank Corp
    "6861",  # Keyence
    "8411",  # Mizuho
    "7751",  # Canon
    "6752",  # Panasonic
    "7974",  # Nintendo
    "6178",  # Japan Post
    "8316",  # Sumitomo Mitsui
    "9831",  # Yamato Holdings
]


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
            logger.info("Telegram not configured. Message:\n%s", text)
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("Telegram message sent")
            return True
        except Exception:
            logger.exception("Failed to send Telegram message")
            return False


# ---------------------------------------------------------------------------
# Slack sender
# ---------------------------------------------------------------------------

class SlackSender:
    """Send messages via Slack Webhook (Incoming Webhook)."""

    def __init__(self, webhook_url: str | None = None) -> None:
        self.webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")
        if not self.webhook_url:
            logger.warning("SLACK_WEBHOOK_URL not set — messages will only be logged")

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
            logger.info("Slack not configured. Message:\n%s", text)
            return False

        try:
            payload = {"text": self._html_to_mrkdwn(text)}
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("Slack message sent")
            return True
        except Exception:
            logger.exception("Failed to send Slack message")
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
        self.loader = YFinanceDataLoader()
        self.engine = TechnicalEngine()

    def scan(self) -> dict[str, list[Signal]]:
        """Scan all tickers and return signals.

        Returns:
            Dict mapping ticker -> list of signals.
        """
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")
        results: dict[str, list[Signal]] = {}

        for ticker in self.tickers:
            try:
                df = self.loader.fetch_ohlcv(ticker, start, end)
                df = self.engine.enrich(df)
                signals: list[Signal] = []
                for strat in self.strategies:
                    signals.extend(strat.generate_signals(df, ticker))
                results[ticker] = signals
            except Exception:
                logger.exception("Scan failed for %s", ticker)
                results[ticker] = []

        return results

    def scan_and_alert(self) -> dict[str, list[Signal]]:
        """Scan all tickers, format results, and send via Telegram & Slack.

        Returns:
            Dict mapping ticker -> list of signals.
        """
        results = self.scan()
        scan_date = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Send combined report (summary includes all signals)
        summary = format_summary_report(results, scan_date)
        self.telegram_sender.send(summary)
        self.slack_sender.send(summary)

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

    schedule.every().day.at("15:30").do(scanner.scan_and_alert)
    logger.info("Daemon started. Scanning at 15:30 JST daily.")
    logger.info("Watching: %s", ", ".join(tickers))

    while True:
        schedule.run_pending()
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
        scan_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        summary = format_summary_report(results, scan_date)
        print(summary)


if __name__ == "__main__":
    main()
