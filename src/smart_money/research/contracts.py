"""Versioned contracts from the MAS optimization and acceptance specification."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_serializer, model_validator

from smart_money.contracts import StrictModel


class Domain(str, Enum):  # noqa: UP042 - production still supports Python 3.10
    CRYPTO = "CRYPTO"
    WEATHER_CLIMATE = "WEATHER_CLIMATE"
    POLITICS_ELECTIONS = "POLITICS_ELECTIONS"
    GEOPOLITICS = "GEOPOLITICS"
    SPORTS = "SPORTS"
    ESPORTS = "ESPORTS"
    FINANCE = "FINANCE"
    MACRO_ECONOMY = "MACRO_ECONOMY"
    TECH_SCIENCE = "TECH_SCIENCE"
    CULTURE_ENTERTAINMENT = "CULTURE_ENTERTAINMENT"
    MENTIONS_SOCIAL = "MENTIONS_SOCIAL"
    GENERAL = "GENERAL"


class MarketArchetype(str, Enum):  # noqa: UP042 - production still supports Python 3.10
    PRICE_DIRECTION_SHORT_WINDOW = "PRICE_DIRECTION_SHORT_WINDOW"
    PRICE_THRESHOLD_TOUCH = "PRICE_THRESHOLD_TOUCH"
    PRICE_THRESHOLD_ENDPOINT = "PRICE_THRESHOLD_ENDPOINT"
    CRYPTO_TOKEN_LAUNCH = "CRYPTO_TOKEN_LAUNCH"
    CRYPTO_FDV_AFTER_LAUNCH = "CRYPTO_FDV_AFTER_LAUNCH"
    CRYPTO_PUBLIC_SALE = "CRYPTO_PUBLIC_SALE"
    CRYPTO_CORPORATE_BTC_ACTION = "CRYPTO_CORPORATE_BTC_ACTION"
    CRYPTO_REGULATION = "CRYPTO_REGULATION"
    CRYPTO_EXCHANGE_LISTING = "CRYPTO_EXCHANGE_LISTING"
    CRYPTO_PROTOCOL_EVENT = "CRYPTO_PROTOCOL_EVENT"
    GENERAL_CRYPTO = "GENERAL_CRYPTO"
    EXACT_WEATHER_BUCKET = "EXACT_WEATHER_BUCKET"
    WEATHER_THRESHOLD = "WEATHER_THRESHOLD"
    CENTRAL_BANK_DECISION = "CENTRAL_BANK_DECISION"
    MACRO_DATA_RELEASE = "MACRO_DATA_RELEASE"
    ELECTION_WINNER = "ELECTION_WINNER"
    APPOINTMENT_OR_REMOVAL = "APPOINTMENT_OR_REMOVAL"
    POLITICAL_EXECUTIVE_ACTION = "POLITICAL_EXECUTIVE_ACTION"
    POLITICAL_PARTY_CONTROL = "POLITICAL_PARTY_CONTROL"
    POLITICAL_POLLING = "POLITICAL_POLLING"
    POLITICAL_INSTITUTIONAL_VOTE = "POLITICAL_INSTITUTIONAL_VOTE"
    POLITICAL_COURT_DECISION = "POLITICAL_COURT_DECISION"
    POLITICAL_LEGISLATION = "POLITICAL_LEGISLATION"
    MILITARY_ACTION = "MILITARY_ACTION"
    TERRITORY_CONTROL = "TERRITORY_CONTROL"
    SPORTS_MATCH_WINNER = "SPORTS_MATCH_WINNER"
    SPORTS_SERIES_WINNER = "SPORTS_SERIES_WINNER"
    ESPORTS_MATCH_WINNER = "ESPORTS_MATCH_WINNER"
    IPO_COMPLETION = "IPO_COMPLETION"
    IPO_VALUATION = "IPO_VALUATION"
    EARNINGS_OR_CORPORATE_EVENT = "EARNINGS_OR_CORPORATE_EVENT"
    PRODUCT_RELEASE_BY_DATE = "PRODUCT_RELEASE_BY_DATE"
    SCIENTIFIC_MILESTONE = "SCIENTIFIC_MILESTONE"
    AWARD_WINNER = "AWARD_WINNER"
    BOX_OFFICE_THRESHOLD = "BOX_OFFICE_THRESHOLD"
    MENTION_OR_POST_COUNT = "MENTION_OR_POST_COUNT"
    OFFICIAL_RANKING_SNAPSHOT = "OFFICIAL_RANKING_SNAPSHOT"
    RULE_DISPUTE = "RULE_DISPUTE"
    GENERAL_EVENT = "GENERAL_EVENT"


class PoliticalCaseType(str, Enum):  # noqa: UP042 - production still supports Python 3.10
    ELECTION = "ELECTION"
    APPOINTMENT_OR_REMOVAL = "APPOINTMENT_OR_REMOVAL"
    EXECUTIVE_ACTION = "EXECUTIVE_ACTION"
    PARTY_CONTROL = "PARTY_CONTROL"
    POLLING_OR_APPROVAL = "POLLING_OR_APPROVAL"
    INSTITUTIONAL_VOTE = "INSTITUTIONAL_VOTE"
    COURT_DECISION = "COURT_DECISION"
    LEGISLATION = "LEGISLATION"
    GENERAL = "GENERAL"


class JurisdictionScope(str, Enum):  # noqa: UP042 - production still supports Python 3.10
    NATIONAL = "NATIONAL"
    SUBNATIONAL = "SUBNATIONAL"
    SUPRANATIONAL = "SUPRANATIONAL"
    INTERNATIONAL_ORGANIZATION = "INTERNATIONAL_ORGANIZATION"
    UNKNOWN = "UNKNOWN"


class PoliticsCase(StrictModel):
    """Jurisdiction-neutral political context derived before specialist analysis."""

    case_version: str = "politics-case-v1"
    case_type: PoliticalCaseType
    jurisdiction_scope: JurisdictionScope = JurisdictionScope.UNKNOWN
    jurisdiction_code: str | None = None
    institution: str | None = None
    actors: list[str] = Field(default_factory=list)
    decision_stage: str = "UNKNOWN"
    deadline: str | None = None
    resolution_source: str | None = None


class CapabilityState(str, Enum):  # noqa: UP042 - production still supports Python 3.10
    DISABLED = "DISABLED"
    SHADOW = "SHADOW"
    REVIEW_ONLY = "REVIEW_ONLY"
    AUTO_PUBLISH = "AUTO_PUBLISH"


class CaseClassification(StrictModel):
    classification_version: str = "case-classifier-v2"
    primary_domain: Domain
    secondary_domains: list[Domain] = Field(default_factory=list)
    market_archetype: MarketArchetype
    political_case_type: PoliticalCaseType | None = None
    politics_case: PoliticsCase | None = None
    resolution_mechanism: str
    underlying_entity: str | None = None
    action_or_metric: str | None = None
    time_horizon: str = "UNKNOWN"
    data_modality: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class CapabilityDecision(StrictModel):
    capability_id: str
    state: CapabilityState
    evidence_builder: str | None = None
    min_evidence_contract_version: int = Field(default=2, ge=1)
    point_in_time_supported: bool = False
    probability_mode: Literal["QUANTITATIVE_PROBABILITY", "PROBABILITY_INTERVAL", "QUALITATIVE_STATE"] = (
        "QUALITATIVE_STATE"
    )
    reason: str | None = None
    domain_status: Literal["COMPLETE", "PARTIAL", "INCOMPLETE"]
    public_draft_generated: bool


class EvidenceItem(StrictModel):
    """Canonical evidence; retain the raw snapshot separately from its checked field projection."""

    evidence_contract_version: Literal[2] = 2
    evidence_id: str
    source_type: str | None = None
    observed_at: datetime | None = None
    source_valid_as_of: datetime | None = None
    source_snapshot: dict[str, Any] = Field(default_factory=dict)
    source_id: str
    source_tier: Literal["T0", "T1", "T2", "T3", "T4", "T5"]
    artifact_hash: str
    canonical_url: str | None = None
    entity_ids: list[str] = Field(default_factory=list)
    market_id: str
    published_at: datetime | None = None
    first_seen_at: datetime
    retrieved_at: datetime
    valid_as_of: datetime
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    source_independence_group: str | None = None
    canonical_story_cluster_id: str | None = None
    origin_source_id: str | None = None
    syndication_parent_id: str | None = None
    entity_match_score: float = Field(ge=0, le=1)
    rule_relevance_score: float = Field(ge=0, le=1)
    temporal_relation_to_signal: Literal[
        "PIT_CONFIRMED", "PIT_RECONSTRUCTED", "CURRENT_ONLY", "POST_SIGNAL_CONTEXT", "UNKNOWN"
    ]
    structured_payload: dict[str, Any]
    sanitized_text: str | None = None
    prompt_injection_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def read_referenced_document(cls, value: Any) -> Any:
        from smart_money.infrastructure.sources.documents import hydrate_document

        if isinstance(value, dict):
            value = dict(value)
            for key in ("source_snapshot", "structured_payload"):
                if isinstance(value.get(key), dict):
                    value[key] = hydrate_document(value[key])
        return value

    @field_serializer("source_snapshot", "structured_payload")
    def serialize_document_reference(self, value: dict[str, Any]) -> dict[str, Any]:
        from smart_money.infrastructure.sources.documents import compact_document

        return compact_document(value)

    @field_serializer("sanitized_text")
    def serialize_text(self, value: str | None) -> str | None:
        return None if (self.source_snapshot.get("source_metadata") or {}).get("document_ref") else value


class EvidenceContractResult(StrictModel):
    contract_id: str
    contract_version: int = 2
    required_fields: list[str]
    satisfied_fields: list[str]
    missing_fields: list[str]
    pit_eligible_fields: list[str]
    pit_missing_fields: list[str] = Field(default_factory=list)
    field_evidence_ids: dict[str, list[str]] = Field(default_factory=dict)
    pit_field_evidence_ids: dict[str, list[str]] = Field(default_factory=dict)
    passed: bool


class SourceIntelligenceDecision(StrictModel):
    decision_version: str = "source-intelligence-v2"
    selected_source_ids: list[str]
    freshness_failures: list[str]
    freshness_status: Literal["UNCONFIRMED"] = "UNCONFIRMED"
    critical_gaps: list[str]
    requested_activities: list[str]
    independent_source_groups: list[str]
    search_budget_used: int = Field(ge=0)
    search_budget_limit: int = Field(ge=0)
    llm_invocation_required: bool
    evidence_contract_already_satisfied: bool
    activity_runs: list[dict[str, Any]] = Field(default_factory=list)


class PublicationPolicy(StrictModel):
    policy_version: str = "publication-policy-v2"
    publication_type: Literal[
        "SMART_MONEY_SIGNAL",
        "ANOMALOUS_WALLET_ALERT",
        "EVIDENCE_SUPPORTED_VIEW",
        "RULE_EDGE_ALERT",
        "MARKET_RESEARCH",
        "FOLLOW_UP_UPDATE",
        "RESEARCH_ONLY",
        "SUPPRESSED",
    ]
    status: Literal["READY_TO_PUBLISH", "REVIEW_REQUIRED", "SUPPRESSED", "VALIDATION_FAILED"]
    long_form_allowed: bool
    trade_time_edge_claims_allowed: bool
    reasons: list[str]
    wallet_display_label: Literal["已验证领域聪明钱", "高盈利观察账户", "异常新账户", "同步地址群", "普通候选账户"]
