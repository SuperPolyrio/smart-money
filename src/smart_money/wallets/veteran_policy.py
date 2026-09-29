"""Observation-only eligibility for high-PnL, experienced sector traders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

POLICY_VERSION = "polybeats-high-pnl-veteran-policy-v1"
STRUCTURED_PORTFOLIO_POLICY_VERSION = "structured-portfolio-expert-policy-v1"


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class HighPnlVeteranPolicy:
    min_settled_events: int = 50
    min_win_rate: float = 0.50
    min_sector_pnl: float = 25_000.0
    min_profit_factor: float = 1.2
    min_recent_settled_events: int = 5
    min_recent_sector_pnl: float = 0.0
    min_recent_profit_factor: float = 1.0
    max_recent_inactivity_days: float = 30.0
    version: str = POLICY_VERSION


@dataclass(frozen=True)
class HighPnlVeteranDecision:
    eligible: bool
    reasons: tuple[str, ...]
    metrics: dict[str, float | int]


@dataclass(frozen=True)
class StructuredPortfolioPolicy:
    min_settled_events: int = 50
    min_event_win_rate: float = 0.40
    min_sector_pnl: float = 100_000.0
    min_profit_factor: float = 2.0
    min_recent_settled_events: int = 5
    min_recent_sector_pnl: float = 0.0
    min_recent_profit_factor: float = 1.5
    max_recent_inactivity_days: float = 30.0
    min_positions_per_event: float = 1.5
    version: str = STRUCTURED_PORTFOLIO_POLICY_VERSION


def decide_high_pnl_veteran(
    evidence: dict[str, Any],
    policy: HighPnlVeteranPolicy = HighPnlVeteranPolicy(),
) -> HighPnlVeteranDecision:
    """Admit a broad observation tier without granting follow eligibility."""
    metrics: dict[str, float | int] = {
        "settled_events": int(_number(evidence.get("settled_events"))),
        "win_rate": _number(evidence.get("win_rate")),
        "sector_pnl": _number(evidence.get("sector_pnl")),
        "profit_factor": _number(evidence.get("profit_factor")),
        "recent_settled_events": int(_number(evidence.get("recent_settled_events"))),
        "recent_sector_pnl": _number(evidence.get("recent_sector_pnl")),
        "recent_profit_factor": _number(evidence.get("recent_profit_factor")),
        "recent_inactivity_days": _number(evidence.get("recent_inactivity_days")),
    }
    reasons: list[str] = []
    if metrics["settled_events"] < policy.min_settled_events:
        reasons.append("VETERAN_SAMPLE_BELOW_MIN")
    if metrics["win_rate"] < policy.min_win_rate:
        reasons.append("VETERAN_WIN_RATE_BELOW_MIN")
    if metrics["sector_pnl"] < policy.min_sector_pnl:
        reasons.append("VETERAN_PNL_BELOW_MIN")
    if metrics["profit_factor"] < policy.min_profit_factor:
        reasons.append("VETERAN_PROFIT_FACTOR_BELOW_MIN")
    if metrics["recent_settled_events"] < policy.min_recent_settled_events:
        reasons.append("VETERAN_RECENT_SAMPLE_BELOW_MIN")
    if metrics["recent_sector_pnl"] <= policy.min_recent_sector_pnl:
        reasons.append("VETERAN_RECENT_PNL_NOT_POSITIVE")
    if metrics["recent_profit_factor"] < policy.min_recent_profit_factor:
        reasons.append("VETERAN_RECENT_PROFIT_FACTOR_BELOW_MIN")
    if metrics["recent_inactivity_days"] > policy.max_recent_inactivity_days:
        reasons.append("VETERAN_RECENT_ACTIVITY_STALE")
    return HighPnlVeteranDecision(not reasons, tuple(reasons), metrics)


def decide_structured_portfolio_expert(
    evidence: dict[str, Any],
    policy: StructuredPortfolioPolicy = StructuredPortfolioPolicy(),
) -> HighPnlVeteranDecision:
    """Admit profitable multi-leg traders for research, never direct follow."""
    settled_events = int(_number(evidence.get("settled_events")))
    settled_positions = int(_number(evidence.get("settled_position_count")))
    metrics: dict[str, float | int] = {
        "settled_events": settled_events,
        "settled_positions": settled_positions,
        "win_rate": _number(evidence.get("win_rate")),
        "sector_pnl": _number(evidence.get("sector_pnl")),
        "profit_factor": _number(evidence.get("profit_factor")),
        "recent_settled_events": int(_number(evidence.get("recent_settled_events"))),
        "recent_sector_pnl": _number(evidence.get("recent_sector_pnl")),
        "recent_profit_factor": _number(evidence.get("recent_profit_factor")),
        "recent_inactivity_days": _number(evidence.get("recent_inactivity_days")),
        "positions_per_event": settled_positions / settled_events if settled_events else 0.0,
    }
    reasons: list[str] = []
    for failed, reason in (
        (metrics["settled_events"] < policy.min_settled_events, "STRUCTURED_SAMPLE_BELOW_MIN"),
        (metrics["win_rate"] < policy.min_event_win_rate, "STRUCTURED_WIN_RATE_BELOW_MIN"),
        (metrics["sector_pnl"] < policy.min_sector_pnl, "STRUCTURED_PNL_BELOW_MIN"),
        (metrics["profit_factor"] < policy.min_profit_factor, "STRUCTURED_PROFIT_FACTOR_BELOW_MIN"),
        (
            metrics["recent_settled_events"] < policy.min_recent_settled_events,
            "STRUCTURED_RECENT_SAMPLE_BELOW_MIN",
        ),
        (metrics["recent_sector_pnl"] <= policy.min_recent_sector_pnl, "STRUCTURED_RECENT_PNL_NOT_POSITIVE"),
        (
            metrics["recent_profit_factor"] < policy.min_recent_profit_factor,
            "STRUCTURED_RECENT_PROFIT_FACTOR_BELOW_MIN",
        ),
        (
            metrics["recent_inactivity_days"] > policy.max_recent_inactivity_days,
            "STRUCTURED_RECENT_ACTIVITY_STALE",
        ),
        (
            metrics["positions_per_event"] < policy.min_positions_per_event,
            "STRUCTURED_MULTI_LEG_EVIDENCE_BELOW_MIN",
        ),
    ):
        if failed:
            reasons.append(reason)
    return HighPnlVeteranDecision(not reasons, tuple(reasons), metrics)
