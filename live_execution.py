import logging
import secrets
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional

import requests
from eth_account import Account
try:
    from eth_account.messages import encode_typed_data as _encode_typed_data_new
except ImportError:
    _encode_typed_data_new = None

try:
    from eth_account.messages import encode_structured_data as _encode_structured_data_old
except ImportError:
    _encode_structured_data_old = None


def encode_typed_data_compat(*, domain_data, message_types, message_data):
    if _encode_typed_data_new is not None:
        return _encode_typed_data_new(
            domain_data=domain_data,
            message_types=message_types,
            message_data=message_data,
        )
    if _encode_structured_data_old is not None:
        return _encode_structured_data_old(
            primitive={
                'types': {
                    'EIP712Domain': [
                        {'name': 'name', 'type': 'string'},
                        {'name': 'version', 'type': 'string'},
                        {'name': 'chainId', 'type': 'uint256'},
                        {'name': 'verifyingContract', 'type': 'address'},
                    ],
                    **message_types,
                },
                'primaryType': next(iter(message_types.keys())),
                'domain': domain_data,
                'message': message_data,
            }
        )
    raise ImportError('No compatible eth_account typed-data encoder found')

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
        self.sender = self.make_sender(self.account.address, self.subaccount_name)

    @staticmethod
    def sub_bytes12(name: str) -> bytes:
        raw = name.encode('utf-8')[:12]
        return raw + b'\x00' * (12 - len(raw))

    @classmethod
    def make_sender(cls, address: str, subaccount_name: str) -> str:
        addr = bytes.fromhex(address.lower().replace('0x', ''))
        return '0x' + addr.hex() + cls.sub_bytes12(subaccount_name).hex()

    @staticmethod
    def gen_order_verifying_contract(product_id: int) -> str:
        return '0x' + product_id.to_bytes(20, byteorder='big', signed=False).hex()

    @staticmethod
    def to_x18(value: Decimal) -> int:
        scaled = value * Decimal('1000000000000000000')
        return int(scaled.to_integral_value(rounding=ROUND_DOWN))

    def gen_nonce(self) -> int:
        recv_time_ms = int(time.time() * 1000) + self.recv_time_buffer_ms
        rand20 = secrets.randbelow(1 << 20)
        return (recv_time_ms << 20) | rand20

    @staticmethod
    def build_appendix(post_only: bool, reduce_only: bool, ioc: bool = False) -> int:
        version = 1
        isolated = 0
        order_type = 3 if post_only else (1 if ioc else 0)
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
        sender = self.sender
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
        sender_bytes = bytes.fromhex(sender[2:] if isinstance(sender, str) and sender.startswith('0x') else sender)

        message = {
            'sender': sender_bytes,
            'priceX18': price_x18,
            'amount': amount_x18,
            'expiration': expiration,
            'nonce': nonce,
            'appendix': appendix,
        }

        signable = encode_typed_data_compat(
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

    @staticmethod
    def extract_digest(order_result: Any) -> Optional[str]:
        if not isinstance(order_result, dict):
            return None
        data = order_result.get('data')
        if isinstance(data, dict) and data.get('digest'):
            return str(data.get('digest'))
        if order_result.get('digest'):
            return str(order_result.get('digest'))
        if order_result.get('orderdigest'):
            return str(order_result.get('orderdigest'))
        return None

    @staticmethod
    def _as_decimal(value: Any) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    @staticmethod
    def _collect_position_rows(payload: Any) -> list[Dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []

        rows: list[Dict[str, Any]] = []
        data = payload.get('data')

        if isinstance(data, list):
            rows.extend(row for row in data if isinstance(row, dict))
        elif isinstance(data, dict):
            for key in ('positions', 'orders', 'items', 'rows'):
                value = data.get(key)
                if isinstance(value, list):
                    rows.extend(row for row in value if isinstance(row, dict))
            if not rows:
                rows.append(data)

        for key in ('positions', 'orders', 'items', 'rows'):
            value = payload.get(key)
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))

        return rows

    def place_order(
        self,
        product_id: int,
        side: str,
        price: Decimal,
        size: Decimal,
        ioc: bool = False,
        post_only: bool = True,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        signed_amount = size if side == 'long' else -size
        now_ts = int(time.time())
        expiration = now_ts + 60
        nonce = self.gen_nonce()
        recv_time_ms = nonce >> 20
        appendix = self.build_appendix(post_only=post_only, reduce_only=reduce_only, ioc=ioc)

        logger.error(
            'LOCALTIME now=%s expiration=%s nonce=%s recv_time_ms=%s appendix=%s side=%s price=%s size=%s',
            now_ts, expiration, nonce, recv_time_ms, appendix, side, price, size,
        )

        order, signature = self._sign_order(
            product_id=product_id,
            price=price,
            amount=signed_amount,
            expiration=expiration,
            nonce=nonce,
            appendix=appendix,
        )

        payload = {
            'place_order': {
                'product_id': int(product_id),
                'order': order,
                'signature': signature,
            }
        }
        safe_payload = {
            'place_order': {
                'product_id': int(product_id),
                'order': order,
                'signature': signature[:12] + '...' if signature else None,
            }
        }

        url = f'{self.rest_base}/execute'
        resp = requests.post(url, json=payload, timeout=15)
        logger.error(
            'NADOEXECUTE status=%s url=%s body=%s payload=%s',
            resp.status_code, url, resp.text, safe_payload,
        )
        resp.raise_for_status()
        return resp.json()

    def cancel_orders(self, product_id: int, digests: list[str]) -> Dict[str, Any]:
        payload = {
            'cancel_orders': {
                'tx': {
                    'sender': self.sender,
                    'product_id': int(product_id),
                    'digests': list(digests),
                }
            }
        }
        url = f'{self.rest_base}/execute'
        resp = requests.post(url, json=payload, timeout=15)
        logger.error(
            'NADOCANCEL status=%s url=%s body=%s payload=%s',
            resp.status_code, url, resp.text, payload,
        )
        resp.raise_for_status()
        return resp.json()

    def cancel_product_orders(self, product_id: int) -> Dict[str, Any]:
        payload = {
            'cancel_product_orders': {
                'tx': {
                    'sender': self.sender,
                    'product_id': int(product_id),
                }
            }
        }
        url = f'{self.rest_base}/execute'
        resp = requests.post(url, json=payload, timeout=15)
        logger.error(
            'NADOCANCELPRODUCT status=%s url=%s body=%s payload=%s',
            resp.status_code, url, resp.text, payload,
        )
        resp.raise_for_status()
        return resp.json()

    def get_order(self, product_id: int, digest: str) -> Optional[Dict[str, Any]]:
        if not digest:
            return None

        params = {'type': 'order', 'product_id': product_id, 'digest': digest}
        resp = requests.get(f'{self.rest_base}/query', params=params, timeout=15)
        logger.error(
            'NADOQUERYORDER status=%s body=%s params=%s',
            resp.status_code, resp.text, params,
        )
        if resp.status_code != 200:
            return None
        return resp.json()

    def get_positions(self, product_id: int) -> Optional[Dict[str, Any]]:
        params = {
            'type': 'subaccount_orders',
            'sender': self.sender,
            'product_id': int(product_id),
        }
        resp = requests.get(f'{self.rest_base}/query', params=params, timeout=15)
        logger.error(
            'NADOQUERYPOSITIONS status=%s body=%s params=%s',
            resp.status_code, resp.text, params,
        )
        if resp.status_code != 200:
            return None
        return resp.json()

        def find_open_position(self, product_id: int) -> Optional[Dict[str, Any]]:
            params = {
                'type': 'isolated_positions',
                'subaccount': self.sender,
            }
    
        try:
            resp = requests.get(f'{self.rest_base}/query', params=params, timeout=15)
            logger.error(
                'NADOQUERYISOLATED status=%s body=%s params=%s',
                resp.status_code, resp.text, params,
            )
            if resp.status_code != 200:
                return None
    
            payload = resp.json()
        except Exception:
            logger.exception('FAILED_QUERY_ISOLATED_POSITIONS product_id=%s subaccount=%s', product_id, self.sender)
            return None
    
        if not isinstance(payload, dict):
            return None
        if str(payload.get('status')).lower() != 'success':
            logger.error(
                'ISOLATED_POSITIONS_QUERY_FAILED product_id=%s payload=%s',
                product_id, payload,
            )
            return None
    
        data = payload.get('data') or {}
        rows = data.get('isolated_positions') or []
        if not isinstance(rows, list):
            return None
    
        for row in rows:
            if not isinstance(row, dict):
                continue
    
            base_product = row.get('base_product') or {}
            if int(base_product.get('product_id', 0) or 0) != int(product_id):
                continue
    
            base_balance = ((row.get('base_balance') or {}).get('balance') or {})
            amount_raw = self._as_decimal(base_balance.get('amount'))
            v_quote_raw = self._as_decimal(base_balance.get('v_quote_balance'))
            oracle_raw = self._as_decimal(base_product.get('oracle_price_x18'))
    
            if amount_raw is None or amount_raw == 0:
                continue
    
            size = abs(amount_raw) / Decimal('1000000000000000000')
            side = 'long' if amount_raw > 0 else 'short'
    
            entry_price = None
            if v_quote_raw is not None and amount_raw != 0:
                try:
                    entry_price = abs(v_quote_raw / amount_raw)
                except Exception:
                    entry_price = None
    
            if entry_price is None and oracle_raw is not None:
                entry_price = oracle_raw / Decimal('1000000000000000000')
    
            result = {
                'product_id': int(product_id),
                'side': side,
                'size': str(size),
                'entry_price': str(entry_price) if entry_price is not None else None,
                'raw_amount_x18': str(amount_raw),
                'raw_v_quote_x18': str(v_quote_raw) if v_quote_raw is not None else None,
                'subaccount': row.get('subaccount'),
                'source': 'isolated_positions',
            }
    
            logger.error('OPEN_POSITION_FOUND product_id=%s result=%s raw_row=%s', product_id, result, row)
            return result
    
        logger.error('OPEN_POSITION_NOT_FOUND product_id=%s subaccount=%s rows=%s', product_id, self.sender, rows)
        return None
