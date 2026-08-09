import asyncio
import logging
from decimal import Decimal
from typing import Dict, Optional

from tastytrade import Account, DXLinkStreamer, Session
from tastytrade.dxfeed import Quote
from tastytrade.instruments import Future
from tastytrade.order import (
    InstrumentType,
    Leg,
    NewComplexOrder,
    NewOrder,
    OrderAction,
    OrderTimeInForce,
    OrderType,
)

logger = logging.getLogger(__name__)


class TastyTradeClient:
    """Handles TastyTrade authentication, order placement, and quote retrieval."""

    def __init__(self):
        self.session: Optional[Session] = None
        self.account: Optional[Account] = None
        self.streamer_symbol: Optional[str] = None

    async def login(self, username: str, password: str, symbol_base: str) -> None:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.session = Session(username, password)
                future = Future.get(self.session, [symbol_base])[0]
                self.streamer_symbol = future.streamer_symbol
                self.account = Account.get(self.session)[1]
                logger.info("Login successful - %s", self.streamer_symbol)
                return
            except Exception as e:
                logger.warning(
                    "Login attempt %d/%d failed: %s", attempt + 1, max_retries, e
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(5)
                else:
                    raise

    def validate_session(self) -> bool:
        try:
            if self.session:
                self.session.validate()
                logger.info("Session validated")
                return True
        except Exception as e:
            logger.warning("Session validation failed: %s", e)
        return False

    async def revalidate_session(
        self, username: str, password: str, symbol_base: str
    ) -> bool:
        if self.validate_session():
            return True
        try:
            await self.login(username, password, symbol_base)
            return True
        except Exception as e:
            logger.error("Re-login failed: %s", e)
            return False

    async def place_bracket_order(
        self,
        symbol: str,
        buy: bool,
        target_points: Decimal,
        stop_points: Decimal,
        entry_price: Optional[Decimal] = None,
    ) -> Dict:
        """Places a market/limit entry, polls for fill, then places OCO brackets."""
        entry_action = OrderAction.BUY if buy else OrderAction.SELL
        entry_leg = Leg(
            instrument_type=InstrumentType.FUTURE,
            symbol=symbol,
            quantity=Decimal("1"),
            action=entry_action,
        )

        order_type = OrderType.MARKET if entry_price is None else OrderType.LIMIT
        entry_order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=order_type,
            legs=[entry_leg],
            price=entry_price,
        )

        entry_response = self.account.place_order(
            self.session, entry_order, dry_run=False
        )
        order_id = entry_response.order.id

        fill_price = None
        for _ in range(60):
            current = self.account.get_order(self.session, order_id)
            if current.status.value == "Filled":
                fills = current.legs[0].fills
                fill_price = sum(f.fill_price * f.quantity for f in fills) / sum(
                    f.quantity for f in fills
                )
                break
            elif current.status.value in ("Cancelled", "Rejected"):
                return {"error": f"Entry {current.status.value}"}
            await asyncio.sleep(1)

        if not fill_price:
            return {"error": "Timeout waiting for fill"}

        target_price = (
            fill_price + target_points if buy else fill_price - target_points
        )
        stop_price = fill_price - stop_points if buy else fill_price + stop_points
        exit_action = OrderAction.SELL if buy else OrderAction.BUY

        exit_leg = Leg(
            instrument_type=InstrumentType.FUTURE,
            symbol=symbol,
            quantity=Decimal("1"),
            action=exit_action,
        )
        oco_orders = [
            NewOrder(
                time_in_force=OrderTimeInForce.GTC,
                order_type=OrderType.LIMIT,
                price=target_price,
                legs=[exit_leg],
            ),
            NewOrder(
                time_in_force=OrderTimeInForce.GTC,
                order_type=OrderType.STOP,
                stop_trigger=stop_price,
                legs=[exit_leg],
            ),
        ]

        oco_response = self.account.place_complex_order(
            self.session, NewComplexOrder(orders=oco_orders), dry_run=False
        )
        return {
            "entry_order_id": order_id,
            "fill_price": fill_price,
            "complex_order_id": oco_response.complex_order.id,
            "target_price": target_price,
            "stop_price": stop_price,
        }

    async def get_current_quotes(self) -> str:
        """Fetches 5 bid/ask snapshots from a temporary streamer."""
        quotes = []
        try:
            async with DXLinkStreamer(self.session) as temp_streamer:
                await temp_streamer.subscribe(Quote, [self.streamer_symbol])
                async for quote in temp_streamer.listen(Quote):
                    quotes.append(
                        f"B: `{quote.bid_price}` | A: `{quote.ask_price}`"
                    )
                    if len(quotes) >= 5:
                        break
            return "\n" + "\n".join(quotes)
        except Exception as e:
            logger.warning("Quote fetch failed: %s", e)
            return "\n`Quotes unavailable`"
