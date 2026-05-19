import logging
from decimal import Decimal
from typing import Optional

import aiohttp

from config import settings

logger = logging.getLogger(__name__)


def _fmt(value) -> str:
    if value is None:
        return 'n/a'
    if isinstance(value, Decimal):
        text = format(value.normalize(), 'f')
        if '.' in text:
            text = text.rstrip('0').rstrip('.')
        return text
    return str(value)


class TelegramNotifier:
    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, 'telegram_enabled', False))
        self.token: Optional[str] = getattr(settings, 'telegram_bot_token', None)
        self.chat_id: Optional[str] = getattr(settings, 'telegram_chat_id', None)
        if self.enabled and (not self.token or not self.chat_id):
            logger.warning('Telegram enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing')
            self.enabled = False

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        if '\\n' in text:
            text = text.replace('\\n', '\n')
        if len(text) > 3500:
            text = text[:3490] + '...'
        url = f'https://api.telegram.org/bot{self.token}/sendMessage'
        payload = {'chat_id': self.chat_id, 'text': text, 'disable_web_page_preview': True}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=8) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning('Telegram send failed status=%s body=%s', resp.status, body)
        except Exception:
            logger.exception('Telegram send error')

    async def send_startup(self, health: dict) -> None:
        text = (
            'Nado bot started\n'
            f"Symbol: {health.get('symbol')}\n"
            f"Product ID: {health.get('product_id')}\n"
            f"Mode: {'READ_ONLY' if health.get('read_only') else 'LIVE'}\n"
            f"Dry run: {health.get('dry_run')}\n"
            f"Exec style: {health.get('execution_style')}\n"
            f"30d volume: {health.get('volume_30d_usd')}\n"
            f"Fee tier: {health.get('fee_tier')}\n"
            f"Maker/Taker bps: {health.get('maker_bps')} / {health.get('taker_bps')}"
        )
        await self.send(text)

    async def send_signal(self, event: dict) -> None:
        action = event.get('action')
        if action == 'open':
            text = (
                f"OPEN {str(event.get('side')).upper()}\n"
                f"Symbol: {event.get('symbol')} product_id={event.get('product_id')}\n"
                f"Entry: {_fmt(event.get('entry_price'))} Size: {_fmt(event.get('size'))}\n"
                f"SL/TP: {_fmt(event.get('stop_loss'))} / {_fmt(event.get('take_profit'))}\n"
                f"Raw/effective edge: {_fmt(event.get('edge_bps'))} / {_fmt(event.get('effective_edge_bps'))} bps\n"
                f"Entry/exit fee bps: {_fmt(event.get('entry_fee_bps'))} / {_fmt(event.get('exit_fee_bps'))}\n"
                f"Round-trip fee bps: {_fmt(event.get('round_trip_fee_bps'))}\n"
                f"Entry/exit exec: {event.get('entry_exec_style')} / {event.get('exit_exec_style')} | Tier: {event.get('fee_tier')}"
            )
            await self.send(text)
        elif action == 'close':
            text = (
                f"CLOSE {str(event.get('side')).upper()}\n"
                f"Symbol: {event.get('symbol')} product_id={event.get('product_id')} reason={event.get('reason')}\n"
                f"Entry/Exit: {_fmt(event.get('entry_price'))} / {_fmt(event.get('exit_price'))}\n"
                f"Gross/Fee/Net: {_fmt(event.get('gross_pnl'))} / {_fmt(event.get('fees'))} / {_fmt(event.get('net_pnl'))}\n"
                f"Day trades: {event.get('day_trades')} Day net: {_fmt(event.get('day_net_pnl'))}"
            )
            await self.send(text)

    async def send_summary(self, title: str, lines: list[str]) -> None:
        await self.send(title + '\n' + '\n'.join(lines))

    async def send_error(self, title: str, error_text: str) -> None:
        await self.send(f'{title}\n{error_text[:1500]}')


notifier = TelegramNotifier()
