import os
import time
import secrets
from decimal import Decimal
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_typed_data

load_dotenv()

PRIVATE_KEY      = os.getenv("PRIVATE_KEY")
ACCOUNT_ADDRESS  = os.getenv("ACCOUNT_ADDRESS")
SUBACCOUNT_NAME  = os.getenv("SUBACCOUNT_NAME", "default")
REST_BASE        = os.getenv("NADO_REST_BASE", "https://gateway.prod.nado.xyz/v1")

NADO_VERIFYING_CONTRACT = "0x05ec92d78ed421f3d3ada77ffde167106565974e"
NADO_CHAIN_ID = 57073

account = Account.from_key(PRIVATE_KEY)
nonce = 1
# sender = wallet(20 bytes) + subaccount(12 bytes)
addr_bytes = bytes.fromhex(ACCOUNT_ADDRESS.removeprefix("0x"))
sub_bytes  = SUBACCOUNT_NAME.encode("utf-8")[:12].ljust(12, b"\x00")
sender_hex = "0x" + (addr_bytes + sub_bytes).hex()

# signer = нулевой адрес = отвязать linked signer
ZERO_SIGNER = "0x0000000000000000000000000000000000000000000000000000000000000000"

now_ts     = int(time.time())
expiration = now_ts + 60
nonce      = ((int(time.time() * 1000) + 2000) << 20) | secrets.randbelow(1 << 20)

LINK_SIGNER_TYPES = {
    "LinkSigner": [
        {"name": "sender",     "type": "bytes32"},
        {"name": "signer",     "type": "bytes32"},
        {"name": "nonce",      "type": "uint64"},
    ],
}

ZERO_SIGNER = "0x" + "00" * 32

domain = {
    "name":              "Nado",
    "version":           "0.0.1",
    "chainId":           NADO_CHAIN_ID,
    "verifyingContract": NADO_VERIFYING_CONTRACT,
}

message = {
    "sender":  bytes.fromhex(sender_hex.removeprefix("0x")),
    "signer":  bytes(32),
    "nonce":   nonce,
}

structured = encode_typed_data(
    domain_data=domain,
    message_types={"LinkSigner": LINK_SIGNER_TYPES["LinkSigner"]},
    message_data=message,
)
signed    = account.sign_message(structured)
signature = "0x" + signed.signature.hex()

payload = {
    "link_signer": {
        "tx": {
            "sender":    sender_hex,
            "signer":    "0x" + "00" * 32,
            "nonce":     nonce,
        },
        "signature": signature,
    }
}

import requests
resp = requests.post(f"{REST_BASE}/execute", json=payload, timeout=15)
print("status:", resp.status_code)
print("body:  ", resp.text)