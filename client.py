import asyncio
import json
import logging
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict

import aiohttp
import websockets

logger = logging.getLogger(__name__)


class NadoWsClient:
    def __init__(self, ws_base: str, rest_base: str, ping_interval_seconds: int = 20, open_timeout_seconds: int = 20) -> None:
        self.ws_base = ws_base
        self.rest_base = rest_base.rstrip('/')
        self.ping_interval_seconds = ping_interval_seconds
        self.open_timeout_seconds = open_timeout_seconds

    async def resolve_product(self, symbol: str) -> Dict[str, Any]:
        url = f'{self.rest_base}/symbols'
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                resp.raise_for_status()
                payload = await resp.json()

        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and item.get('symbol') == symbol:
                    return item
            raise ValueError(f'Symbol not found in list response: {symbol}')

        if isinstance(payload, dict):
            symbols = (payload.get('data') or {}).get('symbols') or {}
            if symbol in symbols:
                item = symbols[symbol]
                if 'symbol' not in item:
                    item['symbol'] = symbol
                return item

        raise ValueError(f'Unexpected symbols response format or symbol not found: {symbol}')

    async def stream_bbo(self, product_id: int, callback: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        async with websockets.connect(
            self.ws_base,
            ping_interval=self.ping_interval_seconds,
            open_timeout=self.open_timeout_seconds,
            close_timeout=10,
            max_size=2**20,
        ) as ws:
            subscribe_msg = {
                'method': 'subscribe',
                'stream': {'type': 'best_bid_offer', 'product_id': product_id},
                'id': 10,
            }
            logger.info('Connecting to Nado subscriptions ws=%s product_id=%s', self.ws_base, product_id)
            await ws.send(json.dumps(subscribe_msg))
            logger.info('Sent subscribe message: %s', subscribe_msg)

            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8', errors='ignore')
                logger.debug('RAW_WS %s', raw)
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning('Invalid WS JSON: %s', raw)
                    continue
                await callback(msg)
                await asyncio.sleep(0)


def scale_x18(value: Any, scale: Decimal) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)) / scale
    except Exception:
        return None
