import logging
import threading
import time
from typing import Dict

import requests

logger = logging.getLogger(__name__)

# Minimum seconds between any two Telegram sends.
MIN_INTERVAL_SECONDS = 1.0
# Suppress identical messages seen again within this many seconds.
DEDUP_SECONDS = 120


class TelegramNotifier:
    """Sends messages to a Telegram chat via the Bot API.

    The bot calls `send()` synchronously from the asyncio event loop (candle
    processing), so the actual HTTP POST runs on a daemon thread to avoid
    stalling the stream on a slow or rate-limited Telegram response. Also
    throttles the send rate, suppresses duplicate messages, and honors
    Telegram's 429 retry_after by dropping (never sleeping) sends during an
    active backoff window.
    """

    def __init__(self, token: str, chat_id: str):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._last_sent = 0.0
        self._blocked_until = 0.0
        self._recent: Dict[str, float] = {}

    def send(self, message: str, force: bool = False) -> None:
        """Queues `message` for delivery on a background thread.

        `force=True` bypasses the dedup/min-interval throttle for critical
        alerts, but still respects an active 429 backoff window (sending
        into an active rate limit would only make it worse).
        """
        now = time.monotonic()

        if now < self._blocked_until:
            return

        if self._recent:
            cutoff = now - DEDUP_SECONDS
            self._recent = {m: t for m, t in self._recent.items() if t >= cutoff}

        if not force:
            last = self._recent.get(message)
            if last is not None and (now - last) < DEDUP_SECONDS:
                return
            if (now - self._last_sent) < MIN_INTERVAL_SECONDS:
                return

        self._last_sent = now
        self._recent[message] = now
        threading.Thread(target=self._post, args=(message,), daemon=True).start()

    def _post(self, message: str) -> None:
        """Blocking HTTP POST, run off the event loop thread."""
        data = {"chat_id": self._chat_id, "text": message, "parse_mode": "Markdown"}
        try:
            response = requests.post(self._url, json=data, timeout=5)
            if response.status_code == 429:
                try:
                    retry_after = int(
                        response.json().get("parameters", {}).get("retry_after", 30)
                    )
                except Exception:
                    retry_after = 30
                self._blocked_until = time.monotonic() + retry_after
                logger.warning("Telegram 429 - backing off %ds", retry_after)
            elif response.status_code != 200:
                logger.warning(
                    "Telegram send failed: %d %s",
                    response.status_code, response.text[:200],
                )
        except Exception as e:
            logger.warning("Telegram error: %s", e)
