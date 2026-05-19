import logging
from decimal import Decimal
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class RealFeeAdapter:
    def __init__(self, rest_base: str, account_address: str, subaccount_name: str) -> None:
        self.rest_base = rest_base.rstrip('/')
        self.account_address = account_address
        self.subaccount_name = subaccount_name

    async def get_available_balance_usd(self) -> Optional[Decimal]:
        candidates = [
            f"{self.rest_base}/subaccounts/{self.account_address}/{self.subaccount_name}",
            f"{self.rest_base}/accounts/{self.account_address}/subaccounts/{self.subaccount_name}",
            f"{self.rest_base}/accounts/{self.account_address}/balance",
        ]
        for url in candidates:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=10) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                value = self._extract_balance(data)
                if value is not None:
                    return value
            except Exception:
                logger.debug('Balance query failed for %s', url, exc_info=True)
        return None

    async def get_order_fee_usd(self, digest: str) -> Optional[Decimal]:
        if not digest:
            return None
        candidates = [
            f"{self.rest_base}/orders/{digest}",
            f"{self.rest_base}/executions/{digest}",
            f"{self.rest_base}/matches/{digest}",
        ]
        for url in candidates:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=10) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                value = self._extract_fee(data)
                if value is not None:
                    return value
            except Exception:
                logger.debug('Fee query failed for %s', url, exc_info=True)
        return None

    def _extract_balance(self, payload) -> Optional[Decimal]:
        if isinstance(payload, dict):
            for key in ('available_balance_usd', 'availableUsd', 'available_balance', 'freeCollateral', 'collateral_value_usd', 'balance_usd', 'equity_usd'):
                if key in payload and payload[key] not in (None, ''):
                    return Decimal(str(payload[key]))
            for value in payload.values():
                found = self._extract_balance(value)
                if found is not None:
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = self._extract_balance(item)
                if found is not None:
                    return found
        return None

    def _extract_fee(self, payload) -> Optional[Decimal]:
        if isinstance(payload, dict):
            for key in ('fee_usd', 'feeUsd', 'fee', 'trading_fee', 'execution_fee'):
                if key in payload and payload[key] not in (None, ''):
                    return Decimal(str(payload[key]))
            for value in payload.values():
                found = self._extract_fee(value)
                if found is not None:
                    return found
        elif isinstance(payload, list):
            total = Decimal('0')
            found_any = False
            for item in payload:
                found = self._extract_fee(item)
                if found is not None:
                    total += found
                    found_any = True
            return total if found_any else None
        return None
