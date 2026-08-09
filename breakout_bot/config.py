import json
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


# Default session set, used when SESSIONS_JSON is not set. Session keys are
# ids (commonly the reference hour); each value needs: name, ref_hour,
# start_hour, end_hour, target_points, stop_points.
_DEFAULT_SESSIONS_JSON = json.dumps({
    "22": {
        "name": "London",
        "ref_hour": 22,
        "start_hour": 23,
        "end_hour": 23,
        "target_points": 0.2,
        "stop_points": 0.5,
    },
})

_SESSION_FIELDS = (
    "name", "ref_hour", "start_hour", "end_hour", "target_points", "stop_points",
)


def _load_sessions() -> Dict[int, SessionConfig]:
    """Parse SESSIONS_JSON into session configs. Falls back to a single
    default session (any futures contract, ref hour 22:00) if unset, so the
    bot runs out of the box without extra configuration.

    Example SESSIONS_JSON for multiple sessions on the same contract:
      {"22": {"name": "London", "ref_hour": 22, "start_hour": 23,
               "end_hour": 23, "target_points": 0.2, "stop_points": 0.5},
       "4":  {"name": "Late NY", "ref_hour": 4, "start_hour": 5,
               "end_hour": 13, "target_points": 1, "stop_points": 2}}
    """
    raw = os.environ.get("SESSIONS_JSON", _DEFAULT_SESSIONS_JSON)
    try:
        parsed = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Invalid SESSIONS_JSON: {e}")

    sessions: Dict[int, SessionConfig] = {}
    for key, cfg in parsed.items():
        try:
            session_id = int(key)
        except Exception:
            raise RuntimeError(f"Session keys must be integers, got: {key!r}")
        missing = [f for f in _SESSION_FIELDS if f not in cfg]
        if missing:
            raise RuntimeError(
                f"Session {key!r} missing required field(s): {missing}"
            )
        sessions[session_id] = SessionConfig(
            name=cfg["name"],
            ref_hour=int(cfg["ref_hour"]),
            start_hour=int(cfg["start_hour"]),
            end_hour=int(cfg["end_hour"]),
            target_points=float(cfg["target_points"]),
            stop_points=float(cfg["stop_points"]),
        )
    return sessions


def load_settings() -> Settings:
    """Load settings from environment variables."""
    load_dotenv()

    return Settings(
        tt_username=os.environ["TT_USERNAME"],
        tt_password=os.environ["TT_PASSWORD"],
        telegram_token=os.environ["TELEGRAM_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
        symbol_base=os.environ.get("SYMBOL_BASE", "/MES"),
        sessions=_load_sessions(),
    )
