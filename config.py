import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return int(value)


def _get_str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return value


def _get_optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value in (None, ""):
        return None
    return int(value)


@dataclass
class Settings:
    nado_env: str = _get_str("NADO_ENV", "mainnet") or "mainnet"
    nado_ws_base: str = _get_str("NADO_WS_BASE", "wss://gateway.prod.nado.xyz/v1/subscribe") or "wss://gateway.prod.nado.xyz/v1/subscribe"
    nado_rest_base: str = _get_str("NADO_REST_BASE", "https://gateway.prod.nado.xyz/v1") or "https://gateway.prod.nado.xyz/v1"
    nado_archive_base: str = _get_str("NADO_ARCHIVE_BASE", "https://archive.prod.nado.xyz/v1") or "https://archive.prod.nado.xyz/v1"
    nado_stream_type: str = _get_str("NADO_STREAM_TYPE", "best_bid_offer") or "best_bid_offer"
    nado_product_id: int | None = _get_optional_int("NADO_PRODUCT_ID")
    nado_price_scale: str = _get_str("NADO_PRICE_SCALE", "1000000000000000000") or "1000000000000000000"
    nado_volume_30d_usd: str = _get_str("NADO_VOLUME_30D_USD", "0") or "0"

    private_key: str = _get_str("PRIVATE_KEY", "0xYOUR_PRIVATE_KEY") or "0xYOUR_PRIVATE_KEY"
    account_address: str = _get_str("ACCOUNT_ADDRESS", "0xYOUR_ACCOUNT_ADDRESS") or "0xYOUR_ACCOUNT_ADDRESS"
    subaccount_name: str = _get_str("SUBACCOUNT_NAME", "primary") or "primary"

    symbol: str = _get_str("SYMBOL", "BTC-PERP") or "BTC-PERP"
    order_size: str = _get_str("ORDER_SIZE", "0.001") or "0.001"
    leverage: str = _get_str("LEVERAGE", "1") or "1"

    commission_maker_bps: str = _get_str("COMMISSION_MAKER_BPS", "1") or "1"
    commission_taker_bps: str = _get_str("COMMISSION_TAKER_BPS", "3.5") or "3.5"
    execution_style: str = _get_str("EXECUTION_STYLE", "maker") or "maker"

    telegram_enabled: bool = _get_bool("TELEGRAM_ENABLED", False)
    telegram_bot_token: str | None = _get_str("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = _get_str("TELEGRAM_CHAT_ID")
    telegram_admin_id: str | None = _get_str("TELEGRAM_ADMIN_ID")

    read_only: bool = _get_bool("READ_ONLY", True)
    dry_run: bool = _get_bool("DRY_RUN", True)

    log_level: str = _get_str("LOG_LEVEL", "INFO") or "INFO"
    log_dir: str = _get_str("LOG_DIR", "./logs") or "./logs"
    log_to_console: bool = _get_bool("LOG_TO_CONSOLE", True)
    log_ticker_every: int = _get_int("LOG_TICKER_EVERY", 1)

    ping_interval_seconds: int = _get_int("PING_INTERVAL_SECONDS", 20)
    reconnect_delay_seconds: int = _get_int("RECONNECT_DELAY_SECONDS", 5)
    ws_open_timeout_seconds: int = _get_int("WS_OPEN_TIMEOUT_SECONDS", 20)

    short_window: int = _get_int("SHORT_WINDOW", 8)
    long_window: int = _get_int("LONG_WINDOW", 24)
    min_edge_bps: str = _get_str("MIN_EDGE_BPS", "2") or "2"

    stop_loss_offset: str | None = _get_str("STOP_LOSS_OFFSET")
    take_profit_offset: str | None = _get_str("TAKE_PROFIT_OFFSET")
    trailing_distance: str | None = _get_str("TRAILING_DISTANCE")

    stop_loss_pct: str | None = _get_str("STOP_LOSS_PCT", "0.22")
    take_profit_pct: str | None = _get_str("TAKE_PROFIT_PCT", "0.40")
    trailing_pct: str | None = _get_str("TRAILING_PCT", "0.18")

    cooldown_ticks: int = _get_int("COOLDOWN_TICKS", 10)
    use_book_prices: bool = _get_bool("USE_BOOK_PRICES", True)

    min_wallet_balance_usd: str = _get_str("MIN_WALLET_BALANCE_USD", "5") or "5"
    enable_real_fee_sync: bool = _get_bool("ENABLE_REAL_FEE_SYNC", True)
    protect_net_positive_only: bool = _get_bool("PROTECT_NET_POSITIVE_ONLY", False)


settings = Settings()