"""
real_fee_adapter.py — взаимодействие с Gateway и Archive API:
  - get_available_balance_usd()   — баланс USDT0
  - get_open_position(product_id) — реальная открытая позиция на бирже
  - get_order_fee(digest)         — реальная комиссия по digest ордера
"""
import logging
from decimal import Decimal
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

SCALE_X18 = Decimal("1000000000000000000")
HEADERS = {"Accept-Encoding": "gzip, br, deflate", "Content-Type": "application/json"}


def _build_subaccount_bytes32(address: str, subaccount_name: str) -> str:
    """
    Формирует bytes32 hex субаккаунта: address (20 bytes) + name (12 bytes, right-padded 0).
    Пример: 0x<address_40chars><name_hex_padded_24chars>
    """
    addr_clean = address.lower().replace("0x", "")
    name_hex = subaccount_name.encode("utf-8").hex()
    name_padded = name_hex[:24].ljust(24, "0")
    return "0x" + addr_clean + name_padded


class RealFeeAdapter:
    def __init__(self, rest_base: str, account_address: str, subaccount_name: str) -> None:
        self.rest_base = rest_base.rstrip("/")
        self.account_address = account_address
        self.subaccount_name = subaccount_name
        self.subaccount_bytes32 = _build_subaccount_bytes32(account_address, subaccount_name)

    async def _query(self, session: aiohttp.ClientSession, payload: dict) -> Optional[dict]:
        url = f"{self.rest_base}/query"
        try:
            async with session.post(
                url, json=payload, headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json(content_type=None)
                if data.get("status") != "success":
                    logger.warning("gateway query failed payload=%s response=%s", payload, data)
                    return None
                return data.get("data")
        except Exception as exc:
            logger.warning("gateway query error payload=%s exc=%s", payload, exc)
            return None

    async def get_available_balance_usd(self) -> Optional[Decimal]:
        """Возвращает доступный баланс USDT0 (product_id=0) субаккаунта."""
        async with aiohttp.ClientSession() as session:
            data = await self._query(session, {
                "type": "subaccount_info",
                "subaccount": self.subaccount_bytes32,
            })
        if data is None:
            return None
        for bal in data.get("spot_balances", []):
            if bal.get("product_id") == 0:
                raw = bal.get("balance", {}).get("amount", "0")
                return Decimal(str(raw)) / SCALE_X18
        return Decimal("0")

    async def get_open_position(self, product_id: int) -> Optional[dict]:
        """
        Возвращает реальную perp-позицию по product_id или None если позиции нет.

        Возвращаемый dict:
          {
            'side': 'long' | 'short',
            'amount': Decimal,          # абсолютный размер
            'v_quote_balance': Decimal, # virtual quote (отрицательный entry cost)
            'entry_price': Decimal,     # приблизительная цена входа
          }
        """
        async with aiohttp.ClientSession() as session:
            data = await self._query(session, {
                "type": "subaccount_info",
                "subaccount": self.subaccount_bytes32,
            })
        if data is None:
            return None

        for bal in data.get("perp_balances", []):
            if bal.get("product_id") == product_id:
                amount_raw = Decimal(str(bal["balance"]["amount"]))
                amount = amount_raw / SCALE_X18
                if amount == 0:
                    return None
                v_quote = Decimal(str(bal["balance"]["v_quote_balance"])) / SCALE_X18
                side = "long" if amount > 0 else "short"
                abs_amount = abs(amount)
                # entry_price ≈ |v_quote| / abs_amount
                entry_price = abs(v_quote) / abs_amount if abs_amount > 0 else Decimal("0")
                return {
                    "side": side,
                    "amount": abs_amount,
                    "v_quote_balance": v_quote,
                    "entry_price": entry_price,
                }
        return None

    async def get_order_fee(
        self, archive_base: str, order_digest: str
    ) -> Optional[Decimal]:
        """
        Запрашивает реальную комиссию по digest ордера из Archive API.
        Возвращает комиссию в USD (Decimal) или None.
        """
        url = archive_base
        payload = {"orders": {"digests": [order_digest], "limit": 1}}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json(content_type=None)
            orders = data.get("orders") or []
            if not orders:
                return None
            fee_raw = orders[0].get("fee", "0")
            return Decimal(str(fee_raw)) / SCALE_X18
        except Exception as exc:
            logger.warning("get_order_fee error digest=%s exc=%s", order_digest, exc)
            return None
