"""
order_tracker.py — ожидание заполнения ордера через Archive API.
"""
import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

SCALE_X18 = Decimal("1000000000000000000")
POLL_INTERVAL_SEC = 0.5
DEFAULT_TIMEOUT_SEC = 30


@dataclass
class fill_result:
    digest: str
    filled_amount: Decimal
    fill_price: Decimal
    fee_usd: Decimal
    is_taker: bool
    timestamp: int


async def wait_for_fill(
    archive_base: str,
    order_digest: str,
    order_amount: Decimal,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Optional[fill_result]:
    """
    Поллит Archive API до полного заполнения ордера или таймаута.

    Возвращает fill_result если ордер заполнился, None если таймаут.
    """
    url = archive_base
    payload = {"orders": {"digests": [order_digest], "limit": 1}}
    headers = {"Accept-Encoding": "gzip, br, deflate", "Content-Type": "application/json"}

    deadline = asyncio.get_event_loop().time() + timeout_sec
    last_log_at = 0.0

    async with aiohttp.ClientSession() as session:
        while True:
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                logger.warning(
                    "wait_for_fill TIMEOUT digest=%s timeout_sec=%s",
                    order_digest, timeout_sec,
                )
                return None

            try:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json(content_type=None)
            except Exception as exc:
                logger.debug("wait_for_fill poll error digest=%s exc=%s", order_digest, exc)
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            orders = (data.get("orders") or []) if isinstance(data, dict) else []
            if not orders:
                if now - last_log_at > 10:
                    logger.debug("wait_for_fill: ещё нет в индексе digest=%s", order_digest)
                    last_log_at = now
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            order = orders[0]
            base_filled = abs(Decimal(str(order.get("base_filled", "0")))) / SCALE_X18
            quote_filled = abs(Decimal(str(order.get("quote_filled", "0")))) / SCALE_X18
            fee_usd = Decimal(str(order.get("fee", "0"))) / SCALE_X18
            fill_price = (quote_filled / base_filled) if base_filled > 0 else Decimal("0")
            filled_fraction = (base_filled / order_amount) if order_amount > 0 else Decimal("0")
            ts_raw = order.get("last_fill_timestamp")
            timestamp = int(ts_raw) if ts_raw else 0

            if filled_fraction >= Decimal("0.999"):
                logger.info(
                    "wait_for_fill FILLED digest=%s base_filled=%s fill_price=%s fee_usd=%s",
                    order_digest, base_filled, fill_price, fee_usd,
                )
                return fill_result(
                    digest=order_digest,
                    filled_amount=base_filled,
                    fill_price=fill_price,
                    fee_usd=fee_usd,
                    is_taker=False,
                    timestamp=timestamp,
                )

            if now - last_log_at > 5:
                logger.info(
                    "wait_for_fill PARTIAL digest=%s filled=%.1f%%",
                    order_digest, float(filled_fraction) * 100,
                )
                last_log_at = now

            await asyncio.sleep(POLL_INTERVAL_SEC)
