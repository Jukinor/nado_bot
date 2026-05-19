import logging
import secrets
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data

logger = logging.getLogger(__name__)


class LiveExecutionClient:
    def __init__(
        self,
        rest_base: str,
        archive_base: str,
        private_key: str,
        subaccount_name: str,
        chain_id: int = 57073,
        recv_time_buffer_ms: int = 2000,
    ) -> None:
        self.rest_base = rest_base.rstrip('/')
        self.archive_base = archive_base.rstrip('/')
        self.account = Account.from_key(private_key)
        self.subaccount_name = subaccount_name
        self.chain_id = chain_id
        self.recv_time_buffer_ms = recv_time_buffer_ms

    @staticmethod
    def _sub_bytes12(name: str) -> bytes:
        raw = name.encode('utf-8')[:12]
        return raw + b'\x00' * (12 - len(raw))

    @classmethod
    def make_sender(cls, address: str, subaccount_name: str) -> str:
        addr = bytes.fromhex(address.lower().replace('0x', ''))
        return '0x' + (addr + cls._sub_bytes12(subaccount_name)).hex()

    @staticmethod
    def gen_order_verifying_contract(product_id: int) -> str:
        return '0x' + product_id.to_bytes(20, byteorder='big', signed=False).hex()

    @staticmethod
    def to_x18(value: Decimal) -> int:
        scaled = value * Decimal('1000000000000000000')
        return int(scaled.to_integral_value(rounding=ROUND_DOWN))

    def gen_nonce(self) -> int:
        recv_time_ms = int(time.time() * 1000) + self.recv_time_buffer_ms
        rand_20 = secrets.randbelow(1 << 20)
        return (recv_time_ms << 20) + rand_20

    @staticmethod
    def build_appendix(post_only: bool, reduce_only: bool) -> int:
        version = 1
        isolated = 0
        order_type = 3 if post_only else 0
        reduce = 1 if reduce_only else 0
        trigger = 0
        reserved = 0
        builder_fee_rate = 0
        builder = 0
        value = 0

        appendix = version
        appendix |= isolated << 8
        appendix |= order_type << 9
        appendix |= reduce << 11
        appendix |= trigger << 12
        appendix |= reserved << 14
        appendix |= builder_fee_rate << 38
        appendix |= builder << 48
        appendix |= value << 64
        return appendix

    def _sign_order(
        self,
        product_id: int,
        price: Decimal,
        amount: Decimal,
        expiration: int,
        nonce: int,
        appendix: int,
    ) -> tuple[Dict[str, Any], str]:
        sender = self.make_sender(self.account.address, self.subaccount_name)
        price_x18 = self.to_x18(price)
        amount_x18 = self.to_x18(amount)

        domain = {
            'name': 'Nado',
            'version': '0.0.1',
            'chainId': self.chain_id,
            'verifyingContract': self.gen_order_verifying_contract(product_id),
        }
        types = {
            'Order': [
                {'name': 'sender', 'type': 'bytes32'},
                {'name': 'priceX18', 'type': 'int128'},
                {'name': 'amount', 'type': 'int128'},
                {'name': 'expiration', 'type': 'uint64'},
                {'name': 'nonce', 'type': 'uint64'},
                {'name': 'appendix', 'type': 'uint128'},
            ]
        }
        message = {
            'sender': sender,
            'priceX18': price_x18,
            'amount': amount_x18,
            'expiration': expiration,
            'nonce': nonce,
            'appendix': appendix,
        }
        signable = encode_typed_data(
            domain_data=domain,
            message_types=types,
            message_data=message,
        )
        signature = self.account.sign_message(signable).signature.hex()

        wire_order = {
            'sender': sender,
            'priceX18': str(price_x18),
            'amount': str(amount_x18),
            'expiration': str(expiration),
            'nonce': str(nonce),
            'appendix': str(appendix),
        }
        return wire_order, '0x' + signature.replace('0x', '')

    def place_order(
        self,
        product_id: int,
        side: str,
        price: Decimal,
        size: Decimal,
        post_only: bool = True,
        reduce_only: bool = False,
        client_id: Optional[int] = None,
        spot_leverage: Optional[bool] = None,
    ) -> Dict[str, Any]:
        signed_amount = size if side == 'long' else -size
        now_ts = int(time.time())
        expiration = now_ts + 60
        nonce = self.gen_nonce()
        recv_time_ms = nonce >> 20
        appendix = self.build_appendix(post_only=post_only, reduce_only=reduce_only)

        logger.error(
            'LOCAL_TIME now=%s expiration=%s nonce=%s recv_time_ms=%s appendix=%s',
            now_ts,
            expiration,
            nonce,
            recv_time_ms,
            appendix,
        )

        order, signature = self._sign_order(
            product_id=product_id,
            price=price,
            amount=signed_amount,
            expiration=expiration,
            nonce=nonce,
            appendix=appendix,
        )

        place_order_payload: Dict[str, Any] = {
            'product_id': product_id,
            'order': order,
            'signature': signature,
        }
        if client_id is not None:
            place_order_payload['id'] = client_id
        if spot_leverage is not None:
            place_order_payload['spot_leverage'] = spot_leverage

        payload = {'place_order': place_order_payload}
        safe_payload = {
            'place_order': {
                **place_order_payload,
                'signature': (signature[:12] + '...') if signature else None,
            }
        }

        url = f'{self.rest_base}/execute'
        resp = requests.post(url, json=payload, timeout=15)
        logger.error(
            'NADO_EXECUTE status=%s url=%s body=%s payload=%s',
            resp.status_code,
            url,
            resp.text,
            safe_payload,
        )
        resp.raise_for_status()
        return resp.json()

    def get_order(self, product_id: int, digest: str) -> Optional[Dict[str, Any]]:
        if not digest:
            return None
        params = {
            'type': 'order',
            'product_id': product_id,
            'digest': digest,
        }
        resp = requests.get(f'{self.rest_base}/query', params=params, timeout=15)
        logger.error('NADO_QUERY_ORDER status=%s body=%s params=%s', resp.status_code, resp.text, params)
        if resp.status_code != 200:
            return None
        return resp.json()

    def get_order_fee(self, product_id: int, digest: str) -> Optional[Decimal]:
        data = self.get_order(product_id, digest)
        if not data:
            return None
        fee = ((data.get('data') or {}).get('fee')) if isinstance(data, dict) else None
        return Decimal(str(fee)) if fee is not None else None