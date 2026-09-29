"""Deterministic, versioned admission rules for simulated follow trades."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

WALLET_EVENT_SCOPE = "wallet+cluster:first_eligible_signal"


@dataclass(frozen=True)
class FollowPolicy:
    version: str
    min_entry_price: Decimal
    max_entry_price: Decimal


@dataclass(frozen=True)
class FollowDecision:
    eligible: bool
    reasons: tuple[str, ...]
    evidence: dict[str, Any]


def dedupe_key(
    *,
    wallet: str,
    cluster_id: str,
) -> tuple[str, str]:
    return wallet.lower(), cluster_id


def decide_follow(
    *,
    policy: FollowPolicy,
    execution_ready: bool,
    fill_price: Decimal | None,
    duplicate_wallet_event: bool,
    sector_id: str,
    sector_gate: dict[str, Any] | None,
    profile_admission_status: str | None = None,
) -> FollowDecision:
    reasons: list[str] = []
    if not execution_ready or fill_price is None:
        reasons.append("EXECUTION_NOT_READY")
    elif fill_price < policy.min_entry_price:
        reasons.append("ENTRY_PRICE_BELOW_POLICY_MIN")
    elif fill_price >= policy.max_entry_price:
        reasons.append("ENTRY_PRICE_AT_OR_ABOVE_POLICY_MAX")
    if duplicate_wallet_event:
        reasons.append("DUPLICATE_WALLET_EVENT")
    if profile_admission_status != "FORWARD_VALIDATED":
        reasons.append("PROFILE_NOT_FORWARD_VALIDATED")
    if sector_gate is None:
        reasons.append("SECTOR_NOT_CALIBRATED")
    elif not bool(sector_gate.get("eligible")):
        reasons.append("SECTOR_NOT_APPROVED")
    normalized_gate = (
        {key: str(value) if isinstance(value, Decimal) else value for key, value in sector_gate.items()}
        if sector_gate is not None
        else None
    )
    evidence = {
        "policy_version": policy.version,
        "price_range": {
            "min_inclusive": str(policy.min_entry_price),
            "max_exclusive": str(policy.max_entry_price),
        },
        "fill_price": str(fill_price) if fill_price is not None else None,
        "dedupe_scope": WALLET_EVENT_SCOPE,
        "duplicate_wallet_event": duplicate_wallet_event,
        "required_profile_status": "FORWARD_VALIDATED",
        "profile_admission_status": profile_admission_status,
        "sector_id": sector_id,
        "sector_gate": normalized_gate,
    }
    return FollowDecision(eligible=not reasons, reasons=tuple(reasons), evidence=evidence)
