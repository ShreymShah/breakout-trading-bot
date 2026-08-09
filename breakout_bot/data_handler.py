import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Optional

import pytz
from tastytrade import DXLinkStreamer
from tastytrade.dxfeed import Candle

logger = logging.getLogger(__name__)


class DataHandler:
    """Historical candle fetching and candle validation helpers."""

    def __init__(self, timezone, streamer_symbol: str):
        self._tz = timezone
        self._streamer_symbol = streamer_symbol

    async def fetch_hourly_levels(
        self, streamer: DXLinkStreamer, hour_pt: int
    ) -> Optional[Dict[str, Decimal]]:
        """Fetches the high/low of a completed hourly candle."""
        try:
            now_la = datetime.now(self._tz)
            ref_hour_today = now_la.replace(
                hour=hour_pt, minute=0, second=0, microsecond=0
            )

            if now_la < ref_hour_today:
                logger.info("Hour %d:00 hasn't occurred yet today", hour_pt)
                return None

            if now_la.hour == hour_pt:
                logger.info("Hour %d:00 is still in progress", hour_pt)
                return None

            ref_dt = self._tz.localize(
                datetime.combine(now_la.date(), datetime.min.time())
                + timedelta(hours=hour_pt)
            )
            start_utc = ref_dt.astimezone(pytz.utc)

            logger.info("Fetching historical data for hour %d:00", hour_pt)

            await streamer.subscribe_candle(
                [self._streamer_symbol],
                interval="1h",
                start_time=start_utc,
            )

            async with asyncio.timeout(30):
                async for candle in streamer.listen(Candle):
                    candle_dt = datetime.fromtimestamp(candle.time / 1000, self._tz)
                    if candle_dt.hour == hour_pt and candle.high > 0:
                        logger.info(
                            "Levels loaded for hour %d: H %s | L %s",
                            hour_pt,
                            candle.high,
                            candle.low,
                        )
                        return {
                            "high": Decimal(str(candle.high)),
                            "low": Decimal(str(candle.low)),
                        }
        except asyncio.TimeoutError:
            logger.warning("Timeout fetching hour %d", hour_pt)
        except Exception as e:
            logger.warning("Error fetching hour %d: %s", hour_pt, e)

        return None

    @staticmethod
    def is_valid_candle(candle: Candle) -> bool:
        return candle.close > 0 and candle.high > 0 and candle.low > 0

    @staticmethod
    def is_minute_candle(candle: Candle) -> bool:
        return "{=m" in candle.event_symbol

    @staticmethod
    def candle_age_seconds(candle: Candle) -> float:
        candle_ts = datetime.fromtimestamp(candle.time / 1000, pytz.utc)
        return (datetime.now(pytz.utc) - candle_ts).total_seconds()
