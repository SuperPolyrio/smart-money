"""Deterministic gates for copyable, directional wallet-sector experts."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

POLICY_VERSION = "directional-settlement-window-180d-2026-09-29"


@dataclass(frozen=True)
class WalletRefreshSettings:
    """Trial resource limits, independent of the qualification thresholds below."""

    new_wallets_per_round: int = 50
    existing_wallets_per_round: int = 50
    normal_capacity: int = 100
    explore_capacity: int = 50
    explore_days: int = 7
    validity_hours: int = 48
    archive_days: int = 30
    slice_seconds: int = 60
    slice_requests: int = 10
    connect_seconds: int = 5
    request_seconds: int = 20
    extra_retries: int = 2
    # A single reader is stricter than the suggested 4 tasks / 2 per host.
    workers: int = 1


def _number(value: Any) -> float:
    try:
        result = float(value)
        return result if isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class DirectionalExpertPolicy:
    history_window_days: int = 180
    min_directional_events: int = 20
    min_win_rate: float = 0.65
    min_sector_pnl: float = 0.0
    min_profit_factor: float = 1.2
    max_two_sided_ratio: float = 0.10
    recent_window_days: int = 90
    min_recent_events: int = 10
    min_recent_win_rate: float = 0.60
    min_recent_pnl: float = 0.0
    min_recent_profit_factor: float = 1.2
    max_recent_inactivity_days: float = 30.0
    forward_min_events: int = 10
    forward_suspend_events: int = 5
    forward_min_win_rate: float = 0.65
    forward_min_roi: float = 0.0
    forward_min_profit_factor: float = 1.2
    forward_suspend_profit_factor: float = 1.0
    version: str = POLICY_VERSION


@dataclass(frozen=True)
class DirectionalExpertDecision:
    eligible: bool
    reasons: tuple[str, ...]
    metrics: dict[str, float | int]


@dataclass(frozen=True)
class ForwardAdmission:
    status: str
    reasons: tuple[str, ...]
    metrics: dict[str, float | int]


def decide_directional_expert(
    evidence: dict[str, Any],
    policy: DirectionalExpertPolicy = DirectionalExpertPolicy(),
) -> DirectionalExpertDecision:
    metrics: dict[str, float | int] = {
        "directional_events": int(_number(evidence.get("directional_events"))),
        "win_rate": _number(evidence.get("win_rate")),
        "sector_pnl": _number(evidence.get("sector_pnl")),
        "profit_factor": _number(evidence.get("profit_factor")),
        "two_sided_ratio": _number(evidence.get("two_sided_ratio")),
        "recent_events": int(_number(evidence.get("recent_events"))),
        "recent_win_rate": _number(evidence.get("recent_win_rate")),
        "recent_sector_pnl": _number(evidence.get("recent_sector_pnl")),
        "recent_profit_factor": _number(evidence.get("recent_profit_factor")),
        "recent_inactivity_days": _number(evidence.get("recent_inactivity_days")),
    }
    reasons: list[str] = []
    for name in metrics:
        try:
            if isinstance(evidence.get(name), bool) or not isfinite(float(evidence[name])):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            reasons.append("MISSING_OR_INVALID_METRIC:" + name)
    if metrics["directional_events"] < policy.min_directional_events:
        reasons.append("INSUFFICIENT_DIRECTIONAL_EVENTS")
    if metrics["win_rate"] < policy.min_win_rate:
        reasons.append("DIRECTIONAL_WIN_RATE_BELOW_MIN")
    if metrics["sector_pnl"] <= policy.min_sector_pnl:
        reasons.append("DIRECTIONAL_PNL_NOT_POSITIVE")
    if metrics["profit_factor"] < policy.min_profit_factor:
        reasons.append("DIRECTIONAL_PROFIT_FACTOR_BELOW_MIN")
    if metrics["two_sided_ratio"] > policy.max_two_sided_ratio:
        reasons.append("TWO_SIDED_EVENT_RATIO_ABOVE_MAX")
    if metrics["recent_events"] < policy.min_recent_events:
        reasons.append("RECENT_DIRECTIONAL_SAMPLE_INSUFFICIENT")
    if metrics["recent_win_rate"] < policy.min_recent_win_rate:
        reasons.append("RECENT_DIRECTIONAL_WIN_RATE_BELOW_MIN")
    if metrics["recent_sector_pnl"] <= policy.min_recent_pnl:
        reasons.append("RECENT_DIRECTIONAL_PNL_NOT_POSITIVE")
    if metrics["recent_profit_factor"] < policy.min_recent_profit_factor:
        reasons.append("RECENT_DIRECTIONAL_PROFIT_FACTOR_BELOW_MIN")
    if metrics["recent_inactivity_days"] > policy.max_recent_inactivity_days:
        reasons.append("RECENT_DIRECTIONAL_ACTIVITY_STALE")
    return DirectionalExpertDecision(not reasons, tuple(reasons), metrics)


def decide_forward_admission(
    evidence: dict[str, Any],
    policy: DirectionalExpertPolicy = DirectionalExpertPolicy(),
) -> ForwardAdmission:
    metrics: dict[str, float | int] = {
        "settled_events": int(_number(evidence.get("settled_events"))),
        "roi": _number(evidence.get("roi")),
        "profit_factor": _number(evidence.get("profit_factor")),
        "wins": int(_number(evidence.get("wins"))),
        "losses": int(_number(evidence.get("losses"))),
    }
    if (
        metrics["settled_events"] >= policy.forward_min_events
        and ((metrics["wins"] / metrics["settled_events"]) if metrics["settled_events"] else 0.0)
        >= policy.forward_min_win_rate
        and metrics["roi"] > policy.forward_min_roi
        and metrics["profit_factor"] >= policy.forward_min_profit_factor
    ):
        return ForwardAdmission("FORWARD_VALIDATED", (), metrics)
    if metrics["settled_events"] >= policy.forward_suspend_events and (
        metrics["roi"] <= policy.forward_min_roi
        or metrics["profit_factor"] < policy.forward_suspend_profit_factor
        or (
            metrics["settled_events"] >= policy.forward_min_events
            and ((metrics["wins"] / metrics["settled_events"]) if metrics["settled_events"] else 0.0)
            < policy.forward_min_win_rate
        )
    ):
        reasons = []
        if metrics["roi"] <= policy.forward_min_roi:
            reasons.append("FORWARD_ROI_NOT_POSITIVE")
        if metrics["profit_factor"] < policy.forward_suspend_profit_factor:
            reasons.append("FORWARD_PROFIT_FACTOR_BELOW_ONE")
        if (
            metrics["settled_events"] >= policy.forward_min_events
            and ((metrics["wins"] / metrics["settled_events"]) if metrics["settled_events"] else 0.0)
            < policy.forward_min_win_rate
        ):
            reasons.append("FORWARD_WIN_RATE_BELOW_MIN")
        return ForwardAdmission("SUSPENDED", tuple(reasons), metrics)
    return ForwardAdmission(
        "SHADOW_OBSERVE",
        ("FORWARD_SAMPLE_OR_EDGE_NOT_YET_SUFFICIENT",),
        metrics,
    )
