import asyncio
import logging
import time
from decimal import Decimal
from typing import Callable, Dict, Optional

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

# Order statuses that mean an order is no longer working.
TERMINAL_STATUSES = {"Filled", "Cancelled", "Rejected", "Expired", "Removed"}

FILL_TIMEOUT_SECONDS = 60
OCO_RETRIES = 3
OCO_RETRY_DELAY_SECONDS = 2

# OAuth access tokens last ~15 min; refresh on a shorter cadence so a call
# never hits an expired token.
TOKEN_REFRESH_SECONDS = 600
# Consecutive refresh failures before halting new entries and alerting once.
AUTH_FAIL_THRESHOLD = 3
AUTH_BACKOFF_CAP_SECONDS = 300


class TastyTradeClient:
    """Handles TastyTrade authentication, order placement, and quote retrieval.

    When `live` is False (dry-run), no broker orders are ever placed —
    place_bracket_order simulates an immediate fill at the given signal price
    instead. This lets the bot run signal-only against real market data.

    When `live` is True, entries are MARKET orders and the protective OCO
    bracket is placed from the actual fill price, with retries. If the OCO
    still can't be placed after all retries, the entry is immediately
    market-closed rather than left open with no protection.
    """

    def __init__(self, live: bool = False, notify: Optional[Callable[[str], None]] = None):
        self.live = live
        self._notify = notify or (lambda message: None)
        self.session: Optional[Session] = None
        self.account: Optional[Account] = None
        self.streamer_symbol: Optional[str] = None
        # Token-refresh + auth circuit-breaker state.
        self.auth_halted = False
        self._next_token_refresh = 0.0
        self._auth_failures = 0

    async def login(self, username: str, password: str, symbol_base: str) -> None:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.session = Session(username, password)
                future = Future.get(self.session, [symbol_base])[0]
                self.streamer_symbol = future.streamer_symbol
                # Account is only needed to place real orders. In dry-run,
                # tolerate a missing/inaccessible account so the bot can still
                # run signal-only against market data.
                try:
                    self.account = Account.get(self.session)[1]
                except Exception as acct_err:
                    if self.live:
                        raise
                    self.account = None
                    logger.warning(
                        "Account fetch failed (dry-run, continuing): %s", acct_err
                    )
                # Session() just performed an initial auth; schedule the next
                # proactive refresh and clear any prior auth-failure state.
                self._next_token_refresh = time.monotonic() + TOKEN_REFRESH_SECONDS
                self._auth_failures = 0
                self.auth_halted = False
                logger.info(
                    "Login successful - %s (mode=%s)",
                    self.streamer_symbol,
                    "LIVE" if self.live else "DRY-RUN",
                )
                return
            except Exception as e:
                logger.warning(
                    "Login attempt %d/%d failed: %s", attempt + 1, max_retries, e
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(5)
                else:
                    raise

    async def maybe_refresh_token(self, force: bool = False) -> None:
        """Proactively refreshes the OAuth access token before it expires.

        Gated by an internal timer, so it's cheap to call on every candle.
        On repeated refresh failure (e.g. a revoked refresh token), trips an
        auth circuit-breaker (`self.auth_halted`) so the caller can pause new
        entries; auto-resumes and alerts once refresh succeeds again.
        """
        if not self.session:
            return
        now = time.monotonic()
        if not force and now < self._next_token_refresh:
            return
        try:
            await asyncio.to_thread(self.session.refresh)
            self._next_token_refresh = now + TOKEN_REFRESH_SECONDS
            if self.auth_halted:
                self.auth_halted = False
                self._notify("*Auth recovered* - token refreshed, trading resumed.")
                logger.info("Auth recovered - token refresh succeeded, trading resumed")
            elif self._auth_failures:
                logger.info("Token refresh recovered after %d failure(s)", self._auth_failures)
            self._auth_failures = 0
        except Exception as e:
            self._auth_failures += 1
            backoff = min(30 * (2 ** (self._auth_failures - 1)), AUTH_BACKOFF_CAP_SECONDS)
            self._next_token_refresh = now + backoff
            logger.warning(
                "Token refresh failed #%d: %s (retry in %ds)",
                self._auth_failures, e, backoff,
            )
            if self._auth_failures >= AUTH_FAIL_THRESHOLD and not self.auth_halted:
                self.auth_halted = True
                self._notify(
                    f"*AUTH HALTED* - token refresh failing "
                    f"({self._auth_failures}x). New entries paused; "
                    f"auto-resumes once it recovers.\nLast error: `{str(e)[:160]}`"
                )
                logger.error(
                    "Auth circuit breaker tripped after %d consecutive failures",
                    self._auth_failures,
                )

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
        dry_run_price: Optional[Decimal] = None,
    ) -> Dict:
        """Places a market/limit entry, polls for fill, then places OCO brackets.

        In dry-run mode (self.live is False), places no broker orders and
        instead simulates an immediate fill at `dry_run_price`.

        In live mode, if the entry fills but the protective OCO can't be
        placed after retries, the position is immediately market-closed so
        the bot never holds an unprotected (naked) position.
        """
        if not self.live:
            return self._simulate_bracket(buy, target_points, stop_points, dry_run_price)

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

        fill_price = await self._await_fill(order_id)
        if fill_price is None:
            try:
                self.account.delete_order(self.session, order_id)
            except Exception as cancel_err:
                logger.warning(
                    "Failed to cancel unfilled entry %s: %s", order_id, cancel_err
                )
            return {"error": "Entry order did not fill (cancelled or timed out)"}

        complex_order_id = await self._place_bracket_with_retry(
            symbol, buy, fill_price, target_points, stop_points, order_id
        )
        if complex_order_id is None:
            return {"error": "OCO placement failed after retries; position closed"}

        target_price = (
            fill_price + target_points if buy else fill_price - target_points
        )
        stop_price = fill_price - stop_points if buy else fill_price + stop_points
        return {
            "entry_order_id": order_id,
            "fill_price": fill_price,
            "complex_order_id": complex_order_id,
            "target_price": target_price,
            "stop_price": stop_price,
        }

    async def _await_fill(self, order_id, timeout: int = FILL_TIMEOUT_SECONDS) -> Optional[Decimal]:
        """Polls an order until it fills or reaches a terminal/timeout state.

        Returns the quantity-weighted average fill price, or None if the
        order was cancelled/rejected or never filled within `timeout`.
        """
        for _ in range(timeout):
            current = self.account.get_order(self.session, order_id)
            status = current.status.value
            if status == "Filled":
                fills = current.legs[0].fills
                return sum(f.fill_price * f.quantity for f in fills) / sum(
                    f.quantity for f in fills
                )
            if status in ("Cancelled", "Rejected", "Expired"):
                logger.warning("Order %s ended in status %s", order_id, status)
                return None
            await asyncio.sleep(1)
        logger.warning("Order %s did not fill within %ds", order_id, timeout)
        return None

    async def _place_bracket_with_retry(
        self,
        symbol: str,
        buy: bool,
        fill_price: Decimal,
        target_points: Decimal,
        stop_points: Decimal,
        entry_order_id,
    ) -> Optional[str]:
        """Places the protective OCO bracket, retrying a few times.

        If every attempt fails, the position is market-closed immediately
        (never left naked) and None is returned.
        """
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

        last_err: Optional[Exception] = None
        for attempt in range(1, OCO_RETRIES + 1):
            try:
                oco_response = self.account.place_complex_order(
                    self.session, NewComplexOrder(orders=oco_orders), dry_run=False
                )
                return oco_response.complex_order.id
            except Exception as e:
                last_err = e
                logger.warning(
                    "OCO placement attempt %d/%d failed: %s", attempt, OCO_RETRIES, e
                )
                if attempt < OCO_RETRIES:
                    await asyncio.sleep(OCO_RETRY_DELAY_SECONDS)

        self._notify(
            f"*OCO PLACEMENT FAILED* after {OCO_RETRIES} retries.\n"
            f"Closing position to avoid holding it unprotected.\n"
            f"Last error: `{str(last_err)[:160]}`"
        )
        try:
            close_action = OrderAction.SELL if buy else OrderAction.BUY
            close_leg = Leg(
                instrument_type=InstrumentType.FUTURE,
                symbol=symbol,
                quantity=Decimal("1"),
                action=close_action,
            )
            close_order = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.MARKET,
                legs=[close_leg],
            )
            close_response = self.account.place_order(
                self.session, close_order, dry_run=False
            )
            close_fill = await self._await_fill(close_response.order.id)
            self._notify(
                f"Naked position CLOSED after OCO failure (market exit @ `{close_fill}`)."
            )
        except Exception as close_err:
            self._notify(
                f"*MANUAL INTERVENTION REQUIRED* - could NOT close naked position!\n"
                f"Entry id `{entry_order_id}` ({'LONG' if buy else 'SHORT'} 1 {symbol}).\n"
                f"Close error: `{str(close_err)[:160]}`"
            )
        return None

    async def close_position(
        self, symbol: str, was_buy: bool, complex_order_id: Optional[str]
    ) -> Dict:
        """Closes a live position ahead of an opposite-direction entry.

        Cancels the protective OCO (if any) and market-closes. Checks for a
        same-side fill both before and after the cancel, since the OCO's
        target/stop leg may have filled in the race window right as we
        decide to close — in that case we report the fill instead of
        market-closing into a fresh, unprotected opposite position.

        Only valid in live mode. Returns {"fill_price": ...} on success (with
        "already_closed": True if the OCO beat us to it), or {"error": ...}.
        """
        if not self.live:
            return {"error": "close_position called in dry-run mode"}

        if complex_order_id:
            orders = await self.fetch_live_orders()
            status, price = self.oco_status(orders, complex_order_id)
            if status == "FILLED":
                return {"fill_price": price, "already_closed": True}
            try:
                self.account.delete_complex_order(self.session, complex_order_id)
            except Exception as e:
                logger.warning(
                    "OCO %s cancel failed (may already be terminal): %s",
                    complex_order_id, e,
                )
            orders = await self.fetch_live_orders()
            status, price = self.oco_status(orders, complex_order_id)
            if status == "FILLED":
                return {"fill_price": price, "already_closed": True}

        close_action = OrderAction.SELL if was_buy else OrderAction.BUY
        close_leg = Leg(
            instrument_type=InstrumentType.FUTURE,
            symbol=symbol,
            quantity=Decimal("1"),
            action=close_action,
        )
        close_order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.MARKET,
            legs=[close_leg],
        )
        try:
            close_response = self.account.place_order(
                self.session, close_order, dry_run=False
            )
        except Exception as e:
            return {"error": f"Market close order failed: {e}"}

        fill_price = await self._await_fill(close_response.order.id)
        if fill_price is None:
            return {"error": "Market close order did not fill"}
        return {"fill_price": fill_price}

    async def fetch_live_orders(self) -> list:
        """Live + recently-terminal orders, or [] on failure / dry-run.

        Used both to detect a race-condition fill in close_position() and to
        reconcile restored trades against the broker on startup.
        """
        if not self.live or self.account is None:
            return []
        try:
            return self.account.get_live_orders(self.session)
        except Exception as e:
            logger.warning("get_live_orders failed: %s", e)
            return []

    @staticmethod
    def oco_status(orders: list, complex_order_id: Optional[str]):
        """Classifies a bracket's state from a fetch_live_orders() snapshot.

        Returns ("FILLED", price) if a leg has filled, ("WORKING", None) if
        the bracket is still live but unfilled, or ("UNKNOWN", None) if it
        has no id or wasn't found in the snapshot (e.g. aged out of the
        broker's live-orders window) — callers should not assume either
        filled or working in that case.
        """
        if not complex_order_id:
            return ("UNKNOWN", None)
        target = str(complex_order_id)
        has_working = False
        for o in orders:
            if str(getattr(o, "complex_order_id", None)) != target:
                continue
            try:
                status = o.status.value
            except Exception:
                continue
            if status == "Filled":
                try:
                    fills = o.legs[0].fills
                except Exception:
                    fills = None
                if fills:
                    total_qty = sum(f.quantity for f in fills)
                    if total_qty:
                        price = (
                            sum(f.fill_price * f.quantity for f in fills) / total_qty
                        )
                        return ("FILLED", price)
            elif status not in TERMINAL_STATUSES:
                has_working = True
        if has_working:
            return ("WORKING", None)
        return ("UNKNOWN", None)

    @staticmethod
    def _simulate_bracket(
        buy: bool,
        target_points: Decimal,
        stop_points: Decimal,
        dry_run_price: Optional[Decimal],
    ) -> Dict:
        """Dry-run stand-in for place_bracket_order: no broker calls, fills
        immediately at dry_run_price."""
        if dry_run_price is None:
            return {"error": "dry-run simulation requires dry_run_price"}
        fill_price = dry_run_price
        target_price = (
            fill_price + target_points if buy else fill_price - target_points
        )
        stop_price = fill_price - stop_points if buy else fill_price + stop_points
        return {
            "entry_order_id": None,
            "fill_price": fill_price,
            "complex_order_id": None,
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
