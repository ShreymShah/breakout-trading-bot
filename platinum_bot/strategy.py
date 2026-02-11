from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from platinum_bot.state import ActiveTrade


@dataclass
class EntrySignal:
    side: str
    price: Decimal


@dataclass
class ExitSignal:
    reason: str
    trade: ActiveTrade
    exit_price: Decimal


class BreakoutStrategy:
    """Pure signal logic for platinum breakout entries and exits."""

    @staticmethod
    def check_entry_signal(
        close_price: Decimal,
        high_level: Decimal,
        low_level: Decimal,
    ) -> Optional[EntrySignal]:
        if close_price > high_level:
            return EntrySignal(side="LONG", price=close_price)
        if close_price < low_level:
            return EntrySignal(side="SHORT", price=close_price)
        return None

    @staticmethod
    def check_exit_signals(
        trade: ActiveTrade,
        close_price: Decimal,
        current_hour: int,
    ) -> Optional[ExitSignal]:
        if current_hour >= trade.cutoff_h:
            return ExitSignal(reason="CUTOFF", trade=trade, exit_price=close_price)

        if trade.side == "LONG":
            if close_price >= trade.tp:
                return ExitSignal(reason="TARGET", trade=trade, exit_price=close_price)
            if close_price <= trade.sl:
                return ExitSignal(reason="STOP", trade=trade, exit_price=close_price)
        else:
            if close_price <= trade.tp:
                return ExitSignal(reason="TARGET", trade=trade, exit_price=close_price)
            if close_price >= trade.sl:
                return ExitSignal(reason="STOP", trade=trade, exit_price=close_price)

        return None
