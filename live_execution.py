"""
live_execution.py — исполнение ордеров на Nado DEX.

Поток:
1. execute_entry() → sign + place_order
2. wait_for_fill() → ждём заполнения (до order_fill_timeout_sec)
3. on timeout: cancel_order() + TG-уведомление
4. on fill:
   a. TG: ордер исполнен
   b. trigger_client.place_tp_sl() → ставим TP/SL с dependency на entry digest
   c. TG: TP/SL выставлены
"""
import asyncio
import logging
from decimal import Decimal
from typing import Optional

from config import settings
from order_tracker import wait_for_fill, fill_result
from real_fee_adapter import RealFeeAdapter
from telegram_notifier import notifier

logger = logging.getLogger(__name__)

SCALE_X18 = Decimal("1000000000000000000")


def _to_x18(value: Decimal) -> str:
    return str(int(value * SCALE_X18))


class live_execution_engine:
    """
    Фасад для выполнения торговых операций через REST/WebSocket Nado.
    Требует: gateway_client (NadoWsClient) и signer (WalletSigner).
    """

    def __init__(self, gateway_client, signer) -> None:
        self.gateway = gateway_client
        self.signer = signer
        self.symbol = settings.symbol
        self.product_id: Optional[int] = settings.nado_product_id

        self.fee_adapter = RealFeeAdapter(
            rest_base=settings.nado_rest_base,
            account_address=settings.account_address,
            subaccount_name=settings.subaccount_name,
        )

        if settings.enable_trigger_tp_sl:
            from trigger_client import trigger_client
            self._trigger_client = trigger_client(
                trigger_base=settings.nado_trigger_base,
                signer=self.signer,
                subaccount_bytes32=self.fee_adapter.subaccount_bytes32,
            )
        else:
            self._trigger_client = None

    # ── публичный API ────────────────────────────────────────────────────────

    async def execute_entry(
        self,
        side: str,
        size: Decimal,
        limit_price: Decimal,
        tp_price: Optional[Decimal] = None,
        sl_price: Optional[Decimal] = None,
    ) -> Optional[fill_result]:
        """
        Размещает лимитный ордер, ожидает заполнения, ставит TP/SL.
        Возвращает fill_result при успехе или None при таймауте/ошибке.
        """
        if settings.dry_run:
            logger.info("DRY_RUN: execute_entry side=%s size=%s price=%s", side, size, limit_price)
            return None

        if not self.product_id:
            logger.error("execute_entry: NADO_PRODUCT_ID не задан в config")
            return None

        # amount: положительный = buy (long), отрицательный = sell (short)
        sign_amount = size if side == "long" else -size
        nonce = self.signer.next_nonce()

        order = {
            "sender":     self.fee_adapter.subaccount_bytes32,
            "priceX18":   _to_x18(limit_price),
            "amount":     _to_x18(sign_amount),
            "expiration": "4294967295",
            "nonce":      str(nonce),
            "appendix":   "1",   # version=1, order_type=DEFAULT, no trigger
        }

        try:
            # sign_order теперь требует product_id для правильного verifyingContract
            signature = self.signer.sign_order(order, product_id=self.product_id)
        except Exception as exc:
            logger.error("execute_entry sign error: %s", exc)
            await notifier.send_error("Ошибка подписи ордера", str(exc))
            return None

        place_resp = await self.gateway.place_order(
            product_id=self.product_id,
            order=order,
            signature=signature,
        )

        if not place_resp or place_resp.get("status") != "success":
            logger.error("execute_entry place_order failed: %s", place_resp)
            await notifier.send_error("Ошибка размещения ордера", str(place_resp))
            return None

        digest: str = place_resp["data"]["digest"]
        logger.info(
            "execute_entry placed digest=%s side=%s size=%s price=%s",
            digest, side, size, limit_price,
        )
        await notifier.send_order_placed(
            digest=digest, side=side, price=limit_price, size=size, symbol=self.symbol,
        )

        # Ждём заполнения
        result = await wait_for_fill(
            archive_base=settings.nado_archive_base,
            order_digest=digest,
            order_amount=size,
            timeout_sec=settings.order_fill_timeout_sec,
        )

        if result is None:
            logger.warning("execute_entry TIMEOUT, cancelling digest=%s", digest)
            await self._cancel_order(digest)
            await notifier.send_order_timeout(digest, self.symbol, side)
            return None

        await notifier.send_order_filled(
            digest=digest,
            symbol=self.symbol,
            side=side,
            fill_price=result.fill_price,
            filled_amount=result.filled_amount,
            fee_usd=result.fee_usd,
            tp_price=tp_price,
            sl_price=sl_price,
        )

        if self._trigger_client and (tp_price or sl_price):
            await self._place_tp_sl(
                entry_digest=digest,
                side=side,
                fill_result_=result,
                tp_price=tp_price,
                sl_price=sl_price,
            )

        return result

    async def get_position(self) -> Optional[dict]:
        if not self.product_id:
            return None
        return await self.fee_adapter.get_open_position(self.product_id)

    async def get_balance_usd(self) -> Optional[Decimal]:
        return await self.fee_adapter.get_available_balance_usd()

    # ── приватные методы ─────────────────────────────────────────────────────

    async def _cancel_order(self, digest: str) -> None:
        try:
            cancel_data = {
                "sender":     self.fee_adapter.subaccount_bytes32,
                "productIds": [self.product_id],
                "digests":    [digest],
                "nonce":      str(self.signer.next_nonce()),
            }
            cancel_sig = self.signer.sign_cancel(cancel_data)
            await self.gateway.cancel_orders(
                cancel_order_data=cancel_data,
                signature=cancel_sig,
            )
            logger.info("_cancel_order ok digest=%s", digest)
        except Exception as exc:
            logger.error("_cancel_order failed digest=%s exc=%s", digest, exc)

    async def _place_tp_sl(
        self,
        entry_digest: str,
        side: str,
        fill_result_: fill_result,
        tp_price: Optional[Decimal],
        sl_price: Optional[Decimal],
    ) -> None:
        results = await self._trigger_client.place_tp_sl(
            product_id=self.product_id,
            entry_digest=entry_digest,
            side=side,
            filled_amount=fill_result_.filled_amount,
            entry_price=fill_result_.fill_price,
            tp_price=tp_price,
            sl_price=sl_price,
        )
        tp_digest = (results.get("tp") or {}).get("data", {}).get("digest")
        sl_digest = (results.get("sl") or {}).get("data", {}).get("digest")
        logger.info("_place_tp_sl tp_digest=%s sl_digest=%s", tp_digest, sl_digest)
        await notifier.send_tp_sl_placed(
            symbol=self.symbol,
            tp_digest=tp_digest,
            sl_digest=sl_digest,
            tp_price=tp_price,
            sl_price=sl_price,
        )


# Алиас для обратной совместимости с bot.py
LiveExecutionClient = live_execution_engine
