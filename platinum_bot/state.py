import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ActiveTrade:
    side: str
    tp: Decimal
    sl: Decimal
    sess_name: str
    cutoff_h: int
    sess_id: int
    entry_price: Decimal


@dataclass
class SessionTradeState:
    count: int = 0
    directions: List[str] = field(default_factory=list)


@dataclass
class BotState:
    last_reset_date: Optional[str] = None
    trades_taken: Dict[int, SessionTradeState] = field(default_factory=dict)
    active_trades: List[ActiveTrade] = field(default_factory=list)
    ref_levels: Dict[int, Optional[Dict[str, Decimal]]] = field(default_factory=dict)
    session_trade_eligible_time: Dict[int, Optional[datetime]] = field(
        default_factory=dict
    )
    fetch_attempted: Dict[int, bool] = field(default_factory=dict)
    scanning_started: Dict[int, bool] = field(default_factory=dict)
    reconnect_count: int = 0


class StateManager:
    """Persists BotState to a JSON file."""

    def __init__(self, filepath: str = "bot_state.json"):
        self._filepath = filepath

    def save(self, state: BotState) -> None:
        try:
            data = {
                "last_reset_date": state.last_reset_date,
                "trades_taken": {
                    str(k): {"count": v.count, "directions": v.directions}
                    for k, v in state.trades_taken.items()
                },
                "active_trades": [
                    {
                        "side": t.side,
                        "tp": str(t.tp),
                        "sl": str(t.sl),
                        "sess_name": t.sess_name,
                        "cutoff_h": t.cutoff_h,
                        "sess_id": t.sess_id,
                        "entry_price": str(t.entry_price),
                    }
                    for t in state.active_trades
                ],
                "ref_levels": {
                    str(k): {pk: str(pv) for pk, pv in v.items()} if v else None
                    for k, v in state.ref_levels.items()
                },
                "session_trade_eligible_time": {
                    str(k): v.isoformat() if v else None
                    for k, v in state.session_trade_eligible_time.items()
                },
                "fetch_attempted": {
                    str(k): v for k, v in state.fetch_attempted.items()
                },
            }
            with open(self._filepath, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.error("Failed to save state: %s", e)

    def load(self, session_ids: List[int], timezone) -> BotState:
        state = BotState()
        self.init_session_maps(state, session_ids)

        if not os.path.exists(self._filepath):
            return state

        try:
            with open(self._filepath, "r") as f:
                data = json.load(f)

            today_str = str(datetime.now(timezone).date())
            if data.get("last_reset_date") != today_str:
                return state

            state.last_reset_date = today_str

            for k, v in data.get("trades_taken", {}).items():
                sid = int(k)
                if sid in state.trades_taken:
                    state.trades_taken[sid] = SessionTradeState(
                        count=v["count"], directions=v["directions"]
                    )

            for t in data.get("active_trades", []):
                state.active_trades.append(
                    ActiveTrade(
                        side=t["side"],
                        tp=Decimal(t["tp"]),
                        sl=Decimal(t["sl"]),
                        sess_name=t["sess_name"],
                        cutoff_h=t["cutoff_h"],
                        sess_id=t["sess_id"],
                        entry_price=Decimal(t["entry_price"]),
                    )
                )

            for k, v in data.get("ref_levels", {}).items():
                sid = int(k)
                if v:
                    state.ref_levels[sid] = {pk: Decimal(pv) for pk, pv in v.items()}

            for k, v in data.get("session_trade_eligible_time", {}).items():
                sid = int(k)
                if v:
                    state.session_trade_eligible_time[sid] = datetime.fromisoformat(v)

            for k, v in data.get("fetch_attempted", {}).items():
                state.fetch_attempted[int(k)] = v

            logger.info("State restored from %s", self._filepath)
        except Exception as e:
            logger.error("Failed to load state: %s", e)

        return state

    def init_session_maps(self, state: BotState, session_ids: List[int]) -> None:
        for sid in session_ids:
            state.trades_taken.setdefault(sid, SessionTradeState())
            state.ref_levels.setdefault(sid, None)
            state.session_trade_eligible_time.setdefault(sid, None)
            state.fetch_attempted.setdefault(sid, False)
            state.scanning_started.setdefault(sid, False)

    def delete(self) -> None:
        if os.path.exists(self._filepath):
            os.remove(self._filepath)
            logger.info("State file deleted: %s", self._filepath)
