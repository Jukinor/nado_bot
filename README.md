# Final Nado live build

This build removes `pydantic-settings`, `PyYAML`, and `nado_protocol`, and uses lightweight `eth-account` + `requests` for EIP-712 signed order placement against the Nado Gateway execute API. Nado documents that executes are sent to `POST /execute` or websocket executes, and all execute messages are signed via EIP-712. For place order specifically, the verifying contract must be the 20-byte hex encoding of the product id rather than the general endpoint address. [page:1][page:3]

The TypeScript SDK docs also show that place orders return a digest which can be used for later management and that order params include expiration, nonce, appendix, price, and amount normalized to 18 decimals. [page:2]

## Install

```bash
apt update
apt install -y python3 python3-venv python3-pip python-is-python3 build-essential pkg-config libssl-dev
cd /opt/nado_final_live_build
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

## Live mode

Set `READ_ONLY=false` and `DRY_RUN=false` only after confirming wallet, subaccount, and product id. Nado requires a valid EIP-712 signature and sender bytes32 containing wallet address plus 12-byte subaccount identifier. [page:1][page:3]
