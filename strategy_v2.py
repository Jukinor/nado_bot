import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Deque, Dict, Optional

from fees import resolve_fee_tier
from state import PositionState
from telegram_notifier import notifier

logger = logging.getLogger(__name__)

BPS_DIVISOR = Decimal('10000')
PCT_DIVISOR = Decimal('100')


@dataclass
class DayStats:
    date_key: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: Decimal = Decimal('0')
    fees: Decimal = Decimal('0')
    net_pnl: Decimal = Decimal('0')


class TrendSignalStrategy:
    def __init__(
        self,
        symbol: str,
        product_id: int,
        execution_style: str,
        order_size: str,
        short_window: int,
        long_window: int,
        min_edge_bps: str,
        stop_loss_offset: Optional[str],
        take_profit_offset: Optional[str],
        trailing_distance: Optional[str],
        cooldown_ticks: int,
        use_book_prices: bool,
        leverage: str,
        commission_maker_bps: str,
        commission_taker_bps: str,
        stop_loss_pct: Optional[str],
        take_profit_pct: Optional[str],
        trailing_pct: Optional[str],
        volume_30d_usd: str,
    ) -> None:
        self.symbol = symbol
        self.product_id = product_id
        self.execution_style = execution_style
        self.order_size = Decimal(str(order_size))
        self.short_window = short_window
        self.long_window = long_window
        self.min_edge_bps = Decimal(str(min_edge_bps))
        self.stop_loss_offset = Decimal(str(stop_loss_offset)) if stop_loss_offset not in (None, '') else None
        self.take_profit_offset = Decimal(str(take_profit_offset)) if take_profit_offset not in (None, '') else None
        self.trailing_distance = Decimal(str(trailing_distance)) if trailing_distance not in (None, '') else None
        self.cooldown_ticks = cooldown_ticks
        self.use_book_prices = use_book_prices
        self.leverage = Decimal(str(leverage))
        self.default_commission_maker_bps = Decimal(str(commission_maker_bps))
        self.default_commission_taker_bps = Decimal(str(commission_taker_bps))
        self.stop_loss_pct = Decimal(str(stop_loss_pct)) if stop_loss_pct not in (None, '') else None
        self.take_profit_pct = Decimal(str(take_profit_pct)) if take_profit_pct not in (None, '') else None
        self.trailing_pct = Decimal(str(trailing_pct)) if trailing_pct not in (None, '') else None
        self.volume_30d_usd = Decimal(str(volume_30d_usd))

        self.state = PositionState()
        self.prices: Deque[Decimal] = deque(maxlen=max(long_window, short_window) + 5)
        self.paused = False
        self.manual_close_requested = False
        self.day_stats = DayStats(date_key=self._today_key())

        self._last_bid_qty = Decimal('0')
        self._last_ask_qty = Decimal('0')

    def _today_key(self) -> str:
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')

    def _roll_day(self) -> None:
        today = self._today_key()
        if self.day_stats.date_key != today:
            self.day_stats = DayStats(date_key=today)

    def current_fee_tier(self):
        return resolve_fee_tier(self.volume_30d_usd)

    def current_fee_bps(self, style: Optional[str] = None) -> Decimal:
        tier = self.current_fee_tier()
        style = style or self.execution_style
        if style == 'maker':
            return tier.maker_bps if tier.maker_bps is not None else self.default_commission_maker_bps
        return tier.taker_bps if tier.taker_bps is not None else self.default_commission_taker_bps

    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    def request_manual_close(self) -> None:
        self.manual_close_requested = True

    def snapshot_state(self) -> Dict[str, str]:
        self._roll_day()
        fee_tier = self.current_fee_tier()
        return {
            'symbol': self.symbol,
            'product_id': str(self.product_id),
            'execution_style': str(self.execution_style),
            'fee_tier': fee_tier.name,
            'maker_bps': str(fee_tier.maker_bps),
            'taker_bps': str(fee_tier.taker_bps),
            'volume_30d_usd': str(self.volume_30d_usd),
            'in_position': str(self.state.in_position),
            'entry_pending': str(self.state.entry_pending),
            'side': str(self.state.side),
            'entry_price': str(self.state.entry_price),
            'size': str(self.state.size),
            'stop_loss': str(self.state.stop_loss),
            'take_profit': str(self.state.take_profit),
            'best_price': str(self.state.best_price),
            'breakeven_price': str(self.state.breakeven_price),
            'paused': str(self.paused),
            'cooldown_ticks_left': str(self.state.cooldown_ticks_left),
            'day_trades': str(self.day_stats.trades),
            'day_net_pnl': str(self.day_stats.net_pnl),
        }

    def _avg(self, items) -> Optional[Decimal]:
        items = list(items)
        if not items:
            return None
        return sum(items) / Decimal(len(items))

    def _edge_bps(self) -> Optional[Decimal]:
        if len(self.prices) < self.long_window:
            return None
        short_avg = self._avg(list(self.prices)[-self.short_window:])
        long_avg = self._avg(list(self.prices)[-self.long_window:])
        if short_avg is None or long_avg is None or long_avg == 0:
            return None
        return (short_avg - long_avg) / long_avg * BPS_DIVISOR

    def _entry_fee_bps(self) -> Decimal:
        return self.current_fee_bps(self.execution_style)

    def _exit_fee_bps(self, exit_style: str = 'taker') -> Decimal:
        return self.current_fee_bps(exit_style)

    def _round_trip_fee_bps(self, exit_style: str = 'taker') -> Decimal:
        return self._entry_fee_bps() + self._exit_fee_bps(exit_style)

    def _effective_edge_bps(self, edge_bps: Decimal, exit_style: str = 'taker') -> Decimal:
        return abs(edge_bps) - self._round_trip_fee_bps(exit_style)

    def _mid_price(self, bid_px: Decimal, ask_px: Decimal) -> Decimal:
        return (bid_px + ask_px) / Decimal('2')

    def _spread_bps(self, bid_px: Decimal, ask_px: Decimal) -> Decimal:
        if bid_px is None or ask_px is None:
            return Decimal('0')
        mid = self._mid_price(bid_px, ask_px)
        if mid <= 0:
            return Decimal('0')
        return ((ask_px - bid_px) / mid) * Decimal('10000')

    def _l1_imbalance(self, bid_qty: Decimal, ask_qty: Decimal) -> Decimal:
        denom = bid_qty + ask_qty
        if denom <= 0:
            return Decimal('0')
        return (bid_qty - ask_qty) / denom

    def _entry_score_bps(self, raw_edge_bps: Decimal, imbalance: Decimal) -> Decimal:
        return abs(raw_edge_bps) + (abs(imbalance) * Decimal('10'))

    def update_l1_sizes(self, bid_qty: Decimal, ask_qty: Decimal) -> None:
        self._last_bid_qty = bid_qty
        self._last_ask_qty = ask_qty

    def _pick_entry_price(self, mark_px: Decimal, bid_px: Optional[Decimal], ask_px: Optional[Decimal], side: str) -> Decimal:
        if not self.use_book_prices:
            return mark_px
        if self.execution_style == 'maker':
            if side == 'long' and bid_px is not None:
                return bid_px
            if side == 'short' and ask_px is not None:
                return ask_px
        else:
            if side == 'long' and ask_px is not None:
                return ask_px
            if side == 'short' and bid_px is not None:
                return bid_px
        return mark_px

    def _price_from_pct(self, entry: Decimal, pct: Decimal, is_profit: bool, side: str) -> Decimal:
        move = entry * (pct / PCT_DIVISOR)
        if side == 'long':
            return entry + move if is_profit else entry - move
        return entry - move if is_profit else entry + move

    def _build_protective_levels(self, side: str, entry_price: Decimal) -> Dict[str, Optional[Decimal]]:
        if self.stop_loss_pct is not None:
            stop_loss = self._price_from_pct(entry_price, self.stop_loss_pct, False, side)
        else:
            stop_loss = entry_price - (self.stop_loss_offset or Decimal('0')) if side == 'long' else entry_price + (self.stop_loss_offset or Decimal('0'))

        if self.take_profit_pct is not None:
            take_profit = self._price_from_pct(entry_price, self.take_profit_pct, True, side)
        else:
            take_profit = entry_price + (self.take_profit_offset or Decimal('0')) if side == 'long' else entry_price - (self.take_profit_offset or Decimal('0'))

        return {
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'trailing_distance': self.trailing_distance,
        }

    def _open_position(
        self,
        side: str,
        entry_price: Decimal,
        edge_bps: Decimal,
        effective_edge_bps: Decimal,
        exit_style: str = 'taker',
    ) -> Dict[str, str]:
        fee_tier = self.current_fee_tier()
        entry_fee_bps = self.current_fee_bps(self.execution_style)
        levels = self._build_protective_levels(side, entry_price)

        return {
            'action': 'open',
            'side': side,
            'symbol': self.symbol,
            'product_id': str(self.product_id),
            'exec_style': self.execution_style,
            'entry_price': str(entry_price),
            'size': str(self.order_size),
            'stop_loss': str(levels['stop_loss']),
            'take_profit': str(levels['take_profit']),
            'edge_bps': str(edge_bps),
            'effective_edge_bps': str(effective_edge_bps),
            'entry_fee_bps': str(entry_fee_bps),
            'exit_fee_bps': str(self._exit_fee_bps(exit_style)),
            'round_trip_fee_bps': str(self._round_trip_fee_bps(exit_style)),
            'entry_exec_style': self.execution_style,
            'exit_exec_style': exit_style,
            'fee_tier': fee_tier.name,
            'mode': 'percent' if self.stop_loss_pct is not None or self.take_profit_pct is not None else 'absolute',
            'trailing_distance': str(levels['trailing_distance']) if levels['trailing_distance'] is not None else '',
        }

    def _update_trailing(self, mark_px: Decimal) -> None:
        if not self.state.in_position or self.state.entry_price is None or self.state.side is None:
            return

        if self.state.side == 'long':
            if self.state.best_price is None or mark_px > self.state.best_price:
                self.state.best_price = mark_px
            if self.trailing_pct is not None and self.state.best_price is not None:
                candidate = self.state.best_price * (Decimal('1') - self.trailing_pct / PCT_DIVISOR)
                if self.state.stop_loss is None or candidate > self.state.stop_loss:
                    self.state.stop_loss = candidate
        else:
            if self.state.best_price is None or mark_px < self.state.best_price:
                self.state.best_price = mark_px
            if self.trailing_pct is not None and self.state.best_price is not None:
                candidate = self.state.best_price * (Decimal('1') + self.trailing_pct / PCT_DIVISOR)
                if self.state.stop_loss is None or candidate < self.state.stop_loss:
                    self.state.stop_loss = candidate

    def _unrealized_pnl(self, mark_px: Decimal) -> Dict[str, Decimal]:
        if not self.state.in_position or self.state.entry_price is None or self.state.side is None:
            return {'gross': Decimal('0'), 'fees_est': Decimal('0'), 'net': Decimal('0')}

        entry = self.state.entry_price
        size = self.state.size
        notional = entry * size * self.leverage

        if self.state.side == 'long':
            gross = (mark_px - entry) * size * self.leverage
        else:
            gross = (entry - mark_px) * size * self.leverage

        fee_bps = self.current_fee_bps(self.execution_style)
        fees_est = notional * (fee_bps / BPS_DIVISOR) * Decimal('2')
        return {'gross': gross, 'fees_est': fees_est, 'net': gross - fees_est}

    def _close_position(self, exit_price: Decimal, reason: str) -> Dict[str, str]:
        side = self.state.side or 'flat'
        entry = self.state.entry_price or Decimal('0')
        size = self.state.size
        notional = entry * size * self.leverage

        if side == 'long':
            gross = (exit_price - entry) * size * self.leverage
        else:
            gross = (entry - exit_price) * size * self.leverage

        fee_bps = self.current_fee_bps(self.execution_style)
        fees = notional * (fee_bps / BPS_DIVISOR) * Decimal('2')
        net = gross - fees

        self._roll_day()
        self.day_stats.trades += 1
        self.day_stats.gross_pnl += gross
        self.day_stats.fees += fees
        self.day_stats.net_pnl += net
        if net >= 0:
            self.day_stats.wins += 1
        else:
            self.day_stats.losses += 1

        result = {
            'action': 'close',
            'side': side,
            'symbol': self.symbol,
            'product_id': str(self.product_id),
            'exec_style': self.execution_style,
            'entry_price': str(entry),
            'exit_price': str(exit_price),
            'size': str(size),
            'gross_pnl': str(gross),
            'fees': str(fees),
            'net_pnl': str(net),
            'reason': reason,
            'day_trades': str(self.day_stats.trades),
            'day_net_pnl': str(self.day_stats.net_pnl),
            'fee_tier': self.current_fee_tier().name,
            'fee_bps': str(fee_bps),
        }

        self.state = PositionState(cooldown_ticks_left=self.cooldown_ticks)
        self.manual_close_requested = False
        return result

    async def send_period_summary(self, period: str) -> None:
        fee_tier = self.current_fee_tier()
        lines = [
            f"Symbol: `{self.symbol}` product_id=`{self.product_id}`",
            f"Exec style: `{self.execution_style}` Tier: `{fee_tier.name}`",
            f"30d volume: `{self.volume_30d_usd}` Maker/Taker bps: `{fee_tier.maker_bps}` / `{fee_tier.taker_bps}`",
            f"Trades: `{self.day_stats.trades}` Wins/Losses: `{self.day_stats.wins}` / `{self.day_stats.losses}`",
            f"Gross/Fee/Net: `{self.day_stats.gross_pnl}` / `{self.day_stats.fees}` / `{self.day_stats.net_pnl}`",
        ]

        logger.info(
            '%s summary | symbol=%s product_id=%s exec_style=%s tier=%s maker_bps=%s taker_bps=%s trades=%s gross=%s fees=%s net=%s',
            period.upper(),
            self.symbol,
            self.product_id,
            self.execution_style,
            fee_tier.name,
            fee_tier.maker_bps,
            fee_tier.taker_bps,
            self.day_stats.trades,
            self.day_stats.gross_pnl,
            self.day_stats.fees,
            self.day_stats.net_pnl,
        )
        await notifier.send_summary(f'Nado {period.title()} Summary', lines)
        await asyncio.sleep(0)

    def on_ticker(self, mark_px: Decimal, bid_px: Optional[Decimal], ask_px: Optional[Decimal]):
        self._roll_day()
        self.prices.append(mark_px)
        fee_tier = self.current_fee_tier()
        active_fee_bps = self.current_fee_bps(self.execution_style)
        logger.info(
            'STRATEGY_TICK symbol=%s product_id=%s exec_style=%s price=%s bid=%s ask=%s history=%s in_position=%s entry_pending=%s paused=%s cooldown=%s fee_tier=%s active_fee_bps=%s volume_30d=%s',
            self.symbol, self.product_id, self.execution_style, mark_px, bid_px, ask_px,
            len(self.prices), self.state.in_position, self.state.entry_pending, self.paused, self.state.cooldown_ticks_left,
            fee_tier.name, active_fee_bps, self.volume_30d_usd,
        )

        if self.state.entry_pending:
            logger.info(
                'ENTRY_PENDING_BLOCKED symbol=%s product_id=%s',
                self.symbol, self.product_id,
            )
            return None

        if self.state.in_position:
            self._update_trailing(mark_px)
            upnl = self._unrealized_pnl(mark_px)
            logger.info(
                'POSITION_MANAGE symbol=%s product_id=%s side=%s entry=%s mark=%s stop=%s take=%s best=%s upnl_gross=%s upnl_fees_est=%s upnl_net=%s fee_tier=%s',
                self.symbol, self.product_id, self.state.side, self.state.entry_price,
                mark_px, self.state.stop_loss, self.state.take_profit, self.state.best_price,
                upnl['gross'], upnl['fees_est'], upnl['net'], fee_tier.name,
            )

            if self.manual_close_requested:
                return self._close_position(mark_px, 'manual_close')

            if self.state.side == 'long':
                if self.state.take_profit is not None and mark_px >= self.state.take_profit:
                    return self._close_position(mark_px, 'take_profit')
                if self.state.stop_loss is not None and mark_px <= self.state.stop_loss:
                    return self._close_position(mark_px, 'stop_loss')
            else:
                if self.state.take_profit is not None and mark_px <= self.state.take_profit:
                    return self._close_position(mark_px, 'take_profit')
                if self.state.stop_loss is not None and mark_px >= self.state.stop_loss:
                    return self._close_position(mark_px, 'stop_loss')
            return None

        if self.paused or self.state.cooldown_ticks_left > 0:
            logger.info(
                'ENTRY_BLOCKED symbol=%s product_id=%s paused=%s cooldown=%s',
                self.symbol, self.product_id, self.paused, self.state.cooldown_ticks_left,
            )
            return None

        edge = self._edge_bps()
        if edge is None:
            logger.info(
                'EDGE_WAIT symbol=%s product_id=%s collected=%s need=%s',
                self.symbol, self.product_id, len(self.prices), self.long_window,
            )
            return None

        planned_exit_style = 'taker'
        entry_fee_bps = self._entry_fee_bps()
        exit_fee_bps = self._exit_fee_bps(planned_exit_style)
        round_trip_fee_bps = self._round_trip_fee_bps(planned_exit_style)
        effective_edge = self._effective_edge_bps(edge, planned_exit_style)
        bid_qty = getattr(self, '_last_bid_qty', Decimal('0'))
        ask_qty = getattr(self, '_last_ask_qty', Decimal('0'))
        imbalance = self._l1_imbalance(bid_qty, ask_qty)
        spread_bps = self._spread_bps(bid_px, ask_px)
        entry_score_bps = self._entry_score_bps(edge, imbalance)
        min_spread_bps = Decimal('1.0')
        score_threshold_bps = self.min_edge_bps
        logger.info(
            'EDGE_CHECK symbol=%s product_id=%s raw_edge_bps=%s effective_edge_bps=%s entry_score_bps=%s imbalance=%s spread_bps=%s entry_fee_bps=%s exit_fee_bps=%s round_trip_fee_bps=%s threshold=%s entry_exec_style=%s exit_exec_style=%s fee_tier=%s',
            self.symbol, self.product_id, edge, effective_edge, entry_score_bps, imbalance, spread_bps,
            entry_fee_bps, exit_fee_bps, round_trip_fee_bps, score_threshold_bps,
            'maker', planned_exit_style, fee_tier.name,
        )

        long_ok = edge > 0 and imbalance > Decimal('0.10') and spread_bps >= min_spread_bps and entry_score_bps >= score_threshold_bps
        short_ok = edge < 0 and imbalance < Decimal('-0.10') and spread_bps >= min_spread_bps and entry_score_bps >= score_threshold_bps

        if long_ok:
            entry = self._pick_entry_price(mark_px, bid_px, ask_px, 'long')
            logger.info(
                'OPEN long symbol=%s product_id=%s raw_edge_bps=%s effective_edge_bps=%s entry_score_bps=%s imbalance=%s spread_bps=%s entry_fee_bps=%s exit_fee_bps=%s round_trip_fee_bps=%s entry=%s entry_exec_style=%s exit_exec_style=%s fee_tier=%s',
                self.symbol, self.product_id, edge, effective_edge, entry_score_bps, imbalance, spread_bps,
                entry_fee_bps, exit_fee_bps, round_trip_fee_bps, entry, 'maker', planned_exit_style, fee_tier.name,
            )
            return self._open_position('long', entry, edge, effective_edge, planned_exit_style)

        if short_ok:
            entry = self._pick_entry_price(mark_px, bid_px, ask_px, 'short')
            logger.info(
                'OPEN short symbol=%s product_id=%s raw_edge_bps=%s effective_edge_bps=%s entry_score_bps=%s imbalance=%s spread_bps=%s entry_fee_bps=%s exit_fee_bps=%s round_trip_fee_bps=%s entry=%s entry_exec_style=%s exit_exec_style=%s fee_tier=%s',
                self.symbol, self.product_id, edge, effective_edge, entry_score_bps, imbalance, spread_bps,
                entry_fee_bps, exit_fee_bps, round_trip_fee_bps, entry, 'maker', planned_exit_style, fee_tier.name,
            )
            return self._open_position('short', entry, edge, effective_edge, planned_exit_style)

        logger.info(
            'FLAT raw_edge_bps=%s effective_edge_bps=%s entry_score_bps=%s imbalance=%s spread_bps=%s entry_fee_bps=%s exit_fee_bps=%s round_trip_fee_bps=%s threshold=%s symbol=%s product_id=%s entry_exec_style=%s exit_exec_style=%s fee_tier=%s',
            edge, effective_edge, entry_score_bps, imbalance, spread_bps,
            entry_fee_bps, exit_fee_bps, round_trip_fee_bps, score_threshold_bps,
            self.symbol, self.product_id, 'maker', planned_exit_style, fee_tier.name,
        )
        return None
