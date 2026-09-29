"""The single publication decision; research output is not delivery."""

from __future__ import annotations

from typing import Literal

from smart_money.publication.release_validator import ReleaseValidation
from smart_money.research.contracts import (
    CapabilityDecision,
    CapabilityState,
    EvidenceContractResult,
    PublicationPolicy,
)
from smart_money.research.models import EvidenceVerificationReport, SignalCandidate, VerifiedClaim
from smart_money.research.wallet_profile import public_smart_money_wallets, wallet_display_label


class PublicationPolicyEngine:
    version = "publication-policy-v2"

    def decide(
        self,
        candidate: SignalCandidate,
        capability: CapabilityDecision,
        contract: EvidenceContractResult,
        claims: list[VerifiedClaim],
        verification: EvidenceVerificationReport,
        *,
        execution_mode: str,
        services_degraded: bool,
        required_agent_semantic_task_incomplete: bool,
        analysis_review_reasons: list[str],
        release_validation: ReleaseValidation,
    ) -> PublicationPolicy:
        reasons = list(analysis_review_reasons)
        status: Literal["READY_TO_PUBLISH", "REVIEW_REQUIRED", "SUPPRESSED", "VALIDATION_FAILED"] = "READY_TO_PUBLISH"
        if reasons:
            status = "REVIEW_REQUIRED"
        long_form = True
        trade_time = not verification.future_leakage_detected and not contract.pit_missing_fields
        if services_degraded:
            status = "REVIEW_REQUIRED"
            reasons.append("PRODUCTION_DEGRADED")
        if required_agent_semantic_task_incomplete:
            status = "REVIEW_REQUIRED"
            reasons.append("REQUIRED_ANALYSIS_INCOMPLETE")
        if not contract.passed:
            status = "SUPPRESSED"
            reasons.append("DOMAIN_EVIDENCE_INSUFFICIENT")
        if capability.state in {CapabilityState.DISABLED, CapabilityState.SHADOW}:
            status = "SUPPRESSED"
            reasons.append("CAPABILITY_NOT_PUBLIC")
        elif capability.state == CapabilityState.REVIEW_ONLY and status == "READY_TO_PUBLISH":
            status = "REVIEW_REQUIRED"
            reasons.append("CAPABILITY_REVIEW_ONLY")
        if execution_mode == "DETERMINISTIC_ONLY":
            long_form = False
            if status == "READY_TO_PUBLISH":
                status = "REVIEW_REQUIRED"
            reasons.append("LLM_DEGRADED")
        if not trade_time:
            trade_time = False
            reasons.append("TRADE_TIME_EDGE_CLAIMS_PROHIBITED")
        if any(claim.critical and claim.status != "VERIFIED" for claim in claims):
            status = "VALIDATION_FAILED"
            reasons.append("CRITICAL_CLAIM_UNSUPPORTED")
        if verification.conflicts:
            if status == "READY_TO_PUBLISH":
                status = "REVIEW_REQUIRED"
            reasons.append("UNRESOLVED_CONTRADICTION")
        if "NO_VERIFIED_CLAIMS" in reasons or not any(claim.status == "VERIFIED" for claim in claims):
            reasons.append("NO_VERIFIED_CLAIMS")
            status = "SUPPRESSED"
        if not release_validation.valid:
            status = "VALIDATION_FAILED"
            reasons.extend(f"RELEASE_VALIDATION:{error}" for error in release_validation.errors)
        validated_profiles = public_smart_money_wallets(candidate)
        publication_type: Literal[
            "SMART_MONEY_SIGNAL",
            "ANOMALOUS_WALLET_ALERT",
            "RULE_EDGE_ALERT",
            "MARKET_RESEARCH",
            "SUPPRESSED",
            "RESEARCH_ONLY",
        ] = "SMART_MONEY_SIGNAL" if validated_profiles else "MARKET_RESEARCH"
        if status == "SUPPRESSED":
            publication_type = "SUPPRESSED"
        elif status in {"REVIEW_REQUIRED", "VALIDATION_FAILED"}:
            publication_type = "RESEARCH_ONLY"
        return PublicationPolicy(
            publication_type=publication_type,
            status=status,
            long_form_allowed=long_form,
            trade_time_edge_claims_allowed=trade_time,
            reasons=list(dict.fromkeys(reasons)),
            wallet_display_label=wallet_display_label(candidate),
        )
