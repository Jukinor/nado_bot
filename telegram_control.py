import asyncio
import logging
from typing import Any, Dict

import aiohttp

from config import settings

logger = logging.getLogger(__name__)


class TelegramControlBot:
    def __init__(self, strategy) -> None:
        self.strategy = strategy
        self.token = getattr(settings, 'telegram_bot_token', None)
        self.enabled = bool(getattr(settings, 'telegram_enabled', False))
        self.admin_id = getattr(settings, 'telegram_admin_id', None)
        self._offset: int | None = None
        if not self.enabled or not self.token:
            logger.info('Telegram control disabled')
            self.enabled = False

    async def _api(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f'https://api.telegram.org/bot{self.token}/{method}'
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    data = await resp.json()
                    if not data.get('ok'):
                        logger.warning('Telegram API error method=%s resp=%s', method, data)
                    return data
        except Exception:
            logger.exception('Telegram API request failed method=%s', method)
            return {}

    def _is_admin(self, msg: Dict[str, Any]) -> bool:
        if not self.admin_id:
            return True
        from_id = str(msg.get('from', {}).get('id'))
        return from_id == str(self.admin_id)

    async def _handle_command(self, msg: Dict[str, Any]) -> None:
        if not self._is_admin(msg):
            logger.warning('Unauthorized command from %s', msg.get('from'))
            return
        text = (msg.get('text') or '').strip()
        chat_id = msg.get('chat', {}).get('id')
        if not text.startswith('/'):
            return
        cmd = text.split()[0].lower()
        if cmd == '/status':
            snap = self.strategy.snapshot_state()
            msg_text = (
                f"Status {snap['symbol']}\n"
                f"Product ID: {snap['product_id']}\n"
                f"Exec style: {snap['execution_style']}\n"
                f"Fee tier: {snap['fee_tier']}\n"
                f"Maker/Taker bps: {snap['maker_bps']} / {snap['taker_bps']}\n"
                f"30d volume: {snap['volume_30d_usd']}\n"
                f"In position: {snap['in_position']} {snap['side']}\n"
                f"Entry: {snap['entry_price']} Size: {snap['size']}\n"
                f"SL/TP: {snap['stop_loss']} / {snap['take_profit']}\n"
                f"Best: {snap['best_price']} Breakeven: {snap['breakeven_price']}\n"
                f"Paused: {snap['paused']} Cooldown: {snap['cooldown_ticks_left']}\n"
                f"Day trades: {snap['day_trades']} Net: {snap['day_net_pnl']}"
            )
            await self._api('sendMessage', {'chat_id': chat_id, 'text': msg_text})
        elif cmd == '/pause':
            self.strategy.set_paused(True)
            await self._api('sendMessage', {'chat_id': chat_id, 'text': 'Bot paused (no new entries).'})
        elif cmd == '/resume':
            self.strategy.set_paused(False)
            await self._api('sendMessage', {'chat_id': chat_id, 'text': 'Bot resumed (entries allowed).'})
        elif cmd == '/close':
            self.strategy.request_manual_close()
            await self._api('sendMessage', {'chat_id': chat_id, 'text': 'Manual close requested on next tick.'})
        elif cmd == '/panic':
            self.strategy.request_manual_close()
            self.strategy.set_paused(True)
            await self._api('sendMessage', {'chat_id': chat_id, 'text': 'Panic: close current position and pause new entries.'})
        else:
            await self._api('sendMessage', {'chat_id': chat_id, 'text': 'Unknown command.'})

    async def run(self) -> None:
        if not self.enabled:
            return
        logger.info('Starting Telegram control bot polling')
        while True:
            try:
                params: Dict[str, Any] = {'timeout': 30}
                if self._offset is not None:
                    params['offset'] = self._offset
                url = f'https://api.telegram.org/bot{self.token}/getUpdates'
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params, timeout=35) as resp:
                        data = await resp.json()
                        if not data.get('ok'):
                            await asyncio.sleep(2)
                            continue
                        for upd in data.get('result', []):
                            self._offset = upd['update_id'] + 1
                            msg = upd.get('message') or upd.get('edited_message')
                            if not msg:
                                continue
                            await self._handle_command(msg)
            except Exception:
                logger.exception('Telegram control polling error')
                await asyncio.sleep(5)
