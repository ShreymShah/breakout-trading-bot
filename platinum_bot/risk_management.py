from datetime import datetime
from typing import Dict, Optional

from platinum_bot.config import SessionConfig
from platinum_bot.state import SessionTradeState


class RiskManager:
    """Trade eligibility and session window checks."""

    @staticmethod
    def is_in_session_window(cfg: SessionConfig, current_hour: int) -> bool:
        return cfg.start_hour <= current_hour < cfg.end_hour

    @staticmethod
    def is_trade_eligible(
        session_id: int,
        trades_taken: Dict[int, SessionTradeState],
        now: datetime,
        eligible_time: Optional[datetime],
    ) -> bool:
        session_trades = trades_taken.get(session_id)
        if session_trades is None or session_trades.count >= 2:
            return False
        if eligible_time is None or now < eligible_time:
            return False
        return True

    @staticmethod
    def can_take_direction(
        session_id: int,
        direction: str,
        trades_taken: Dict[int, SessionTradeState],
    ) -> bool:
        session_trades = trades_taken.get(session_id)
        if session_trades is None:
            return False
        taken_dirs = session_trades.directions
        if len(taken_dirs) == 0:
            return True
        if len(taken_dirs) == 1 and direction != taken_dirs[0]:
            return True
        return False
