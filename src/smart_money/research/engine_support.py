"""Shared immutable MAS stage inputs and formatting helpers."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel

from smart_money.markets.taxonomy import normalize_market
from smart_money.research.contracts import EvidenceItem
from smart_money.research.models import (
    SignalCandidate,
)


def _money(value: Any) -> str:
    if value is None:
        return "--"
    return f"${Decimal(str(value)):f}"


def _pct(value: Any) -> str:
    if value is None:
        return "--"
    return f"{Decimal(str(value)) * 100:f}%"


def _compact(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


T = TypeVar("T", bound=BaseModel)


EVIDENCE_KEY_GUIDE = {
    "signal": "The candidate plus embedded wallet, trade, signal, and caveat evidence.",
    "market": "The supplied point-in-time Polymarket market snapshot.",
    "rules": "Captured settlement rules; snapshot_at determines trade-time versus later-update use.",
    "osint": "Domain-routed external evidence with source tier, publication time, and trade-time relation.",
    "cross_market": "Related markets and this wallet's visible signals in the same event cluster, bounded by as_of.",
}

MARKET_PROMPT_FIELDS = (
    "condition_id",
    "market_id",
    "gamma_market_id",
    "event_id",
    "market_slug",
    "event_slug",
    "title",
    "end_at",
    "outcomes",
    "outcome_prices",
    "tags",
    "series",
    "official_category",
    "internal_category_l1",
    "internal_category_l2",
    "internal_topic",
    "rules_risk_score",
    "closed",
    "resolved",
    "winning_outcome",
    "liquidity",
    "volume",
    "last_seen_at",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _select_fields(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: value[key] for key in fields if value.get(key) not in (None, "", [], {})}


def research_context(context: dict[str, Any], candidate: SignalCandidate) -> dict[str, Any]:
    """Map official Gamma fields once, without inventing a historical rule snapshot."""
    market = dict(context.get("market") or candidate.evidence.get("market") or {})
    context["_market_source_snapshot"] = dict(market)
    context["market"] = market
    for key in ("condition_id", "conditionId"):
        if market.get(key) and market[key] != candidate.market_id:
            raise ValueError("Research market identity differs from the frozen observation")
    normalized = normalize_market(market)
    if normalized:
        for key in ("condition_id", "title", "market_slug", "event_title", "end_at", "outcomes", "tags"):
            market.setdefault(key, getattr(normalized, key))
    text = market.get("rules_current") or (normalized.rules if normalized else None)
    obtained = context.get("market_obtained_at")
    if text and obtained and not context.get("rules"):
        context["rules"] = {"rules_text": text, "snapshot_at": obtained, "condition_id": candidate.market_id}
    if context.get("rules", {}).get("rules_text"):
        market["rules_current"] = context["rules"]["rules_text"]
    return context


def evidence_packet(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    """Give roles original sources once; omit repeated projections and parser audit copies."""
    return [
        {
            **item.model_dump(
                mode="json",
                exclude={"structured_payload", "sanitized_text"},
            ),
            "source_snapshot": item.source_snapshot,
            "structured_payload": {"contract_fields": item.structured_payload.get("contract_fields", {})},
        }
        for item in items
    ]


def _quality_packet(
    candidate: SignalCandidate, context: dict[str, Any], osint: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    profiles = [p for p in candidate.wallet_profiles if p.wallet.lower() == candidate.wallet.lower()]
    market = context.get("market") or {}
    rules = context.get("rules") or {}
    missing = []
    for key, present in (
        ("wallet_history", any(p.sector_resolved_count is not None for p in profiles)),
        ("sector_match", any(p.sector_match is not None for p in profiles)),
        ("size_baseline", any(p.same_price_band_median_size is not None for p in profiles)),
        ("market_metadata", bool(market.get("title"))),
        ("rule_snapshot", bool(rules or market.get("rules_current"))),
    ):
        if not present:
            missing.append(key)
    return {
        "available_sources": [
            key
            for key, present in (
                ("signal", bool(candidate.evidence)),
                ("market", bool(market)),
                ("rules", bool(rules)),
                ("osint", bool(osint)),
            )
            if present
        ],
        "missing": missing,
        "sector_match": all(p.sector_match is True for p in profiles) if profiles else None,
        "signal_mode": candidate.signal_type,
    }
