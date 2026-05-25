from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class PositionState:
    in_position: bool = False
    entry_pending: bool = False
    side: Optional[str] = None
    entry_price: Optional[Decimal] = None
    size: Decimal = Decimal('0')
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    trailing_distance: Optional[Decimal] = None
    best_price: Optional[Decimal] = None
    cooldown_ticks_left: int = 0
    breakeven_price: Optional[Decimal] = None
    breakeven_after_fees: Optional[Decimal] = None
    entry_fee_bps: Decimal = Decimal('0')
    fee_actual_open: Optional[Decimal] = None
    fee_actual_close: Optional[Decimal] = None
    entry_order_digest: Optional[str] = None
    exit_order_digest: Optional[str] = None