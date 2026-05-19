from dataclasses import dataclass
from decimal import Decimal
from typing import List


@dataclass(frozen=True)
class FeeTier:
    name: str
    min_volume_usd: Decimal
    taker_bps: Decimal
    maker_bps: Decimal


FEE_TIERS: List[FeeTier] = [
    FeeTier('starter', Decimal('0'), Decimal('3.5'), Decimal('1.0')),
    FeeTier('active', Decimal('1000000'), Decimal('3.0'), Decimal('0.5')),
    FeeTier('pro', Decimal('10000000'), Decimal('2.5'), Decimal('0.0')),
    FeeTier('desk', Decimal('100000000'), Decimal('2.0'), Decimal('-0.3')),
    FeeTier('elite', Decimal('5000000000'), Decimal('1.5'), Decimal('-0.8')),
]


def resolve_fee_tier(volume_30d_usd: Decimal) -> FeeTier:
    current = FEE_TIERS[0]
    for tier in FEE_TIERS:
        if volume_30d_usd >= tier.min_volume_usd:
            current = tier
        else:
            break
    return current
