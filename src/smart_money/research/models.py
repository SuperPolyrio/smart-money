"""Typed contracts shared by the deterministic signal layer and MAS."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from smart_money.contracts import StrictModel, parse_utc
from smart_money.research.contracts import (
    CapabilityDecision,
    CaseClassification,
    EvidenceContractResult,
    EvidenceItem,
    PublicationPolicy,
)

EvidenceKey = Literal["signal", "market", "rules", "osint", "cross_market"]
DomainName = Literal[
    "POLITICS_ELECTIONS",
    "GEOPOLITICS",
    "ESPORTS",
    "CRYPTO",
    "FINANCE",
    "MACRO_ECONOMY",
    "TECH_SCIENCE",
    "CULTURE_ENTERTAINMENT",
    "WEATHER_CLIMATE",
    "MENTIONS_SOCIAL",
    "SPORTS",
    "GENERAL",
]
DomainAgentName = Literal[
    "CryptoAgent",
    "WeatherClimateAgent",
    "PoliticsElectionsAgent",
    "GeopoliticsAgent",
    "EsportsAgent",
    "FinanceAgent",
    "MacroEconomyAgent",
    "TechScienceAgent",
    "CultureEntertainmentAgent",
    "MentionsSocialAgent",
    "GeneralTagAgent",
    "PoliticsAgent",
    "SportsAgent",
]

WalletValidationStatus = Literal[
    "VALIDATED_SPECIALIST",
    "QUALIFIED_OBSERVER",
    "UNPROFILED",
    "REJECTED",
    "UNKNOWN",
]
PositionChange = Literal["OPEN", "ADD", "REDUCE", "EXIT", "CLOSE", "NON_TRADE", "UNKNOWN"]


class WalletProfilePacket(StrictModel):
    """Point-in-time wallet profile supplied explicitly to the MAS.

    Optional values deliberately remain ``None`` instead of being fabricated.  The
    completeness fields keep research-only inputs from silently qualifying as
    public smart-money evidence.
    """

    packet_version: Literal["wallet-profile-v2"] = "wallet-profile-v2"
    wallet: str
    display_name: str | None = None
    discovery_sources: list[dict[str, Any]] = Field(default_factory=list)
    wallet_validation_status: WalletValidationStatus = "UNKNOWN"
    profile_role: str | None = None
    profile_snapshot_at: datetime | None = None
    profile_source_version: str | None = None
    market_sector: str | None = None
    source_profile_sector: str | None = None
    sector_match: bool | None = None
    sector_pnl: float | None = None
    sector_resolved_count: int | None = Field(default=None, ge=0)
    sector_win_rate: float | None = Field(default=None, ge=0, le=1)
    sector_profit_factor: float | None = Field(default=None, ge=0)
    recent_window_days: int | None = Field(default=None, ge=1)
    recent_sector_pnl: float | None = None
    recent_sector_resolved_count: int | None = Field(default=None, ge=0)
    recent_sector_win_rate: float | None = Field(default=None, ge=0, le=1)
    low_entry_high_exit_count: int | None = Field(default=None, ge=0)
    same_price_band: str | None = None
    same_price_band_sample_count: int | None = Field(default=None, ge=0)
    same_price_band_median_size: float | None = Field(default=None, ge=0)
    current_trade_size: float | None = Field(default=None, ge=0)
    current_trade_size_multiple: float | None = Field(default=None, ge=0)
    position_change: PositionChange = "UNKNOWN"
    position_before: float | None = None
    position_after: float | None = None
    consensus_wallet_count: int = Field(default=1, ge=1)
    historical_behavior_flags: list[str] = Field(default_factory=list)
    two_sided_ratio: float | None = Field(default=None, ge=0, le=1)
    non_directional_probability: float | None = Field(default=None, ge=0, le=1)
    admission_status: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    wallet_profile_complete: bool = False
    missing_wallet_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_completion(self) -> WalletProfilePacket:
        required = {
            "profile_snapshot_at": self.profile_snapshot_at,
            "profile_source_version": self.profile_source_version,
            "market_sector": self.market_sector,
            "source_profile_sector": self.source_profile_sector,
            "sector_match": self.sector_match,
            "sector_pnl": self.sector_pnl,
            "sector_resolved_count": self.sector_resolved_count,
            "sector_win_rate": self.sector_win_rate,
            "same_price_band": self.same_price_band,
            "same_price_band_sample_count": self.same_price_band_sample_count,
            "same_price_band_median_size": self.same_price_band_median_size,
            "current_trade_size": self.current_trade_size,
            "current_trade_size_multiple": self.current_trade_size_multiple,
            "admission_status": self.admission_status,
            "evidence_ids": self.evidence_ids,
        }
        missing = [key for key, value in required.items() if value is None or value == [] or value == ""]
        if self.wallet_validation_status in {"UNPROFILED", "REJECTED", "UNKNOWN"}:
            missing.append("wallet_validation_status")
        if self.position_change == "UNKNOWN":
            missing.append("position_change")
        normalized = list(dict.fromkeys([*self.missing_wallet_fields, *missing]))
        object.__setattr__(self, "missing_wallet_fields", normalized)
        object.__setattr__(self, "wallet_profile_complete", not normalized)
        return self


class SignalCandidate(StrictModel):
    candidate_id: str
    as_of: datetime
    wallet: str
    market_id: str
    event_cluster_id: str
    sector_id: str
    side: str | None = None
    outcome: str | None = None
    entry_price: float | None = None
    notional: float | None = None
    signal_type: str
    trigger_reasons: list[str] = Field(default_factory=list)
    source_snapshot_ids: list[str] = Field(default_factory=list)
    wallet_profiles: list[WalletProfilePacket] = Field(default_factory=list)
    evidence: dict[str, Any]

    @model_validator(mode="after")
    def wallet_profiles_must_be_point_in_time(self) -> SignalCandidate:
        trade_at = (self.evidence.get("signal") or {}).get("trade_at")
        if trade_at is not None and parse_utc(trade_at) != self.as_of:
            raise ValueError("signal as_of must equal recorded trade_at, not research or observation time")
        future = [
            packet.wallet
            for packet in self.wallet_profiles
            if packet.profile_snapshot_at is not None and packet.profile_snapshot_at > self.as_of
        ]
        if future:
            raise ValueError(f"wallet profile snapshot is later than signal as_of: {','.join(future)}")
        if any(
            (seen := parse_utc(source.get("last_seen_at"))) is not None and seen > self.as_of
            for packet in self.wallet_profiles
            for source in packet.discovery_sources
        ):
            raise ValueError("wallet discovery source is later than signal as_of")
        return self


class ClaimDraft(StrictModel):
    claim_id: str = ""
    statement: str
    modality: Literal["FACT", "INFERENCE", "HYPOTHESIS", "UNKNOWN"]
    confidence: float = Field(ge=0, le=1)
    evidence_keys: list[EvidenceKey] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    time_scope: Literal["TRADE_TIME", "RESEARCH_UPDATE"] = "TRADE_TIME"
    depends_on: list[str] = Field(default_factory=list)
    references: list[EvidenceReference] = Field(default_factory=list)
    required_qualifications: list[str] = Field(default_factory=list)
    contract_field: str | None = None
    field_value: Any = None


class SpecialistReport(StrictModel):
    agent: Literal["WalletForensicsAgent", "RulesAndOsintAgent"]
    summary: str
    claims: list[ClaimDraft] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class RuleQuestion(StrictModel):
    field: str
    question: str
    rule_quote: str
    source_ids: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    fact_time: str | None = None
    value: Any = None
    value_quote: str | None = None


class RulesReport(SpecialistReport):
    questions: list[RuleQuestion] = Field(default_factory=list)


class WalletTradeInterpretation(StrictModel):
    observed_trade: str
    position_effect: Literal["INCREASE", "REDUCE", "CLOSE", "UNKNOWN"]
    market_direction_equivalent: Literal["UP", "DOWN", "NEUTRAL", "UNKNOWN"]
    crypto_agent_bias: str
    alignment: Literal["ALIGNED", "CONTRADICTED", "MIXED", "UNKNOWN"]
    fair_probability_traded_outcome: float | None = Field(default=None, ge=0, le=1)
    entry_probability: float | None = Field(default=None, ge=0, le=1)
    edge_vs_entry: float | None = None
    interpretation: str
    alternative_explanations: list[str] = Field(default_factory=list)


class DomainExpertReport(StrictModel):
    agent: DomainAgentName
    domain: DomainName
    market_state: str
    signal_interpretation: str
    summary: str
    alignment: Literal["SUPPORTS", "CONTRADICTS", "MIXED", "INSUFFICIENT"] = "INSUFFICIENT"
    claims: list[ClaimDraft] = Field(default_factory=list)
    key_factors: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    wallet_trade_interpretation: WalletTradeInterpretation | None = None


class CounterHypothesis(StrictModel):
    hypothesis: str
    severity: Literal["low", "medium", "high"]
    evidence_keys: list[EvidenceKey] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    references: list[EvidenceReference] = Field(default_factory=list)
    required_qualification: str | None = None


class SkepticReport(StrictModel):
    counter_hypotheses: list[CounterHypothesis] = Field(default_factory=list)
    overall_risk: Literal["low", "medium", "high"] = "medium"
    requested_fields: list[str] = Field(default_factory=list)


class VerifiedClaim(StrictModel):
    claim_id: str
    statement: str
    modality: Literal["FACT", "INFERENCE", "HYPOTHESIS", "UNKNOWN"]
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: list[str]
    status: Literal["VERIFIED", "UNSUPPORTED"]
    time_scope: Literal["TRADE_TIME", "RESEARCH_UPDATE"] = "TRADE_TIME"
    depends_on: list[str] = Field(default_factory=list)
    required_qualifications: list[str] = Field(default_factory=list)
    critical: bool = True


class EvidenceReference(StrictModel):
    evidence_id: str
    locator: str  # JSON pointer into the item's source_snapshot.
    quote: str = Field(min_length=1)


class EvidenceClaimReview(StrictModel):
    entity_matches_market: bool = False
    preserved_qualifications: list[str] = Field(default_factory=list)
    claim_index: int = Field(ge=1)
    verdict: Literal["SUPPORTED", "UNSUPPORTED", "CONTRADICTED", "AMBIGUOUS"]
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    references: list[EvidenceReference] = Field(default_factory=list)


class EvidenceVerificationReport(StrictModel):
    agent: Literal["EvidenceVerifierAgent"] = "EvidenceVerifierAgent"
    summary: str
    reviews: list[EvidenceClaimReview] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    future_leakage_detected: bool = False


class DraftSemanticReview(StrictModel):
    input_hash: str
    report: EvidenceVerificationReport


class PolicyDecision(PublicationPolicy):
    risk_level: Literal["low", "medium", "high"] = "medium"


class PublicationDraft(StrictModel):
    title: str
    brief: str
    market_read: str
    wallet_read: str
    why_it_matters: str
    rules_context: str = ""
    domain_analysis: str = ""
    verification_summary: str = ""
    external_context: str = ""
    risk: str
    confidence: Literal["low", "medium", "high"]
    content_text: str = ""
    body_text: str = ""
    short_summary: str = ""
    risk_note: str = ""
    claim_refs: dict[str, list[str]] = Field(default_factory=dict)


class AnalysisRequest(StrictModel):
    candidate: SignalCandidate
    context: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class MasResult(StrictModel):
    result_schema_version: Literal[3]
    run_id: str
    classification: CaseClassification
    capability: CapabilityDecision
    evidence_contract: EvidenceContractResult
    candidate: SignalCandidate
    wallet_report: SpecialistReport
    rules_report: RulesReport
    domain_report: DomainExpertReport | None = None
    skeptic_report: SkepticReport
    evidence: list[EvidenceItem]
    claims: list[VerifiedClaim]
    verification_report: EvidenceVerificationReport
    policy: PolicyDecision
    publication: PublicationDraft
    draft_review: DraftSemanticReview | None = None
    agent_runtime: dict[str, Any] = Field(default_factory=dict)
