import logging

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends messages to a Telegram chat via the Bot API."""

    def __init__(self, token: str, chat_id: str):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    def send(self, message: str) -> None:
        data = {"chat_id": self._chat_id, "text": message, "parse_mode": "Markdown"}
        try:
            response = requests.post(self._url, json=data, timeout=5)
            if response.status_code != 200:
                logger.warning("Telegram send failed: %d", response.status_code)
        except Exception as e:
            logger.warning("Telegram error: %s", e)
