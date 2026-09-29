"""Simple hold-to-resolution scoring for a bought Polymarket outcome token."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class HoldToResolutionResult:
    won: bool | None
    payout_per_share: Decimal
    pnl: Decimal
    roi: float
    final_resolution: str


def terminal_settlement_code(outcome_prices: Any, *, closed: bool | None) -> int:
    """Return a settlement code only for an explicit terminal Gamma payout.

    Ordinary market probabilities must never be interpreted as settlement.  A
    market therefore needs to be closed and have an exact binary payout vector.
    """
    if closed is not True:
        return 0
    values = outcome_prices
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except json.JSONDecodeError:
            return 0
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        return 0
    try:
        first, second = (Decimal(str(values[0])), Decimal(str(values[1])))
    except (ArithmeticError, ValueError):
        return 0
    tolerance = Decimal("0.000001")

    def close_to(value: Decimal, target: str) -> bool:
        return abs(value - Decimal(target)) <= tolerance

    if close_to(first, "1") and close_to(second, "0"):
        return 1
    if close_to(first, "0") and close_to(second, "1"):
        return 2
    if close_to(first, "0.5") and close_to(second, "0.5"):
        return 3
    return 0


def score_bought_token(
    *,
    side: str,
    outcome_index: int,
    settlement_code: int,
    entry_price: Decimal,
    notional: Decimal,
) -> HoldToResolutionResult:
    """Score one buy as if the purchased tokens were held until settlement.

    Settlement codes follow the existing core database convention:
    1 = first outcome wins, 2 = second outcome wins, 3 = cancelled/0.5 payout.
    """
    if side.upper() != "BUY":
        raise ValueError("simple settlement scoring only supports bought outcome tokens")
    if outcome_index not in {0, 1}:
        raise ValueError("outcome_index must be 0 or 1")
    if settlement_code not in {1, 2, 3}:
        raise ValueError("settlement_code must be 1, 2, or 3")
    if entry_price <= 0 or entry_price > 1:
        raise ValueError("entry_price must be in (0, 1]")
    if notional <= 0:
        raise ValueError("notional must be positive")

    if settlement_code == 3:
        won = None
        payout_per_share = Decimal("0.5")
        final_resolution = "CANCELLED"
    else:
        won = outcome_index == settlement_code - 1
        payout_per_share = Decimal("1") if won else Decimal("0")
        final_resolution = "WIN" if won else "LOSS"

    token_count = notional / entry_price
    pnl = token_count * payout_per_share - notional
    return HoldToResolutionResult(
        won=won,
        payout_per_share=payout_per_share,
        pnl=pnl,
        roi=float(pnl / notional),
        final_resolution=final_resolution,
    )
