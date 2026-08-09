import os
from dataclasses import dataclass
from typing import Dict

from dotenv import load_dotenv


@dataclass(frozen=True)
class SessionConfig:
    name: str
    ref_hour: int
    start_hour: int
    end_hour: int
    target_points: float
    stop_points: float


@dataclass(frozen=True)
class Settings:
    tt_username: str
    tt_password: str
    telegram_token: str
    telegram_chat_id: str
    symbol_base: str
    sessions: Dict[int, SessionConfig]
    entry_delay_minutes: int = 5
    max_idle_seconds: int = 300
    timezone: str = "America/Los_Angeles"


SESSIONS: Dict[int, SessionConfig] = {
    22: SessionConfig(
        name="London",
        ref_hour=22,
        start_hour=23,
        end_hour=23,
        target_points=0.2,
        stop_points=0.5,
    ),
}


def load_settings() -> Settings:
    """Load settings from environment variables and built-in session definitions."""
    load_dotenv()

    return Settings(
        tt_username=os.environ["TT_USERNAME"],
        tt_password=os.environ["TT_PASSWORD"],
        telegram_token=os.environ["TELEGRAM_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
        symbol_base=os.environ.get("SYMBOL_BASE", "/MES"),
        sessions=SESSIONS,
    )
