import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict

from client import NadoWsClient, scale_x18
from config import settings
from live_execution import LiveExecutionClient
from real_fee_adapter import RealFeeAdapter
from signer import WalletSigner
from strategy_v2 import TrendSignalStrategy
from telegram_control import TelegramControlBot
from telegram_notifier import notifier

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    fmt = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_dir / 'nado_bot.log', maxBytes=5_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)
    root.addHandler(file_handler)
    if settings.log_to_console:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console.setLevel(level)
        root.addHandler(console)
    logging.getLogger('websockets').setLevel(logging.WARNING)
    logger.info(
        'Logging initialized level=%s log_dir=%s pid=%s',
        settings.log_level, log_dir, os.getpid(),
    )


class NadoStage1V34Bot:
    def __init__(self) -> None:
        self.signer = WalletSigner(
            private_key=settings.private_key,
            chain_id=settings.nado_chain_id,
            endpoint_address=settings.nado_endpoint_address,
        )
        self.client = NadoWsClient(
            settings.nado_ws_base,
            settings.nado_rest_base,
            settings.ping_interval_seconds,
            settings.ws_open_timeout_seconds,
        )
        self.real_fee_adapter = RealFeeAdapter(
            settings.nado_rest_base,
            settings.account_address,
            settings.subaccount_name,
        )
        self.live_execution = (
            LiveExecutionClient(self.client, self.signer)
            if (not settings.read_only and not settings.dry_run)
            else None
        )
        self.product_id: int | None = settings.nado_product_id
        self.resolved_symbol: str = settings.symbol
        self.strategy: TrendSignalStrategy | None = None
        self.tg_control: TelegramControlBot | None = None
        self._ticker_count = 0
        self._scale = Decimal(str(settings.nado_price_scale))
        self._trading_halted_low_balance = False

    def _build_strategy(self, product_id: int, symbol: str) -> None:
        self.strategy = TrendSignalStrategy(
            symbol=symbol,
            product_id=product_id,
            execution_style=settings.execution_style,
            order_size=settings.order_size,
            short_window=settings.short_window,
            long_window=settings.long_window,
            min_edge_bps=settings.min_edge_bps,
            stop_loss_offset=settings.stop_loss_offset,
            take_profit_offset=settings.take_profit_offset,
            trailing_distance=settings.trailing_distance,
            cooldown_ticks=settings.cooldown_ticks,
            use_book_prices=settings.use_book_prices,
            leverage=settings.leverage,
            commission_maker_bps=settings.commission_maker_bps,
            commission_taker_bps=settings.commission_taker_bps,
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            trailing_pct=settings.trailing_pct,
            volume_30d_usd=settings.nado_volume_30d_usd,
            protect_net_positive_only=settings.protect_net_positive_only,
        )
        self.tg_control = TelegramControlBot(self.strategy)

    async def _check_balance_guard(self) -> None:
        balance = await self.real_fee_adapter.get_available_balance_usd()
        if balance is None or self.strategy is None:
            return
        threshold = Decimal(str(settings.min_wallet_balance_usd))
        if balance < threshold:
            if not self._trading_halted_low_balance:
                self.strategy.set_paused(True)
                self._trading_halted_low_balance = True
                logger.warning(
                    'LOW_BALANCE_GUARD_TRIGGERED available_balance_usd=%s threshold_usd=%s',
                    balance, threshold,
                )
                await notifier.send_error(
                    'Low balance guard triggered',
                    f'Available balance {balance} is below threshold {threshold}. Trading paused.',
                )
        elif self._trading_halted_low_balance:
            self._trading_halted_low_balance = False
            logger.info(
                'LOW_BALANCE_GUARD_CLEARED available_balance_usd=%s threshold_usd=%s',
                balance, threshold,
            )

    def healthcheck(self) -> Dict[str, Any]:
        snap = self.strategy.snapshot_state() if self.strategy else {}
        return {
            'env': settings.nado_env,
            'configured_wallet': self.signer.address,
            'expected_wallet': settings.account_address,
            'subaccount_name': settings.subaccount_name,
            'symbol': self.resolved_symbol,
            'product_id': self.product_id,
            'chain_id': settings.nado_chain_id,
            'endpoint_address': settings.nado_endpoint_address,
            'ws_base': settings.nado_ws_base,
            'rest_base': settings.nado_rest_base,
            'archive_base': settings.nado_archive_base,
            'trigger_base': settings.nado_trigger_base,
            'stream_type': settings.nado_stream_type,
            'read_only': settings.read_only,
            'dry_run': settings.dry_run,
            'execution_style': settings.execution_style,
            'short_window': settings.short_window,
            'long_window': settings.long_window,
            'min_edge_bps': settings.min_edge_bps,
            'leverage': settings.leverage,
            'stop_loss_pct': settings.stop_loss_pct,
            'take_profit_pct': settings.take_profit_pct,
            'trailing_pct': settings.trailing_pct,
            'min_wallet_balance_usd': settings.min_wallet_balance_usd,
            'real_fee_sync': settings.enable_real_fee_sync,
            'log_level': settings.log_level,
            'log_ticker_every': settings.log_ticker_every,
            'price_scale': settings.nado_price_scale,
            'volume_30d_usd': snap.get('volume_30d_usd'),
            'fee_tier': snap.get('fee_tier'),
            'maker_bps': snap.get('maker_bps'),
            'taker_bps': snap.get('taker_bps'),
        }

    async def _periodic_summary_loop(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            await asyncio.sleep(max((next_hour - now).total_seconds(), 1))
            try:
                if self.strategy is not None:
                    await self.strategy.send_period_summary('hourly')
            except Exception:
                logger.exception('Failed to send hourly summary')

    async def _daily_summary_loop(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            next_day = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            await asyncio.sleep(max((next_day - now).total_seconds(), 1))
            try:
                if self.strategy is not None:
                    await self.strategy.send_period_summary('daily')
            except Exception:
                logger.exception('Failed to send daily summary')

    async def handle_ticker(self, msg: Dict[str, Any]) -> None:
        if msg.get('result') is None and msg.get('id') is not None:
            logger.info('Subscription confirmed: %s', msg)
            return

        data = msg.get('data') or msg.get('result') or msg
        if not isinstance(data, dict):
            logger.debug('Non-dict event: %s', msg)
            return

        event_type = (
            data.get('type')
            or msg.get('type')
            or ((msg.get('stream') or {}).get('type') if isinstance(msg.get('stream'), dict) else None)
        )
        if event_type and event_type != 'best_bid_offer':
            logger.debug('Ignoring non-BBO event: %s', msg)
            return

        event_product_id = data.get('product_id') or data.get('productId') or data.get('pid')
        if (
            self.product_id is not None
            and event_product_id is not None
            and int(event_product_id) != int(self.product_id)
        ):
            return

        bid_px = scale_x18(data.get('bid') or data.get('best_bid') or data.get('bid_price'), self._scale)
        ask_px = scale_x18(data.get('ask') or data.get('best_ask') or data.get('ask_price'), self._scale)
        mark_px = scale_x18(data.get('mark_price') or data.get('price'), self._scale)

        if mark_px is None:
            if bid_px is not None and ask_px is not None:
                mark_px = (bid_px + ask_px) / Decimal('2')
            elif bid_px is not None:
                mark_px = bid_px
            elif ask_px is not None:
                mark_px = ask_px

        if mark_px is None:
            logger.debug('No usable price fields in event: %s', msg)
            return

        self._ticker_count += 1
        if self._ticker_count % max(settings.log_ticker_every, 1) == 0:
            logger.info(
                'Ticker %s product_id=%s mark=%s bid=%s ask=%s raw=%s',
                self.resolved_symbol, self.product_id, mark_px, bid_px, ask_px, data,
            )

        if self.strategy is None:
            logger.warning('Strategy not initialized yet; dropping ticker')
            return

        if self._ticker_count % 25 == 0:
            await self._check_balance_guard()

        raw_bid_qty = data.get('bid_qty') or data.get('bidQty') or msg.get('bid_qty')
        raw_ask_qty = data.get('ask_qty') or data.get('askQty') or msg.get('ask_qty')
        bid_qty = Decimal(str(raw_bid_qty)) / Decimal('1000000000000000000') if raw_bid_qty is not None else Decimal('0')
        ask_qty = Decimal(str(raw_ask_qty)) / Decimal('1000000000000000000') if raw_ask_qty is not None else Decimal('0')

        if hasattr(self.strategy, 'update_l1_sizes'):
            self.strategy.update_l1_sizes(bid_qty, ask_qty)

        event = self.strategy.on_ticker(mark_px, bid_px, ask_px)
        if not event:
            return

        logger.warning('SIGNAL %s', event)
        await notifier.send_signal(event)

        if settings.enable_real_fee_sync:
            digest = str(event.get('order_digest') or '')
            fee = await self.real_fee_adapter.get_order_fee_usd(digest) if digest else None
            if fee is not None:
                if event.get('action') == 'open':
                    self.strategy.mark_entry_fee_actual(fee, digest)
                elif event.get('action') == 'close':
                    self.strategy.mark_exit_fee_actual(fee, digest)

        if settings.read_only:
            logger.info('READ_ONLY mode active, no order sent')
            return
        if settings.dry_run:
            logger.info('DRY_RUN mode active, live credentials loaded but no order sent')
            return
        if self.live_execution is None:
            logger.error('LIVE execution requested but client is not initialized')
            return

        if event.get('action') == 'open':
            order_result = await self.live_execution.execute_entry(
                side=str(event.get('side')),
                size=Decimal(str(event.get('size'))),
                limit_price=Decimal(str(event.get('entry_price'))),
                tp_price=Decimal(str(event.get('take_profit'))) if event.get('take_profit') is not None else None,
                sl_price=Decimal(str(event.get('stop_loss'))) if event.get('stop_loss') is not None else None,
            )
            if order_result is None:
                logger.warning('OPEN order was not filled or failed — resetting position state')
                if self.strategy:
                    self.strategy.reset_position()
        elif event.get('action') == 'close':
            close_side = 'long' if event.get('side') == 'short' else 'short'
            close_size = Decimal(str(
                self.strategy.state.size if self.strategy and self.strategy.state else event.get('size', '0')
            ))
            exit_price = event.get('exit_price') or event.get('entry_price')
            order_result = await self.live_execution.execute_entry(
                side=close_side,
                size=close_size,
                limit_price=Decimal(str(exit_price)),
                tp_price=None,
                sl_price=None,
            )
            if order_result is None:
                logger.warning('CLOSE order was not filled or failed')

    async def initialize(self) -> None:
        if self.product_id is None:
            logger.info(
                'Resolving product_id for symbol=%s via %s/symbols',
                settings.symbol, settings.nado_rest_base,
            )
            product = await self.client.resolve_product(settings.symbol)
            self.product_id = int(product['product_id'])
            self.resolved_symbol = product['symbol']
            logger.info(
                'Resolved symbol=%s product_id=%s trading_status=%s type=%s '
                'maker_fee_x18=%s taker_fee_x18=%s',
                self.resolved_symbol, self.product_id,
                product.get('trading_status'), product.get('type'),
                product.get('maker_fee_rate_x18'), product.get('taker_fee_rate_x18'),
            )
        self._build_strategy(self.product_id, self.resolved_symbol)

    async def run(self) -> None:
        setup_logging()
        await self.initialize()
        health = self.healthcheck()
        logger.info('Healthcheck: %s', health)
        await notifier.send_startup(health)
        asyncio.create_task(self._periodic_summary_loop())
        asyncio.create_task(self._daily_summary_loop())
        if self.tg_control is not None:
            asyncio.create_task(self.tg_control.run())
        while True:
            try:
                logger.info(
                    'Starting stream loop symbol=%s product_id=%s ws=%s exec_style=%s',
                    self.resolved_symbol, self.product_id,
                    settings.nado_ws_base, settings.execution_style,
                )
                await self.client.stream_bbo(self.product_id, self.handle_ticker)
            except Exception as exc:
                logger.exception('WebSocket stream crashed, reconnecting soon')
                await notifier.send_error('Nado stream crashed', str(exc))
                await asyncio.sleep(settings.reconnect_delay_seconds)


if __name__ == '__main__':
    asyncio.run(NadoStage1V34Bot().run())
