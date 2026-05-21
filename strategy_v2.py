import logging
import time
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
    def __init__(self, symbol: str, product_id: int, execution_style: str, order_size: str, short_window: int, long_window: int, min_edge_bps: str, stop_loss_offset: Optional[str], take_profit_offset: Optional[str], trailing_distance: Optional[str], cooldown_ticks: int, use_book_prices: bool, leverage: str, commission_maker_bps: str, commission_taker_bps: str, stop_loss_pct: Optional[str], take_profit_pct: Optional[str], trailing_pct: Optional[str], volume_30d_usd: str, protect_net_positive_only: bool = False) -> None:
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
        self.protect_net_positive_only = protect_net_positive_only
        self.state = PositionState()
        self.prices: Deque[Decimal] = deque(maxlen=max(long_window, short_window) + 5)
        self.paused = False
        self.manual_close_requested = False
        self.day_stats = DayStats(date_key=self._today_key())
        self._last_bid_qty = Decimal('0')
        self._last_ask_qty = Decimal('0')
        self.min_decision_interval_ms = 200
        self._last_decision_ts = 0.0

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
        return tier.maker_bps if style == 'maker' else tier.taker_bps

    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    def request_manual_close(self) -> None:
        self.manual_close_requested = True

    def update_l1_sizes(self, bid_qty: Decimal, ask_qty: Decimal) -> None:
        self._last_bid_qty = bid_qty
        self._last_ask_qty = ask_qty

    def mark_entry_fee_actual(self, fee_usd: Decimal, digest: Optional[str] = None) -> None:
        if self.state.in_position:
            self.state.fee_actual_open = fee_usd
            if digest:
                self.state.entry_order_digest = digest

    def mark_exit_fee_actual(self, fee_usd: Decimal, digest: Optional[str] = None) -> None:
        if digest:
            self.state.exit_order_digest = digest
        self.state.fee_actual_close = fee_usd

    def snapshot_state(self) -> Dict[str, str]:
        self._roll_day()
        fee_tier = self.current_fee_tier()
        return {
            'symbol': self.symbol,
            'product_id': str(self.product_id),
            'execution_style': self.execution_style,
            'fee_tier': fee_tier.name,
            'maker_bps': str(fee_tier.maker_bps),
            'taker_bps': str(fee_tier.taker_bps),
            'volume_30d_usd': str(self.volume_30d_usd),
            'in_position': str(self.state.in_position),
            'side': str(self.state.side),
            'entry_price': str(self.state.entry_price),
            'size': str(self.state.size),
            'stop_loss': str(self.state.stop_loss),
            'take_profit': str(self.state.take_profit),
            'best_price': str(self.state.best_price),
            'breakeven_price': str(self.state.breakeven_price),
            'breakeven_after_fees': str(self.state.breakeven_after_fees),
            'fee_actual_open': str(self.state.fee_actual_open),
            'fee_actual_close': str(self.state.fee_actual_close),
            'paused': str(self.paused),
            'cooldown_ticks_left': str(self.state.cooldown_ticks_left),
            'day_trades': str(self.day_stats.trades),
            'day_net_pnl': str(self.day_stats.net_pnl),
        }

    def _avg(self, items) -> Optional[Decimal]:
        items = list(items)
        return (sum(items) / Decimal(len(items))) if items else None

    def _edge_bps(self) -> Optional[Decimal]:
        if len(self.prices) < self.long_window:
            return None
        short_avg = self._avg(list(self.prices)[-self.short_window:])
        long_avg = self._avg(list(self.prices)[-self.long_window:])
        if short_avg is None or long_avg is None or long_avg == 0:
            return None
        return (short_avg - long_avg) / long_avg * BPS_DIVISOR

    def _entry_fee_bps(self) -> Decimal:
        return self.current_fee_bps('maker')

    def _exit_fee_bps(self, exit_style: str = 'taker') -> Decimal:
        return self.current_fee_bps(exit_style)

    def _round_trip_fee_bps(self, exit_style: str = 'taker') -> Decimal:
        return self._entry_fee_bps() + self._exit_fee_bps(exit_style)

    def _effective_edge_bps(self, edge_bps: Decimal, exit_style: str = 'taker') -> Decimal:
        return abs(edge_bps) - self._round_trip_fee_bps(exit_style)

    def _mid_price(self, bid_px: Decimal, ask_px: Decimal) -> Decimal:
        return (bid_px + ask_px) / Decimal('2')

    def _spread_bps(self, bid_px: Decimal, ask_px: Decimal) -> Decimal:
        mid = self._mid_price(bid_px, ask_px)
        return Decimal('0') if mid <= 0 else ((ask_px - bid_px) / mid) * Decimal('10000')

    def _l1_imbalance(self, bid_qty: Decimal, ask_qty: Decimal) -> Decimal:
        denom = bid_qty + ask_qty
        return Decimal('0') if denom <= 0 else (bid_qty - ask_qty) / denom

    def _entry_score_bps(self, raw_edge_bps: Decimal, imbalance: Decimal) -> Decimal:
        return abs(raw_edge_bps) + (abs(imbalance) * Decimal('100'))

    def _price_from_pct(self, base: Decimal, pct: Optional[Decimal], direction: str, kind: str) -> Optional[Decimal]:
        if pct is None:
            return None
        move = base * pct / PCT_DIVISOR
        if direction == 'long':
            return base - move if kind == 'sl' else base + move
        return base + move if kind == 'sl' else base - move
    def reset_position(self) -> None:
        self.state = PositionState()
        self.manual_close_requested = False

    def _trailing_distance_for(self, entry: Decimal) -> Optional[Decimal]:
        if self.trailing_distance is not None:
            return self.trailing_distance
        if self.trailing_pct is not None:
            return entry * self.trailing_pct / PCT_DIVISOR
        return None

    def _estimated_round_trip_fee_usd(self, entry_price: Decimal) -> Decimal:
        return entry_price * self.order_size * (self._round_trip_fee_bps() / BPS_DIVISOR)

    def _breakeven_after_fees(self, entry: Decimal, side: str) -> Decimal:
        fees = self._estimated_round_trip_fee_usd(entry)
        per_unit = fees / self.order_size if self.order_size > 0 else Decimal('0')
        return entry + per_unit if side == 'long' else entry - per_unit

    def _distance_to_breakeven(self, mark_px: Decimal) -> Optional[Decimal]:
        if not self.state.in_position or self.state.breakeven_after_fees is None:
            return None
        if self.state.side == 'long':
            return mark_px - self.state.breakeven_after_fees
        return self.state.breakeven_after_fees - mark_px

    def _open_position(self, side: str, entry: Decimal, edge_bps: Decimal, effective_edge_bps: Decimal, planned_exit_style: str):
        entry_fee_bps = self._entry_fee_bps()
        exit_fee_bps = self._exit_fee_bps(planned_exit_style)
        round_trip_fee_bps = self._round_trip_fee_bps(planned_exit_style)
        breakeven_after_fees = self._breakeven_after_fees(entry, side)
        self.state = PositionState(
            in_position=True,
            side=side,
            entry_price=entry,
            size=self.order_size,
            stop_loss=self._price_from_pct(entry, self.stop_loss_pct, side, 'sl'),
            take_profit=self._price_from_pct(entry, self.take_profit_pct, side, 'tp'),
            trailing_distance=self._trailing_distance_for(entry),
            best_price=entry,
            cooldown_ticks_left=0,
            breakeven_price=entry,
            breakeven_after_fees=breakeven_after_fees,
            entry_fee_bps=entry_fee_bps,
        )
        return {
            'action': 'open',
            'symbol': self.symbol,
            'product_id': self.product_id,
            'side': side,
            'entry_price': entry,
            'size': self.order_size,
            'stop_loss': self.state.stop_loss,
            'take_profit': self.state.take_profit,
            'edge_bps': edge_bps,
            'effective_edge_bps': effective_edge_bps,
            'entry_fee_bps': entry_fee_bps,
            'exit_fee_bps': exit_fee_bps,
            'round_trip_fee_bps': round_trip_fee_bps,
            'entry_exec_style': 'maker',
            'exit_exec_style': planned_exit_style,
            'fee_tier': self.current_fee_tier().name,
            'breakeven_after_fees': breakeven_after_fees,
        }

    def _update_trailing(self, mark_px: Decimal) -> None:
        if not self.state.in_position or self.state.trailing_distance is None:
            return
        if self.state.side == 'long':
            if self.state.best_price is None or mark_px > self.state.best_price:
                self.state.best_price = mark_px
                self.state.stop_loss = mark_px - self.state.trailing_distance
        else:
            if self.state.best_price is None or mark_px < self.state.best_price:
                self.state.best_price = mark_px
                self.state.stop_loss = mark_px + self.state.trailing_distance

    def _unrealized_pnl(self, mark_px: Decimal) -> Dict[str, Decimal]:
        if not self.state.in_position or self.state.entry_price is None:
            return {'gross': Decimal('0'), 'fees_est': Decimal('0'), 'fees_actual': Decimal('0'), 'net': Decimal('0')}
        side_mult = Decimal('1') if self.state.side == 'long' else Decimal('-1')
        gross = (mark_px - self.state.entry_price) * side_mult * self.state.size
        fees_est = self._estimated_round_trip_fee_usd(self.state.entry_price)
        fees_actual = (self.state.fee_actual_open or Decimal('0')) + (self.state.fee_actual_close or Decimal('0'))
        fees_used = fees_actual if fees_actual > 0 else fees_est
        return {'gross': gross, 'fees_est': fees_est, 'fees_actual': fees_actual, 'net': gross - fees_used}

    def _close_position(self, exit_price: Decimal, reason: str):
        side_mult = Decimal('1') if self.state.side == 'long' else Decimal('-1')
        gross = (exit_price - self.state.entry_price) * side_mult * self.state.size
        fees_est = self._estimated_round_trip_fee_usd(self.state.entry_price)
        fees_actual = (self.state.fee_actual_open or Decimal('0')) + (self.state.fee_actual_close or Decimal('0'))
        fees = fees_actual if fees_actual > 0 else fees_est
        net = gross - fees
        self.day_stats.trades += 1
        self.day_stats.gross_pnl += gross
        self.day_stats.fees += fees
        self.day_stats.net_pnl += net
        if net >= 0:
            self.day_stats.wins += 1
        else:
            self.day_stats.losses += 1
        event = {
            'action': 'close',
            'symbol': self.symbol,
            'product_id': self.product_id,
            'side': self.state.side,
            'reason': reason,
            'entry_price': self.state.entry_price,
            'exit_price': exit_price,
            'gross_pnl': gross,
            'fees': fees,
            'fees_est': fees_est,
            'fees_actual': fees_actual,
            'net_pnl': net,
            'day_trades': self.day_stats.trades,
            'day_net_pnl': self.day_stats.net_pnl,
        }
        self.state = PositionState(cooldown_ticks_left=self.cooldown_ticks)
        self.manual_close_requested = False
        return event

    async def send_period_summary(self, period: str) -> None:
        self._roll_day()
        lines = [
            f'trades={self.day_stats.trades}',
            f'wins={self.day_stats.wins}',
            f'losses={self.day_stats.losses}',
            f'gross={self.day_stats.gross_pnl}',
            f'fees={self.day_stats.fees}',
            f'net={self.day_stats.net_pnl}',
        ]
        await notifier.send_summary(f'{self.symbol} {period} summary', lines)

    def on_ticker(self, mark_px: Decimal, bid_px: Optional[Decimal], ask_px: Optional[Decimal]):
        self._roll_day()
        if self.state.cooldown_ticks_left > 0 and not self.state.in_position:
            self.state.cooldown_ticks_left -= 1
        self.prices.append(mark_px)
        now_ts = time.monotonic()
        if (now_ts - self._last_decision_ts) * 1000 < self.min_decision_interval_ms:
            return None
        self._last_decision_ts = now_ts
        if bid_px is None:
            bid_px = mark_px
        if ask_px is None:
            ask_px = mark_px
        fee_tier = self.current_fee_tier()

        if self.state.in_position:
            self._update_trailing(mark_px)
            upnl = self._unrealized_pnl(mark_px)
            distance = self._distance_to_breakeven(mark_px)
            logger.info('POSITION_MANAGE symbol=%s product_id=%s side=%s entry=%s mark=%s stop=%s take=%s best=%s breakeven_after_fees=%s distance_to_breakeven=%s upnl_gross=%s upnl_fees_est=%s upnl_fees_actual=%s upnl_net=%s fee_tier=%s', self.symbol, self.product_id, self.state.side, self.state.entry_price, mark_px, self.state.stop_loss, self.state.take_profit, self.state.best_price, self.state.breakeven_after_fees, distance, upnl['gross'], upnl['fees_est'], upnl['fees_actual'], upnl['net'], fee_tier.name)
            if self.manual_close_requested:
                if self.protect_net_positive_only and upnl['net'] <= 0:
                    logger.info('MANUAL_CLOSE_BLOCKED_NET_NEGATIVE symbol=%s product_id=%s net=%s', self.symbol, self.product_id, upnl['net'])
                    return None
                return self._close_position(mark_px, 'manual_close')
            if self.state.side == 'long':
                if self.state.take_profit is not None and mark_px >= self.state.take_profit:
                    if self.protect_net_positive_only and upnl['net'] <= 0:
                        return None
                    return self._close_position(mark_px, 'take_profit')
                if self.state.stop_loss is not None and mark_px <= self.state.stop_loss:
                    return self._close_position(mark_px, 'stop_loss')
            else:
                if self.state.take_profit is not None and mark_px <= self.state.take_profit:
                    if self.protect_net_positive_only and upnl['net'] <= 0:
                        return None
                    return self._close_position(mark_px, 'take_profit')
                if self.state.stop_loss is not None and mark_px >= self.state.stop_loss:
                    return self._close_position(mark_px, 'stop_loss')
            return None

        if self.paused or self.state.cooldown_ticks_left > 0:
            logger.info('ENTRY_BLOCKED symbol=%s product_id=%s paused=%s cooldown=%s', self.symbol, self.product_id, self.paused, self.state.cooldown_ticks_left)
            return None

        edge = self._edge_bps()
        if edge is None:
            logger.info('EDGE_WAIT symbol=%s product_id=%s collected=%s need=%s', self.symbol, self.product_id, len(self.prices), self.long_window)
            return None

        planned_exit_style = 'taker'
        entry_fee_bps = self._entry_fee_bps()
        exit_fee_bps = self._exit_fee_bps(planned_exit_style)
        round_trip_fee_bps = self._round_trip_fee_bps(planned_exit_style)
        effective_edge = self._effective_edge_bps(edge, planned_exit_style)
        imbalance = self._l1_imbalance(self._last_bid_qty, self._last_ask_qty)
        spread_bps = self._spread_bps(bid_px, ask_px)
        entry_score_bps = self._entry_score_bps(edge, imbalance)
        min_spread_bps = Decimal('0.05')
        score_threshold_bps = self.min_edge_bps
        logger.info('EDGE_CHECK symbol=%s product_id=%s raw_edge_bps=%s effective_edge_bps=%s entry_score_bps=%s imbalance=%s spread_bps=%s entry_fee_bps=%s exit_fee_bps=%s round_trip_fee_bps=%s threshold=%s entry_exec_style=%s exit_exec_style=%s fee_tier=%s', self.symbol, self.product_id, edge, effective_edge, entry_score_bps, imbalance, spread_bps, entry_fee_bps, exit_fee_bps, round_trip_fee_bps, score_threshold_bps, 'maker', planned_exit_style, fee_tier.name)
        long_ok = edge > 0 and imbalance > Decimal('0.10') and spread_bps >= min_spread_bps and entry_score_bps >= score_threshold_bps
        short_ok = edge < 0 and imbalance < Decimal('-0.10') and spread_bps >= min_spread_bps and entry_score_bps >= score_threshold_bps
        if long_ok:
            return self._open_position('long', bid_px, edge, effective_edge, planned_exit_style)
        if short_ok:
            return self._open_position('short', ask_px, edge, effective_edge, planned_exit_style)
        logger.info('FLAT raw_edge_bps=%s effective_edge_bps=%s entry_score_bps=%s imbalance=%s spread_bps=%s entry_fee_bps=%s exit_fee_bps=%s round_trip_fee_bps=%s threshold=%s symbol=%s product_id=%s entry_exec_style=%s exit_exec_style=%s fee_tier=%s', edge, effective_edge, entry_score_bps, imbalance, spread_bps, entry_fee_bps, exit_fee_bps, round_trip_fee_bps, score_threshold_bps, self.symbol, self.product_id, 'maker', planned_exit_style, fee_tier.name)
        return None
