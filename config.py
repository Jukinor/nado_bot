from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
        env_ignore_empty=True,
    )

    # ── окружение ────────────────────────────────────────────────────────────
    nado_env: str = Field(default='prod', alias='NADO_ENV')

    # ── endpoints ────────────────────────────────────────────────────────────
    nado_ws_base: str    = Field(default='wss://gateway.prod.nado.xyz/v1/subscribe', alias='NADO_WS_BASE')
    nado_rest_base: str  = Field(default='https://gateway.prod.nado.xyz/v1',         alias='NADO_REST_BASE')
    nado_archive_base: str = Field(default='https://archive.prod.nado.xyz/v1',       alias='NADO_ARCHIVE_BASE')
    nado_trigger_base: str = Field(default='https://trigger.prod.nado.xyz/v1',       alias='NADO_TRIGGER_BASE')
    nado_stream_type: str  = Field(default='best_bid_offer',                          alias='NADO_STREAM_TYPE')

    # ── EIP-712 domain ───────────────────────────────────────────────────────
    # Получить через GET /v1/contracts (chainId + endpoint address)
    nado_chain_id: int       = Field(default=57073, alias='NADO_CHAIN_ID')   # Ink mainnet
    nado_endpoint_address: str = Field(
        default='',
        alias='NADO_ENDPOINT_ADDRESS',
        description='verifyingContract для cancel/withdraw/etc. Берётся из GET /v1/contracts',
    )

    # ── продукт ──────────────────────────────────────────────────────────────
    nado_product_id: int | None = Field(default=None, alias='NADO_PRODUCT_ID')
    nado_price_scale: str       = Field(default='1000000000000000000', alias='NADO_PRICE_SCALE')
    nado_volume_30d_usd: str    = Field(default='0', alias='NADO_VOLUME_30D_USD')

    # ── кошелёк ──────────────────────────────────────────────────────────────
    private_key: str       = Field(default='0xYOUR_PRIVATE_KEY', alias='PRIVATE_KEY')
    account_address: str   = Field(default='0xYOUR_ACCOUNT_ADDRESS', alias='ACCOUNT_ADDRESS')
    subaccount_name: str   = Field(default='default', alias='SUBACCOUNT_NAME')

    # ── инструмент ───────────────────────────────────────────────────────────
    symbol: str      = Field(default='BTC-PERP', alias='SYMBOL')
    order_size: str  = Field(default='0.001',    alias='ORDER_SIZE')
    leverage: str    = Field(default='1',        alias='LEVERAGE')

    # ── комиссии ─────────────────────────────────────────────────────────────
    commission_maker_bps: str = Field(default='1',   alias='COMMISSION_MAKER_BPS')
    commission_taker_bps: str = Field(default='3.5', alias='COMMISSION_TAKER_BPS')
    execution_style: str      = Field(default='maker', alias='EXECUTION_STYLE')

    # ── Telegram ─────────────────────────────────────────────────────────────
    telegram_enabled: bool    = Field(default=False, alias='TELEGRAM_ENABLED')
    telegram_bot_token: str | None = Field(default=None, alias='TELEGRAM_BOT_TOKEN')
    telegram_chat_id: str | None   = Field(default=None, alias='TELEGRAM_CHAT_ID')
    telegram_admin_id: str | None  = Field(default=None, alias='TELEGRAM_ADMIN_ID')

    # ── режим запуска ────────────────────────────────────────────────────────
    read_only: bool = Field(default=True,  alias='READ_ONLY')
    dry_run: bool   = Field(default=True,  alias='DRY_RUN')

    # ── исполнение ───────────────────────────────────────────────────────────
    order_fill_timeout_sec: int    = Field(default=30,   alias='ORDER_FILL_TIMEOUT_SEC')
    enable_trigger_tp_sl: bool     = Field(default=True, alias='ENABLE_TRIGGER_TP_SL')
    enable_real_fee_sync: bool     = Field(default=False, alias='ENABLE_REAL_FEE_SYNC')
    min_wallet_balance_usd: str    = Field(default='10', alias='MIN_WALLET_BALANCE_USD')

    # ── логирование ──────────────────────────────────────────────────────────
    log_level: str      = Field(default='INFO', alias='LOG_LEVEL')
    log_dir: str        = Field(default='./logs', alias='LOG_DIR')
    log_to_console: bool = Field(default=True,  alias='LOG_TO_CONSOLE')
    log_ticker_every: int = Field(default=1,     alias='LOG_TICKER_EVERY')

    # ── сеть ─────────────────────────────────────────────────────────────────
    ping_interval_seconds: int    = Field(default=20, alias='PING_INTERVAL_SECONDS')
    reconnect_delay_seconds: int  = Field(default=5,  alias='RECONNECT_DELAY_SECONDS')
    ws_open_timeout_seconds: int  = Field(default=20, alias='WS_OPEN_TIMEOUT_SECONDS')

    # ── стратегия ────────────────────────────────────────────────────────────
    short_window: int  = Field(default=8,  alias='SHORT_WINDOW')
    long_window: int   = Field(default=24, alias='LONG_WINDOW')
    min_edge_bps: str  = Field(default='2', alias='MIN_EDGE_BPS')
    cooldown_ticks: int = Field(default=10, alias='COOLDOWN_TICKS')
    use_book_prices: bool = Field(default=True, alias='USE_BOOK_PRICES')

    stop_loss_offset: str | None     = Field(default=None, alias='STOP_LOSS_OFFSET')
    take_profit_offset: str | None   = Field(default=None, alias='TAKE_PROFIT_OFFSET')
    trailing_distance: str | None    = Field(default=None, alias='TRAILING_DISTANCE')
    stop_loss_pct: str | None        = Field(default='0.22', alias='STOP_LOSS_PCT')
    take_profit_pct: str | None      = Field(default='0.40', alias='TAKE_PROFIT_PCT')
    trailing_pct: str | None         = Field(default='0.18', alias='TRAILING_PCT')
    protect_net_positive_only: bool  = Field(default=False, alias='PROTECT_NET_POSITIVE_ONLY')

    @field_validator('nado_product_id', mode='before')
    @classmethod
    def blank_to_none(cls, value):
        if value in ('', None):
            return None
        return value


settings = Settings()
