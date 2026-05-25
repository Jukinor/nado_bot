import random
import time
from typing import Any, Dict

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

ORDER_TYPES = {
    "Order": [
        {"name": "sender",    "type": "bytes32"},
        {"name": "priceX18",  "type": "int128"},
        {"name": "amount",    "type": "int128"},
        {"name": "expiration","type": "uint64"},
        {"name": "nonce",     "type": "uint64"},
        {"name": "appendix",  "type": "uint128"},
    ]
}

CANCELLATION_TYPES = {
    "Cancellation": [
        {"name": "sender",     "type": "bytes32"},
        {"name": "productIds", "type": "uint32[]"},
        {"name": "digests",    "type": "bytes32[]"},
        {"name": "nonce",      "type": "uint64"},
    ]
}


def gen_order_verifying_contract(product_id: int) -> str:
    be_bytes = product_id.to_bytes(20, byteorder="big", signed=False)
    return "0x" + be_bytes.hex()


class WalletSigner:
    def __init__(self, private_key: str, chain_id: int = 57073, endpoint_address: str = "") -> None:
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        self.private_key = private_key
        self.chain_id = chain_id
        self.endpoint_address = endpoint_address

    @staticmethod
    def next_nonce(recv_window_ms: int = 5000) -> int:
        unix_ms = int(time.time() * 1000)
        random_20 = random.getrandbits(20)
        return ((unix_ms + recv_window_ms) << 20) + random_20

    def _domain_for_order(self, product_id: int) -> Dict[str, Any]:
        return {
            "name": "Nado",
            "version": "0.0.1",
            "chainId": self.chain_id,
            "verifyingContract": gen_order_verifying_contract(product_id),
        }

    def _domain_for_endpoint(self) -> Dict[str, Any]:
        return {
            "name": "Nado",
            "version": "0.0.1",
            "chainId": self.chain_id,
            "verifyingContract": self.endpoint_address,
        }

    def _sign(self, domain: Dict[str, Any], types: Dict[str, Any], message: Dict[str, Any]) -> str:
        signable = encode_typed_data_compat(domain_data=domain, message_types=types, message_data=message)
        return "0x" + self.account.sign_message(signable).signature.hex()

    def sign_order(self, order: Dict[str, Any], product_id: int) -> str:
        message = {
            "sender":     str(order["sender"]),
            "priceX18":   int(order["priceX18"]),
            "amount":     int(order["amount"]),
            "expiration": int(order.get("expiration", 4294967295)),
            "nonce":      int(order["nonce"]),
            "appendix":   int(order.get("appendix", 1)),
        }
        return self._sign(self._domain_for_order(product_id), ORDER_TYPES, message)

    def sign_cancel(self, cancel_data: Dict[str, Any]) -> str:
        message = {
            "sender":     str(cancel_data["sender"]),
            "productIds": [int(x) for x in cancel_data.get("productIds", [])],
            "digests":    [str(x) for x in cancel_data.get("digests", [])],
            "nonce":      int(cancel_data["nonce"]),
        }
        return self._sign(self._domain_for_endpoint(), CANCELLATION_TYPES, message)