"""
telegram_notifier.py — отправка Telegram-уведомлений.
"""
import logging
from decimal import Decimal
from typing import Optional

import aiohttp

from config import settings

logger = logging.getLogger(__name__)


class _TelegramNotifier:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._chat_id: Optional[str] = None
        self._enabled: bool = False

    def configure(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._enabled = bool(token and chat_id)

    async def _send(self, text: str) -> None:
        if not self._enabled:
            logger.debug("TG notifier disabled, skip: %s", text[:60])
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("TG send failed status=%s body=%s", resp.status, body[:200])
        except Exception as exc:
            logger.warning("TG send error: %s", exc)

    # ── запуск / остановка ──────────────────────────────────────────────────

    async def send_startup(self, health: dict) -> None:
        lines = ["🚀 <b>Бот запущен</b>"]
        for k, v in health.items():
            lines.append(f"  {k}: <code>{v}</code>")
        await self._send("\n".join(lines))

    async def send_shutdown(self, reason: str = "") -> None:
        text = "🔴 <b>Бот остановлен</b>"
        if reason:
            text += f"\nПричина: {reason}"
        await self._send(text)

    async def send_reconnect(self, attempt: int, reason: str = "") -> None:
        text = f"🔄 <b>Переподключение</b> (попытка {attempt})"
        if reason:
            text += f"\n{reason}"
        await self._send(text)

    async def send_status(self, msg: str) -> None:
        await self._send(f"ℹ️ {msg}")

    # ── сигналы / ордера ────────────────────────────────────────────────────

    async def send_signal(self, event: dict) -> None:
        action = event.get("action", "?")
        symbol = event.get("symbol", "?")
        side = event.get("side", "?")
        if action == "open":
            price = event.get("entry_price", "?")
            edge = event.get("effective_edge_bps", "?")
            text = (
                f"📡 <b>SIGNAL OPEN</b>\n"
                f"Symbol: {symbol} | Side: {side.upper()}\n"
                f"Price: <code>{price}</code> | Edge: {edge} bps"
            )
        elif action == "close":
            net = event.get("net_pnl", "?")
            reason = event.get("reason", "?")
            text = (
                f"📡 <b>SIGNAL CLOSE</b>\n"
                f"Symbol: {symbol} | Side: {side.upper()}\n"
                f"Reason: {reason} | Net PnL: <code>{net}</code>"
            )
        else:
            text = f"📡 Signal: {event}"
        await self._send(text)

    async def send_trade(
        self,
        action: str,
        symbol: str,
        side: str,
        price: Decimal,
        size: Decimal,
        pnl: Optional[Decimal] = None,
        reason: str = "",
    ) -> None:
        icon = "🟢" if action == "open" else "🔴"
        text = (
            f"{icon} <b>{action.upper()}</b> {symbol} | {side.upper()}\n"
            f"Price: <code>{price:.4f}</code> | Size: <code>{size}</code>"
        )
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            text += f"\nPnL: <code>{sign}{pnl:.4f}</code> USD"
        if reason:
            text += f"\nReason: {reason}"
        await self._send(text)

    async def send_order_placed(
        self, digest: str, side: str, price: Decimal, size: Decimal, symbol: str
    ) -> None:
        text = (
            f"📋 <b>Ордер размещён</b>\n"
            f"Symbol: {symbol} | {side.upper()}\n"
            f"Price: <code>{price:.4f}</code> | Size: <code>{size}</code>\n"
            f"Digest: <code>{digest[:16]}…</code>"
        )
        await self._send(text)

    async def send_order_filled(
        self,
        digest: str,
        symbol: str,
        side: str,
        fill_price: Decimal,
        filled_amount: Decimal,
        fee_usd: Decimal,
        tp_price: Optional[Decimal] = None,
        sl_price: Optional[Decimal] = None,
    ) -> None:
        tp_line = f"TP: <code>{tp_price:.4f}</code>" if tp_price else "TP: —"
        sl_line = f"SL: <code>{sl_price:.4f}</code>" if sl_price else "SL: —"
        fee_sign = "+" if fee_usd < 0 else "-"
        fee_abs = abs(fee_usd)
        text = (
            f"✅ <b>Ордер исполнен</b>\n"
            f"Symbol: {symbol} | {side.upper()}\n"
            f"Fill: <code>{fill_price:.4f}</code> × {filled_amount}\n"
            f"Fee: {fee_sign}<code>{fee_abs:.6f}</code> USD\n"
            f"{tp_line} | {sl_line}\n"
            f"Digest: <code>{digest[:16]}…</code>"
        )
        await self._send(text)

    async def send_order_timeout(self, digest: str, symbol: str, side: str) -> None:
        text = (
            f"⏱ <b>Ордер не заполнен (таймаут)</b>\n"
            f"Symbol: {symbol} | {side.upper()}\n"
            f"Digest: <code>{digest[:16]}…</code>\nОрдер отменяется."
        )
        await self._send(text)

    async def send_tp_sl_placed(
        self,
        symbol: str,
        tp_digest: Optional[str],
        sl_digest: Optional[str],
        tp_price: Optional[Decimal],
        sl_price: Optional[Decimal],
    ) -> None:
        lines = [f"🎯 <b>TP/SL выставлены</b> | {symbol}"]
        if tp_price and tp_digest:
            lines.append(f"TP: <code>{tp_price:.4f}</code> (digest: <code>{tp_digest[:12]}…</code>)")
        if sl_price and sl_digest:
            lines.append(f"SL: <code>{sl_price:.4f}</code> (digest: <code>{sl_digest[:12]}…</code>)")
        await self._send("\n".join(lines))

    # ── баланс / риск ───────────────────────────────────────────────────────

    async def send_balance_warning(self, balance_usd: Decimal, min_usd: Decimal) -> None:
        text = (
            f"⚠️ <b>Низкий баланс</b>\n"
            f"Баланс: <code>{balance_usd:.2f}</code> USD\n"
            f"Минимум: <code>{min_usd:.2f}</code> USD"
        )
        await self._send(text)

    async def send_error(self, title: str, details: str) -> None:
        text = f"🚨 <b>{title}</b>\n{details}"
        await self._send(text)

    # ── статистика ──────────────────────────────────────────────────────────

    async def send_period_summary(self, period: str, stats: dict) -> None:
        trades = stats.get("trades", 0)
        net = stats.get("net_pnl", Decimal("0"))
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        symbol = stats.get("symbol", "?")
        text = (
            f"📊 <b>Итог за {period}</b> | {symbol}\n"
            f"Сделок: {trades} | W/L: {wins}/{losses}\n"
            f"Net PnL: <code>{net:.4f}</code> USD"
        )
        await self._send(text)

    async def send_daily_summary(self, stats: dict) -> None:
        await self.send_period_summary("день", stats)

    async def send_weekly_summary(self, stats: dict) -> None:
        await self.send_period_summary("неделю", stats)


notifier = _TelegramNotifier()

if settings.telegram_enabled and settings.telegram_bot_token and settings.telegram_chat_id:
    notifier.configure(settings.telegram_bot_token, settings.telegram_chat_id)
