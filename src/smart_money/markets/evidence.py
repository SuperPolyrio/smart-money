"""Structured, versioned evidence for signals."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

REQUIRED_TOP_LEVEL = {"signal_id", "signal_at", "market", "wallet", "trade", "signal", "caveats"}


def build_evidence(
    *,
    signal_id: str,
    signal_at: datetime,
    market: Mapping[str, Any],
    wallet: Mapping[str, Any],
    trade: Mapping[str, Any],
    signal: Mapping[str, Any],
    caveats: list[str],
) -> dict[str, Any]:
    evidence = {
        "signal_id": signal_id,
        "signal_at": signal_at.isoformat(),
        "market": dict(market),
        "wallet": dict(wallet),
        "trade": dict(trade),
        "signal": dict(signal),
        "caveats": caveats,
    }
    validate_evidence(evidence)
    return evidence


def validate_evidence(evidence: Mapping[str, Any]) -> None:
    missing = REQUIRED_TOP_LEVEL.difference(evidence)
    if missing:
        raise ValueError(f"evidence missing required keys: {sorted(missing)}")
    for key in ("market", "wallet", "trade", "signal"):
        if not isinstance(evidence[key], Mapping):
            raise ValueError(f"evidence {key} must be an object")
    if not isinstance(evidence["caveats"], list):
        raise ValueError("evidence caveats must be a list")
