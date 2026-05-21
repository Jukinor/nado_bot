"""
trigger_client.py — размещение TP/SL ордеров через Trigger API Nado.

Использует price_trigger с dependency на digest входного ордера.
Trigger активируется только после полного заполнения entry-ордера.
"""
import logging
import time
from decimal import Decimal
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

SCALE_X18 = Decimal("1000000000000000000")

# appendix для price-trigger ордера: bits 12-13 = 1 (PRICE), остальное 0
# build_trigger_appendix(trigger_type=1) → 1 << 12 = 4096
PRICE_TRIGGER_APPENDIX = "4096"


def _to_x18(value: Decimal) -> str:
    return str(int(value * SCALE_X18))


def _build_trigger_order(
    sender: str,
    price: Decimal,
    amount: Decimal,
    nonce: int,
    expiration: int = 4294967295,
) -> dict:
    return {
        "sender": sender,
        "priceX18": _to_x18(price),
        "amount": _to_x18(amount),
        "expiration": str(expiration),
        "nonce": str(nonce),
        "appendix": PRICE_TRIGGER_APPENDIX,
    }


class trigger_client:
    def __init__(
        self,
        trigger_base: str,
        signer,
        subaccount_bytes32: str,
    ) -> None:
        """
        trigger_base: например https://trigger.prod.nado.xyz/v1
        signer: экземпляр WalletSigner
        subaccount_bytes32: bytes32 hex субаккаунта
        """
        self.trigger_base = trigger_base.rstrip("/")
        self.signer = signer
        self.subaccount = subaccount_bytes32
        self._headers = {
            "Accept-Encoding": "gzip, br, deflate",
            "Content-Type": "application/json",
        }

    def _next_nonce(self) -> int:
        return int(time.time_ns() // 1_000_000) << 20

    async def place_tp_sl(
        self,
        product_id: int,
        entry_digest: str,
        side: str,
        filled_amount: Decimal,
        entry_price: Decimal,
        tp_price: Optional[Decimal],
        sl_price: Optional[Decimal],
    ) -> dict:
        """
        Размещает TP и SL как два trigger-ордера с dependency на entry_digest.

        side: 'long' или 'short'
        Возвращает dict с результатами {'tp': ..., 'sl': ...}
        """
        # Для закрытия позиции amount противоположного знака
        close_amount = -filled_amount if side == "long" else filled_amount

        results = {}

        async with aiohttp.ClientSession(headers=self._headers) as session:
            if tp_price is not None:
                results["tp"] = await self._place_single_trigger(
                    session=session,
                    product_id=product_id,
                    entry_digest=entry_digest,
                    side=side,
                    amount=close_amount,
                    trigger_price=tp_price,
                    label="TP",
                )

            if sl_price is not None:
                results["sl"] = await self._place_single_trigger(
                    session=session,
                    product_id=product_id,
                    entry_digest=entry_digest,
                    side=side,
                    amount=close_amount,
                    trigger_price=sl_price,
                    label="SL",
                )

        return results

    async def _place_single_trigger(
        self,
        session: aiohttp.ClientSession,
        product_id: int,
        entry_digest: str,
        side: str,
        amount: Decimal,
        trigger_price: Decimal,
        label: str,
    ) -> dict:
        nonce = self._next_nonce()
        order = _build_trigger_order(
            sender=self.subaccount,
            price=trigger_price,
            amount=amount,
            nonce=nonce,
        )

        # Выбираем тип триггера: TP — price_above для long, price_below для short
        # SL — наоборот
        if label == "TP":
            price_req_key = "oracle_price_above" if side == "long" else "oracle_price_below"
        else:  # SL
            price_req_key = "oracle_price_below" if side == "long" else "oracle_price_above"

        trigger = {
            "price_trigger": {
                "price_requirement": {price_req_key: _to_x18(trigger_price)},
                "dependency": {"digest": entry_digest, "on_partial_fill": False},
            }
        }

        # Подписываем ордер
        try:
            signature = self.signer.sign_order(order)
        except Exception as exc:
            logger.error("trigger_client sign error label=%s exc=%s", label, exc)
            return {"status": "sign_error", "error": str(exc)}

        payload = {
            "place_order": {
                "product_id": product_id,
                "order": order,
                "trigger": trigger,
                "signature": signature,
            }
        }

        url = f"{self.trigger_base}/execute"
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                result = await resp.json(content_type=None)
                if result.get("status") == "success":
                    logger.info(
                        "trigger_client %s placed product_id=%s price=%s digest=%s",
                        label, product_id, trigger_price, result.get("data", {}).get("digest"),
                    )
                else:
                    logger.error("trigger_client %s FAILED response=%s", label, result)
                return result
        except Exception as exc:
            logger.error("trigger_client %s request error exc=%s", label, exc)
            return {"status": "request_error", "error": str(exc)}
