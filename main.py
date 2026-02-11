import asyncio
import logging
import sys
import traceback
from datetime import datetime, timedelta
from decimal import Decimal

import pytz
import websockets.exceptions
from tastytrade import DXLinkStreamer
from tastytrade.dxfeed import Candle

from platinum_bot.api_client import TastyTradeClient
from platinum_bot.config import Settings, load_settings
from platinum_bot.data_handler import DataHandler
from platinum_bot.notifications import TelegramNotifier
from platinum_bot.risk_management import RiskManager
from platinum_bot.state import ActiveTrade, BotState, StateManager
from platinum_bot.strategy import BreakoutStrategy

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


class TradingBot:
    """Orchestrator that owns BotState and delegates to stateless services."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._tz = pytz.timezone(settings.timezone)
        self._client = TastyTradeClient()
        self._notifier = TelegramNotifier(
            settings.telegram_token, settings.telegram_chat_id
        )
        self._strategy = BreakoutStrategy()
        self._risk = RiskManager()
        self._state_mgr = StateManager()
        self._state = self._state_mgr.load(
            list(settings.sessions.keys()), self._tz
        )

    def _reset_daily_state(self) -> None:
        session_ids = list(self._settings.sessions.keys())
        self._state = BotState(
            last_reset_date=str(datetime.now(self._tz).date()),
        )
        self._state_mgr.init_session_maps(self._state, session_ids)
        self._state_mgr.save(self._state)
        logger.info("Daily reset complete")
        self._notifier.send("*New Trading Day* - All settings reset")

    async def _wait_until_next_event(self) -> float:
        now = datetime.now(self._tz)
        weekday = now.weekday()

        if (weekday == 4 and now.hour >= 14) or weekday == 5:
            sunday_open = (now + timedelta(days=(6 - weekday))).replace(
                hour=15, minute=0, second=0, microsecond=0
            )
            wait_seconds = (sunday_open - now).total_seconds()
            logger.info("Weekend detected. Sleeping %.1f hours", wait_seconds / 3600)
            return wait_seconds

        future_events = []
        for cfg in self._settings.sessions.values():
            t = now.replace(hour=cfg.start_hour, minute=0, second=0, microsecond=0)
            if t <= now:
                t += timedelta(days=1)
            future_events.append(t)

        midnight_reset = now.replace(hour=23, minute=59, second=0, microsecond=0)
        if midnight_reset <= now:
            midnight_reset += timedelta(days=1)
        future_events.append(midnight_reset)

        next_event = min(future_events)
        return (next_event - now).total_seconds() + 30

    async def _check_levels_periodically(self) -> None:
        """Background task that loads session reference levels every 60 seconds."""
        iteration = 0
        while True:
            try:
                await asyncio.sleep(60)
                iteration += 1

                if iteration % 60 == 0:
                    logger.info("Hourly session validation check")
                    await self._client.revalidate_session(
                        self._settings.tt_username,
                        self._settings.tt_password,
                        self._settings.symbol_base,
                    )

                now_la = datetime.now(self._tz)
                curr_h = now_la.hour

                for s_id, cfg in self._settings.sessions.items():
                    if curr_h < cfg.start_hour:
                        continue
                    if self._state.ref_levels.get(s_id) is not None:
                        continue
                    if self._state.fetch_attempted.get(s_id, False):
                        continue

                    logger.info(
                        "Background: loading %s levels (hour %d)", cfg.name, curr_h
                    )

                    data_handler = DataHandler(self._tz, self._client.streamer_symbol)
                    try:
                        async with DXLinkStreamer(self._client.session) as temp_streamer:
                            self._state.ref_levels[s_id] = (
                                await data_handler.fetch_hourly_levels(
                                    temp_streamer, cfg.ref_hour
                                )
                            )
                    except Exception as e:
                        logger.warning(
                            "Background streamer error for %s: %s", cfg.name, e
                        )

                    self._state.fetch_attempted[s_id] = True

                    if self._state.ref_levels[s_id]:
                        lvls = self._state.ref_levels[s_id]
                        eligible_time = now_la.replace(
                            hour=cfg.start_hour,
                            minute=self._settings.entry_delay_minutes,
                            second=0,
                            microsecond=0,
                        )
                        self._state.session_trade_eligible_time[s_id] = eligible_time

                        self._notifier.send(
                            f"*{cfg.name} Ready*\n"
                            f"High: `{lvls['high']}` | Low: `{lvls['low']}`\n"
                            f"Entries after: `{eligible_time.strftime('%I:%M %p')}`"
                        )
                        self._state.scanning_started[s_id] = True
                        self._state_mgr.save(self._state)
                        logger.info("Background: %s levels loaded", cfg.name)
                    else:
                        logger.warning(
                            "Background: failed to load %s levels", cfg.name
                        )

            except asyncio.CancelledError:
                logger.info("Level checker task cancelled")
                break
            except Exception as e:
                logger.warning("Error in background level checker: %s", e)
                logger.debug(traceback.format_exc())

    async def _process_exits(self, c_close: Decimal, curr_h: int) -> None:
        if not self._state.active_trades:
            return

        trades_to_remove = []
        for idx, trade in enumerate(self._state.active_trades):
            signal = self._strategy.check_exit_signals(trade, c_close, curr_h)
            if signal:
                msg = f"*{signal.reason}* - {trade.sess_name} {trade.side}"
                quote_block = await self._client.get_current_quotes()
                self._notifier.send(
                    f"{msg}\nEntry: `{trade.entry_price}` -> Exit: `{c_close}`"
                    f"\n**Recent Quotes:**{quote_block}"
                )
                trades_to_remove.append(idx)

        for idx in reversed(trades_to_remove):
            trade = self._state.active_trades[idx]
            logger.info("Trade complete: %s %s", trade.sess_name, trade.side)
            self._state.active_trades.pop(idx)

        if trades_to_remove:
            self._state_mgr.save(self._state)

    async def _process_entries(
        self, c_close: Decimal, now_la: datetime, curr_h: int
    ) -> bool:
        in_active_window = False

        for s_id, cfg in self._settings.sessions.items():
            if not self._risk.is_in_session_window(cfg, curr_h):
                continue

            in_active_window = True
            eligible_time = self._state.session_trade_eligible_time.get(s_id)

            if not self._risk.is_trade_eligible(
                s_id, self._state.trades_taken, now_la, eligible_time
            ):
                continue

            lvls = self._state.ref_levels.get(s_id)
            if not lvls:
                continue

            signal = self._strategy.check_entry_signal(
                c_close, lvls["high"], lvls["low"]
            )
            if not signal:
                continue

            if not self._risk.can_take_direction(
                s_id, signal.side, self._state.trades_taken
            ):
                continue

            quote_block = await self._client.get_current_quotes()

            trade_result = await self._client.place_bracket_order(
                symbol=self._settings.symbol_base,
                buy=(signal.side == "LONG"),
                target_points=Decimal(str(cfg.target_points)),
                stop_points=Decimal(str(cfg.stop_points)),
            )

            if "error" in trade_result:
                self._notifier.send(f"*TRADE ERROR*: {trade_result['error']}")
                continue

            tp = (
                signal.price + Decimal(str(cfg.target_points))
                if signal.side == "LONG"
                else signal.price - Decimal(str(cfg.target_points))
            )
            sl = (
                signal.price - Decimal(str(cfg.stop_points))
                if signal.side == "LONG"
                else signal.price + Decimal(str(cfg.stop_points))
            )

            new_trade = ActiveTrade(
                side=signal.side,
                tp=trade_result.get("target_price"),
                sl=trade_result.get("stop_price"),
                sess_name=cfg.name,
                cutoff_h=cfg.end_hour,
                sess_id=s_id,
                entry_price=trade_result.get("fill_price"),
            )

            self._state.active_trades.append(new_trade)
            self._state.trades_taken[s_id].count += 1
            self._state.trades_taken[s_id].directions.append(signal.side)
            self._state_mgr.save(self._state)

            trade_num = self._state.trades_taken[s_id].count
            self._notifier.send(
                f"*{signal.side} #{trade_num}* ({cfg.name})\n"
                f"Entry: `{signal.price}` | TP: `{tp}` | SL: `{sl}`"
                f"\n**Recent Quotes:**{quote_block}"
            )
            logger.info(
                "Trade entered: %s %s #%d", cfg.name, signal.side, trade_num
            )

        return in_active_window

    async def _run_monitor_cycle(self) -> None:
        """Stream lifecycle: login, subscribe, process candles, reconnect on failure."""
        now_la = datetime.now(self._tz)
        today = now_la.date()
        curr_h = now_la.hour
        curr_min = now_la.minute

        if curr_h == 23 and curr_min == 59:
            logger.info("Midnight reset triggered")
            self._reset_daily_state()
            await asyncio.sleep(120)
            return

        if self._state.last_reset_date != str(today):
            logger.info("New day detected, resetting")
            self._reset_daily_state()

        should_be_scanning = False
        for s_id, cfg in self._settings.sessions.items():
            if cfg.start_hour <= curr_h < cfg.end_hour:
                if self._state.trades_taken[s_id].count < 2:
                    should_be_scanning = True

        if not should_be_scanning and not self._state.active_trades:
            wait_sec = await self._wait_until_next_event()
            if wait_sec > 60:
                logger.info("Sleeping %.1f min until next event", wait_sec / 60)
                await asyncio.sleep(wait_sec)
                return

        await self._client.login(
            self._settings.tt_username,
            self._settings.tt_password,
            self._settings.symbol_base,
        )

        self._state.reconnect_count += 1
        logger.info(
            "Starting monitoring (connection #%d)", self._state.reconnect_count
        )
        streamer = DXLinkStreamer(self._client.session)

        level_checker_task = asyncio.create_task(self._check_levels_periodically())

        try:
            async with streamer:
                live_start = datetime.now(pytz.utc) - timedelta(minutes=5)
                await streamer.subscribe_candle(
                    [self._client.streamer_symbol],
                    interval="1m",
                    start_time=live_start,
                )

                candle_count = 0
                last_log_time = datetime.now()
                candle_stream = streamer.listen(Candle)

                while True:
                    try:
                        candle = await asyncio.wait_for(
                            candle_stream.__anext__(),
                            timeout=self._settings.max_idle_seconds,
                        )

                        candle_count += 1
                        now_utc = datetime.now()
                        if (now_utc - last_log_time).total_seconds() > 300:
                            logger.info(
                                "Alive - %d candles | Connection #%d",
                                candle_count,
                                self._state.reconnect_count,
                            )
                            last_log_time = now_utc

                        now_la = datetime.now(self._tz)
                        curr_h = now_la.hour
                        curr_min = now_la.minute

                        if curr_h == 23 and curr_min == 59:
                            logger.info("Midnight reset during streaming")
                            self._reset_daily_state()
                            break

                        if not DataHandler.is_valid_candle(candle):
                            continue

                        age = DataHandler.candle_age_seconds(candle)
                        if age > 120:
                            logger.info(
                                "Stale candle (age: %.0fs) - processing anyway", age
                            )

                        if not DataHandler.is_minute_candle(candle):
                            continue

                        c_close = Decimal(str(candle.close))

                        await self._process_exits(c_close, curr_h)
                        in_active_window = await self._process_entries(
                            c_close, now_la, curr_h
                        )

                        if not in_active_window and not self._state.active_trades:
                            logger.info("Sessions complete. Closing streamer.")
                            break

                    except asyncio.TimeoutError:
                        logger.warning(
                            "No candles for %ds - reconnecting",
                            self._settings.max_idle_seconds,
                        )
                        self._notifier.send(
                            f"Idle timeout ({self._settings.max_idle_seconds}s)"
                            " - reconnecting"
                        )
                        break

                    except StopAsyncIteration:
                        logger.info("Candle stream ended")
                        break

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning("WebSocket closed: %s", e)
            self._notifier.send("WebSocket disconnected - reconnecting")
        except ConnectionError as e:
            logger.warning("Connection error: %s", e)
            self._notifier.send("Connection lost - reconnecting")
        except Exception as e:
            logger.error("Streamer error: %s\n%s", e, traceback.format_exc())
            self._notifier.send(f"Error: {str(e)[:200]}")
        finally:
            level_checker_task.cancel()
            try:
                await level_checker_task
            except asyncio.CancelledError:
                pass
            logger.info(
                "Streamer closing (connection #%d)", self._state.reconnect_count
            )

    async def start(self) -> None:
        """Top-level restart loop with exponential backoff."""
        consecutive_errors = 0
        max_consecutive_errors = 10

        while True:
            try:
                await self._run_monitor_cycle()
                consecutive_errors = 0
                logger.info(
                    "Monitor cycle completed (reconnection #%d)",
                    self._state.reconnect_count,
                )
            except Exception as e:
                consecutive_errors += 1
                logger.error(
                    "ERROR (%d/%d):\n%s",
                    consecutive_errors,
                    max_consecutive_errors,
                    traceback.format_exc(),
                )

                if consecutive_errors == 1 or consecutive_errors % 3 == 0:
                    self._notifier.send(f"Bot Error #{consecutive_errors}")

                if consecutive_errors >= max_consecutive_errors:
                    logger.critical(
                        "STOPPED after %d consecutive errors", max_consecutive_errors
                    )
                    self._notifier.send(
                        f"STOPPED after {max_consecutive_errors} errors"
                    )
                    break

                wait_time = min(30 * (2 ** (consecutive_errors - 1)), 300)
                logger.info("Waiting %ds before retry", wait_time)
                await asyncio.sleep(wait_time)

            await asyncio.sleep(2)


def main() -> None:
    configure_logging()
    logger.info("Starting Platinum Trading Bot")
    settings = load_settings()
    bot = TradingBot(settings)
    asyncio.run(bot.start())


if __name__ == "__main__":
    main()
