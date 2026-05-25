import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

from client import NadoWsClient, scale_x18
from config import settings
from real_fee_adapter import RealFeeAdapter
from live_execution import LiveExecutionClient
from signer import WalletSigner
from strategy_v2 import TrendSignalStrategy
from telegram_control import TelegramControlBot
from telegram_notifier import notifier

logger = logging.getLogger(__name__)

PRICE_INCREMENT_BY_PRODUCT = {2: Decimal('1')}
CLOSE_REOPEN_COOLDOWN_SECONDS = 8
ENTRY_CONFIRM_TIMEOUT_SECONDS = 12
ENTRY_CONFIRM_POLL_SECONDS = 2
CLOSE_CONFIRM_DELAY_SECONDS = 6
ENTRY_FAILURE_COOLDOWN_SECONDS = 8


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
    logger.info('Logging initialized level=%s log_dir=%s pid=%s', settings.log_level, log_dir, os.getpid())


class NadoStage1V34Bot:
    def __init__(self) -> None:
        self.signer = WalletSigner(settings.private_key)
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
        self.live_execution = LiveExecutionClient(
            settings.nado_rest_base,
            settings.nado_archive_base,
            settings.private_key,
            settings.subaccount_name,
        ) if (not settings.read_only and not settings.dry_run) else None

        self.product_id: Optional[int] = settings.nado_product_id
        self.resolved_symbol: str = settings.symbol
        self.strategy: Optional[TrendSignalStrategy] = None
        self.tg_control: Optional[TelegramControlBot] = None

        self._ticker_count = 0
        self._scale = Decimal(str(settings.nado_price_scale))
        self._trading_halted_low_balance = False
        self._block_new_entries_until = 0.0
        # Trade lifecycle: single source of truth for one active trade
        self._active_trade: Optional[Dict[str, Any]] = None
        # Legacy pending_open kept for _confirm_pending_open loop compatibility
        self._pending_open: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ #
    #  Trade Lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def _set_active_trade(self, *, digest: str, side: str, price: Any, size: Any) -> None:
        self._active_trade = {
            'entry_digest': str(digest),
            'side': str(side),
            'price': str(price),
            'size': str(size),
            'state': 'entry_submitted',
            'created_at': time.time(),
        }
        logger.error('ACTIVE_TRADE_SET trade=%s', self._active_trade)

    def _update_active_trade_state(self, state: str, **extra: Any) -> None:
        if self._active_trade is None:
            return
        self._active_trade['state'] = str(state)
        for k, v in extra.items():
            if v is not None:
                self._active_trade[k] = v
        logger.error('ACTIVE_TRADE_UPDATED state=%s trade=%s', state, self._active_trade)

    def _clear_active_trade(self, reason: str) -> None:
        logger.error('ACTIVE_TRADE_CLEARED reason=%s trade=%s', reason, self._active_trade)
        self._active_trade = None

    def _has_live_trade(self) -> bool:
        if self._active_trade is None:
            return False
        state = str(self._active_trade.get('state') or '').lower()
        return state in {'entry_submitted', 'entry_pending', 'in_position', 'close_submitted'}

    # ------------------------------------------------------------------ #
    #  Strategy build                                                      #
    # ------------------------------------------------------------------ #

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
        )
        self.tg_control = TelegramControlBot(self.strategy)

    @staticmethod
    def _round_to_increment(value: Decimal, increment: Decimal) -> Decimal:
        if increment <= 0:
            return value
        steps = (value / increment).to_integral_value(rounding=ROUND_DOWN)
        return steps * increment

    def _price_increment(self, product_id: Optional[int]) -> Decimal:
        return PRICE_INCREMENT_BY_PRODUCT.get(int(product_id or 0), Decimal('1'))

    def _extract_digest(self, order_result: Any) -> Optional[str]:
        if self.live_execution is None:
            return None
        return self.live_execution.extract_digest(order_result)

    def _log_order_status_if_possible(self, digest: Optional[str]) -> Optional[Dict[str, Any]]:
        if not digest or self.live_execution is None or self.product_id is None:
            return None
        try:
            order_info = self.live_execution.get_order(self.product_id, digest)
            logger.error('NADO_ORDER_STATUS digest=%s order=%s', digest, order_info)
            return order_info
        except Exception:
            logger.exception('Failed to query order status for digest=%s', digest)
            return None

    @staticmethod
    def _order_data(order_info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(order_info, dict):
            return {}
        data = order_info.get('data')
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _status_text(order_data: Dict[str, Any]) -> str:
        for key in ('status', 'order_status', 'state'):
            val = order_data.get(key)
            if val is not None:
                return str(val).lower()
        return ''

    @staticmethod
    def _decimal_or_none(value: Any) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    @staticmethod
    def _normalize_side(value: Any) -> Optional[str]:
        if value is None:
            return None
        s = str(value).strip().lower()
        if s in {'long', 'buy', 'bid', '1'}:
            return 'long'
        if s in {'short', 'sell', 'ask', '-1'}:
            return 'short'
        return None

    def _order_fill_state(self, order_info: Optional[Dict[str, Any]]) -> str:
        data = self._order_data(order_info)
        if not data:
            return 'unknown'

        amount = self._decimal_or_none(data.get('amount'))
        unfilled = self._decimal_or_none(data.get('unfilled_amount'))

        status = self._status_text(data)
        if status in {'filled', 'executed', 'closed'}:
            return 'filled'
        if status in {'cancelled', 'canceled', 'expired', 'rejected'}:
            return status

        if amount is None or unfilled is None:
            return 'unknown'
        if unfilled == 0:
            return 'filled'
        if abs(unfilled) < abs(amount):
            return 'partial'
        if abs(unfilled) == abs(amount):
            return 'open'
        return 'unknown'

    def _is_order_filled(self, order_info: Optional[Dict[str, Any]], expected_size: Decimal) -> bool:
        return self._order_fill_state(order_info) == 'filled'

    def _rebuild_levels_from_entry(self, side: str, entry: Decimal) -> Dict[str, Optional[Decimal]]:
        stop_loss = None
        take_profit = None
        trailing_distance = Decimal(str(settings.trailing_distance)) if getattr(settings, 'trailing_distance', None) not in (None, '') else None

        if side == 'long':
            if getattr(settings, 'stop_loss_pct', None) not in (None, ''):
                stop_loss = entry * (Decimal('1') - Decimal(str(settings.stop_loss_pct)) / Decimal('100'))
            elif getattr(settings, 'stop_loss_offset', None) not in (None, ''):
                stop_loss = entry - Decimal(str(settings.stop_loss_offset))

            if getattr(settings, 'take_profit_pct', None) not in (None, ''):
                take_profit = entry * (Decimal('1') + Decimal(str(settings.take_profit_pct)) / Decimal('100'))
            elif getattr(settings, 'take_profit_offset', None) not in (None, ''):
                take_profit = entry + Decimal(str(settings.take_profit_offset))
        else:
            if getattr(settings, 'stop_loss_pct', None) not in (None, ''):
                stop_loss = entry * (Decimal('1') + Decimal(str(settings.stop_loss_pct)) / Decimal('100'))
            elif getattr(settings, 'stop_loss_offset', None) not in (None, ''):
                stop_loss = entry + Decimal(str(settings.stop_loss_offset))

            if getattr(settings, 'take_profit_pct', None) not in (None, ''):
                take_profit = entry * (Decimal('1') - Decimal(str(settings.take_profit_pct)) / Decimal('100'))
            elif getattr(settings, 'take_profit_offset', None) not in (None, ''):
                take_profit = entry - Decimal(str(settings.take_profit_offset))

        return {
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'trailing_distance': trailing_distance,
        }

    def _sync_strategy_to_exchange_position(self, exchange_pos: Dict[str, Any], pending: Dict[str, Any]) -> None:
        if self.strategy is None:
            return

        raw_side = exchange_pos.get('side')
        side = self._normalize_side(raw_side) or self._normalize_side(pending.get('side'))
        if side is None:
            logger.error('SYNC_POSITION_UNKNOWN_SIDE exchange_pos=%s pending=%s', exchange_pos, pending)
            return

        size_val = exchange_pos.get('size')
        if size_val in (None, '', '0', 0):
            size_val = pending.get('size')
        size = Decimal(str(size_val or '0'))

        entry_val = (
            exchange_pos.get('entry_price')
            or exchange_pos.get('avg_entry_price')
            or exchange_pos.get('average_entry_price')
            or pending.get('price')
        )
        if entry_val in (None, ''):
            logger.error('SYNC_POSITION_NO_ENTRY exchange_pos=%s pending=%s', exchange_pos, pending)
            return

        entry = Decimal(str(entry_val))
        levels = self._rebuild_levels_from_entry(side, entry)

        self.strategy.state.entry_pending = False
        self.strategy.state.in_position = True
        self.strategy.state.side = side
        self.strategy.state.size = size
        self.strategy.state.entry_price = entry
        self.strategy.state.best_price = entry
        self.strategy.state.breakeven_price = entry
        self.strategy.state.cooldown_ticks_left = 0
        self.strategy.state.stop_loss = levels['stop_loss']
        self.strategy.state.take_profit = levels['take_profit']
        self.strategy.state.trailing_distance = levels['trailing_distance']

        logger.error(
            'SYNC_POSITION_OK side=%s entry=%s size=%s stop=%s take=%s trailing=%s raw_side=%s exchange_pos=%s pending=%s',
            side,
            entry,
            size,
            self.strategy.state.stop_loss,
            self.strategy.state.take_profit,
            self.strategy.state.trailing_distance,
            raw_side,
            exchange_pos,
            pending,
        )
    def _ensure_local_risk_levels(self, side: Any, entry_price: Any, size: Any = None) -> None:
        if self.strategy is None:
            return
        norm_side = self._normalize_side(side)
        if norm_side is None or entry_price in (None, ''):
            logger.error('ENSURE_LEVELS_SKIPPED side=%s entry_price=%s size=%s', side, entry_price, size)
            return

        entry = Decimal(str(entry_price))
        levels = self._rebuild_levels_from_entry(norm_side, entry)

        self.strategy.state.entry_pending = False
        self.strategy.state.in_position = True
        self.strategy.state.side = norm_side
        self.strategy.state.entry_price = entry
        if size not in (None, ''):
            self.strategy.state.size = Decimal(str(size))
        if self.strategy.state.best_price is None:
            self.strategy.state.best_price = entry
        if self.strategy.state.breakeven_price is None:
            self.strategy.state.breakeven_price = entry
        if self.strategy.state.stop_loss is None:
            self.strategy.state.stop_loss = levels['stop_loss']
        if self.strategy.state.take_profit is None:
            self.strategy.state.take_profit = levels['take_profit']
        if self.strategy.state.trailing_distance is None:
            self.strategy.state.trailing_distance = levels['trailing_distance']

        logger.error(
            'ENSURE_LEVELS_OK side=%s entry=%s size=%s stop=%s take=%s best=%s trailing=%s',
            self.strategy.state.side,
            self.strategy.state.entry_price,
            self.strategy.state.size,
            self.strategy.state.stop_loss,
            self.strategy.state.take_profit,
            self.strategy.state.best_price,
            self.strategy.state.trailing_distance,
        )    
    def _reset_strategy_position_state(self) -> None:
        if self.strategy is None:
            return
        self.strategy.state.entry_pending = False
        self.strategy.state.in_position = False
        self.strategy.state.side = None
        self.strategy.state.entry_price = None
        self.strategy.state.size = Decimal('0')
        self.strategy.state.stop_loss = None
        self.strategy.state.take_profit = None
        self.strategy.state.trailing_distance = None
        self.strategy.state.best_price = None
        self.strategy.state.breakeven_price = None
        self.strategy.state.entry_fee_bps = Decimal('0')

    # ------------------------------------------------------------------ #
    #  Entry confirmation loop                                            #
    # ------------------------------------------------------------------ #

    async def _confirm_pending_open(self) -> None:
        pending = self._pending_open
        if not pending or self.live_execution is None or self.product_id is None:
            return

        digest = str(pending['digest'])
        started_at = float(pending['started_at'])

        while self._pending_open and self._pending_open.get('digest') == digest:
            order_info = self._log_order_status_if_possible(digest)
            fill_state = self._order_fill_state(order_info)
            exchange_pos = self.live_execution.find_open_position(self.product_id)

            # 1) Best source of truth: exchange says position exists
            if exchange_pos is not None:
                self._sync_strategy_to_exchange_position(exchange_pos, pending)
                self._update_active_trade_state('in_position', confirmed_at=time.time())
                logger.error(
                    'ENTRY_CONFIRMED_EXCHANGE digest=%s fill_state=%s order=%s exchange_position=%s',
                    digest, fill_state, order_info, exchange_pos,
                )
                self._pending_open = None
                return

            # 2) Fallback: order explicitly filled, but position query may lag
            if fill_state == 'filled':
                self._sync_strategy_to_exchange_position(
                    {
                        'side': pending.get('side'),
                        'size': pending.get('size'),
                        'entry_price': pending.get('price'),
                    },
                    pending,
                )
                self._update_active_trade_state('in_position', confirmed_at=time.time())
                logger.error(
                    'ENTRY_CONFIRMED_BY_ORDER digest=%s fill_state=%s order=%s',
                    digest, fill_state, order_info,
                )
                self._pending_open = None
                return

            # 3) Definitive failure statuses
            if fill_state in {'cancelled', 'canceled', 'expired', 'rejected'}:
                logger.error(
                    'ENTRY_ENDED_WITHOUT_POSITION digest=%s fill_state=%s order=%s exchange_position=%s',
                    digest, fill_state, order_info, exchange_pos,
                )
                self._reset_strategy_position_state()
                self._clear_active_trade('entry_rejected_or_expired')
                self._pending_open = None
                self._block_new_entries_until = time.time() + ENTRY_FAILURE_COOLDOWN_SECONDS
                return

            # 4) Timeout without actual position = failed entry
            if time.time() - started_at >= ENTRY_CONFIRM_TIMEOUT_SECONDS:
                logger.error(
                    'ENTRY_CONFIRM_TIMEOUT_NO_POSITION digest=%s fill_state=%s order=%s exchange_position=%s',
                    digest, fill_state, order_info, exchange_pos,
                )
                self._reset_strategy_position_state()
                self._clear_active_trade('entry_timeout_no_position')
                self._pending_open = None
                self._block_new_entries_until = time.time() + ENTRY_FAILURE_COOLDOWN_SECONDS
                return

            await asyncio.sleep(ENTRY_CONFIRM_POLL_SECONDS)

    # ------------------------------------------------------------------ #
    #  Close confirmation                                                  #
    # ------------------------------------------------------------------ #

    async def _confirm_close_and_set_cooldown(self, digest: Optional[str]) -> None:
        if not digest or self.live_execution is None or self.product_id is None:
            return

        await asyncio.sleep(CLOSE_CONFIRM_DELAY_SECONDS)
        order_info = self._log_order_status_if_possible(digest)
        exchange_pos = self.live_execution.find_open_position(self.product_id)

        self._reset_strategy_position_state()
        self._clear_active_trade('close_confirmed')
        self._block_new_entries_until = time.time() + CLOSE_REOPEN_COOLDOWN_SECONDS
        logger.error('ENTRY_BLOCK_SET until=%s digest=%s', self._block_new_entries_until, digest)
        logger.error('CLOSE_CONFIRM_RESULT digest=%s data=%s exchange_position=%s', digest, self._order_data(order_info), exchange_pos)

    # ------------------------------------------------------------------ #
    #  Balance guard                                                       #
    # ------------------------------------------------------------------ #

    async def _check_balance_guard(self) -> None:
        balance = await self.real_fee_adapter.get_available_balance_usd()
        if balance is None or self.strategy is None:
            return

        threshold = Decimal(str(settings.min_wallet_balance_usd))
        if balance < threshold:
            if not self._trading_halted_low_balance:
                self.strategy.set_paused(True)
                self._trading_halted_low_balance = True
                logger.warning('LOW_BALANCE_GUARD_TRIGGERED available_balance_usd=%s threshold_usd=%s', balance, threshold)
                await notifier.send_error(
                    'Low balance guard triggered',
                    f'Available balance {balance} is below threshold {threshold}. Trading paused.'
                )
        elif self._trading_halted_low_balance:
            self._trading_halted_low_balance = False
            logger.info('LOW_BALANCE_GUARD_CLEARED available_balance_usd=%s threshold_usd=%s', balance, threshold)

    # ------------------------------------------------------------------ #
    #  Healthcheck                                                         #
    # ------------------------------------------------------------------ #

    def healthcheck(self) -> Dict[str, Any]:
        snap = self.strategy.snapshot_state() if self.strategy else {}
        return {
            'env': settings.nado_env,
            'configured_wallet': self.signer.address,
            'expected_wallet': settings.account_address,
            'subaccount_name': settings.subaccount_name,
            'symbol': self.resolved_symbol,
            'product_id': self.product_id,
            'ws_base': settings.nado_ws_base,
            'rest_base': settings.nado_rest_base,
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
            'real_fee_sync': getattr(settings, 'enable_real_fee_sync', False),
            'log_level': settings.log_level,
            'log_ticker_every': settings.log_ticker_every,
            'price_scale': settings.nado_price_scale,
            'volume_30d_usd': snap.get('volume_30d_usd'),
            'fee_tier': snap.get('fee_tier'),
            'maker_bps': snap.get('maker_bps'),
            'taker_bps': snap.get('taker_bps'),
            'active_trade': self._active_trade,
            'pending_open': self._pending_open,
        }

    # ------------------------------------------------------------------ #
    #  Summary loops                                                       #
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    #  Ticker handler                                                      #
    # ------------------------------------------------------------------ #

    async def handle_ticker(self, msg: Dict[str, Any]) -> None:
        if msg.get('result') is None and msg.get('id') is not None:
            logger.info('Subscription confirmed: %s', msg)
            return

        data = msg.get('data') or msg.get('result') or msg
        if not isinstance(data, dict):
            logger.debug('Non-dict event: %s', msg)
            return

        event_type = data.get('type') or msg.get('type') or (
            (msg.get('stream') or {}).get('type') if isinstance(msg.get('stream'), dict) else None
        )
        if event_type and event_type != 'best_bid_offer':
            logger.debug('Ignoring non-BBO event: %s', msg)
            return

        event_product_id = data.get('product_id') or data.get('productId') or data.get('pid')
        if self.product_id is not None and event_product_id is not None and int(event_product_id) != int(self.product_id):
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
            logger.info('Ticker %s product_id=%s mark=%s bid=%s ask=%s raw=%s',
                        self.resolved_symbol, self.product_id, mark_px, bid_px, ask_px, data)

        if self.strategy is None:
            logger.warning('Strategy not initialized yet; dropping ticker')
            return

        # Block all activity while active trade is live (except close signals)
        if self._has_live_trade():
            trade_state = str((self._active_trade or {}).get('state', ''))
            if trade_state in ('entry_submitted', 'entry_pending'):
                logger.info('TRADE_ENTRY_IN_FLIGHT trade=%s', self._active_trade)
                return
            # in_position or close_submitted: pass through to allow close signal processing
            if trade_state == 'close_submitted':
                logger.info('TRADE_CLOSE_IN_FLIGHT trade=%s', self._active_trade)
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

        # Hard block: never open if trade already live
        if event.get('action') == 'open' and self._has_live_trade():
            logger.warning('OPEN_BLOCKED_LIVE_TRADE trade=%s event=%s', self._active_trade, event)
            return

        if event.get('action') == 'open' and time.time() < self._block_new_entries_until:
            logger.warning('OPEN_BLOCKED_COOLDOWN cooldown_until=%s now=%s event=%s',
                           self._block_new_entries_until, time.time(), event)
            return

        logger.warning('SIGNAL %s', event)
        await notifier.send_signal(event)

        if settings.read_only:
            logger.info('READ ONLY mode active, no order sent')
            return
        if settings.dry_run:
            logger.info('DRY RUN mode active, live credentials loaded but no order sent')
            return
        if self.live_execution is None:
            logger.error('LIVE execution requested but client is not initialized')
            return

        # ---- OPEN ----
        if event.get('action') == 'open':
            if self.strategy is not None:
                self.strategy.state.entry_pending = True
            try:
                order_result = self.live_execution.place_order(
                    product_id=self.product_id,
                    side=event.get('side'),
                    price=Decimal(str(event.get('entry_price'))),
                    size=Decimal(str(event.get('size'))),
                    post_only=False if settings.execution_style == 'taker' else (settings.execution_style == 'maker'),
                    reduce_only=False,
                )
            except Exception:
                logger.exception('OPEN_ORDER_FAILED event=%s', event)
                self._reset_strategy_position_state()
                self._block_new_entries_until = time.time() + ENTRY_FAILURE_COOLDOWN_SECONDS
                return

            digest = self._extract_digest(order_result)
            self._log_order_status_if_possible(digest)

            order_text = str(order_result)
            if 'Insufficient account health' in order_text:
                logger.error('ENTRY_REJECTED_ACCOUNT_HEALTH order_result=%s event=%s', order_result, event)
                self._reset_strategy_position_state()
                self._block_new_entries_until = time.time() + ENTRY_FAILURE_COOLDOWN_SECONDS
                return

            if digest:
                self._set_active_trade(
                    digest=digest,
                    side=event.get('side'),
                    price=event.get('entry_price'),
                    size=event.get('size'),
                )
                self._update_active_trade_state('entry_pending')
                self._pending_open = {
                    'digest': digest,
                    'side': event.get('side'),
                    'price': str(event.get('entry_price')),
                    'size': str(event.get('size')),
                    'started_at': time.time(),
                }
                logger.error(
                    'ENTRY_ORDER_SUBMITTED digest=%s side=%s price=%s size=%s local_side=%s local_entry=%s local_stop=%s local_take=%s local_best=%s local_in_position=%s',
                    digest, event.get('side'), event.get('entry_price'), event.get('size'),
                    getattr(self.strategy.state, 'side', None) if self.strategy else None,
                    getattr(self.strategy.state, 'entry_price', None) if self.strategy else None,
                    getattr(self.strategy.state, 'stop_loss', None) if self.strategy else None,
                    getattr(self.strategy.state, 'take_profit', None) if self.strategy else None,
                    getattr(self.strategy.state, 'best_price', None) if self.strategy else None,
                    getattr(self.strategy.state, 'in_position', None) if self.strategy else None,
                )
                asyncio.create_task(self._confirm_pending_open())
            else:
                logger.error('ENTRY_ORDER_NO_DIGEST order_result=%s event=%s', order_result, event)
                self._reset_strategy_position_state()
                self._block_new_entries_until = time.time() + ENTRY_FAILURE_COOLDOWN_SECONDS
                return

        # ---- CLOSE ----
        elif event.get('action') == 'close':
            state_size = Decimal(str(self.strategy.state.size if self.strategy else '0'))
            event_size = Decimal(str(event.get('size', '0') or '0'))
            close_size = state_size if state_size > 0 else event_size

            if close_size <= 0:
                logger.error(
                    'SKIP_CLOSE zero_size state_size=%s event_size=%s event=%s',
                    state_size, event_size, event
                )
                self._block_new_entries_until = time.time() + CLOSE_REOPEN_COOLDOWN_SECONDS
                return

            close_side = 'short' if event.get('side') == 'long' else 'long'
            price_increment = self._price_increment(self.product_id)

            if close_side == 'long':
                raw_close_price = ask_px if ask_px is not None else Decimal(str(event.get('exit_price')))
                close_price = self._round_to_increment(raw_close_price + price_increment, price_increment)
            else:
                raw_close_price = bid_px if bid_px is not None else Decimal(str(event.get('exit_price')))
                close_price = self._round_to_increment(raw_close_price - price_increment, price_increment)

            if close_price <= 0:
                close_price = price_increment

            logger.error(
                'CLOSE_ATTEMPT side=%s close_side=%s state_size=%s event_size=%s close_size=%s raw_exit_price=%s close_price=%s bid=%s ask=%s increment=%s',
                event.get('side'), close_side, state_size, event_size, close_size,
                event.get('exit_price'), close_price, bid_px, ask_px, price_increment,
            )

            try:
                order_result = self.live_execution.place_order(
                    product_id=self.product_id,
                    side=close_side,
                    price=close_price,
                    size=close_size,
                    post_only=False,
                    ioc=True,
                    reduce_only=True,
                )
            except Exception:
                logger.exception('CLOSE_ORDER_FAILED event=%s', event)
                return

            digest = self._extract_digest(order_result)
            self._update_active_trade_state('close_submitted', close_digest=digest, close_started_at=time.time())
            self._log_order_status_if_possible(digest)
            asyncio.create_task(self._confirm_close_and_set_cooldown(digest))
            asyncio.create_task(self._confirm_close_and_set_cooldown(digest))

    # ------------------------------------------------------------------ #
    #  Initialization & run                                               #
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        if self.product_id is None:
            logger.info('Resolving product_id for symbol=%s via %s/symbols', settings.symbol, settings.nado_rest_base)
            product = await self.client.resolve_product(settings.symbol)
            self.product_id = int(product['product_id'])
            self.resolved_symbol = product['symbol']
            logger.info(
                'Resolved symbol=%s product_id=%s trading_status=%s type=%s maker_fee_x18=%s taker_fee_x18=%s',
                self.resolved_symbol,
                self.product_id,
                product.get('trading_status'),
                product.get('type'),
                product.get('maker_fee_rate_x18'),
                product.get('taker_fee_rate_x18'),
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
                    self.resolved_symbol, self.product_id, settings.nado_ws_base, settings.execution_style
                )
                await self.client.stream_bbo(self.product_id, self.handle_ticker)
            except Exception as exc:
                logger.exception('WebSocket stream crashed, reconnecting soon')
                await notifier.send_error('Nado stream crashed', str(exc))
                await asyncio.sleep(settings.reconnect_delay_seconds)


if __name__ == '__main__':
    asyncio.run(NadoStage1V34Bot().run())
