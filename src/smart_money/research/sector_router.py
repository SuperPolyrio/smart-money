"""Project the unique case classification into the domain report route."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from smart_money.contracts import StrictModel
from smart_money.markets.official_classification import (
    OfficialCategoryRef,
    OfficialClassificationExtractor,
    OfficialTagRef,
    SportsMetadataRef,
)
from smart_money.research.contracts import CaseClassification, PoliticsCase
from smart_money.research.domain_experts import definition_for
from smart_money.research.models import DomainAgentName, DomainName, SignalCandidate

ROUTE_VERSION = "case-classifier-v2"
RouteMethod = Literal["PREDICATE_CLASSIFIER"]

CanonicalDomain = Literal[
    "POLITICS_ELECTIONS",
    "GEOPOLITICS",
    "SPORTS",
    "ESPORTS",
    "CRYPTO",
    "FINANCE",
    "MACRO_ECONOMY",
    "TECH_SCIENCE",
    "CULTURE_ENTERTAINMENT",
    "WEATHER_CLIMATE",
    "MENTIONS_SOCIAL",
    "GENERAL",
]

_SECTOR_BY_DOMAIN: dict[CanonicalDomain, str] = {
    "POLITICS_ELECTIONS": "POLITICS.ELECTIONS",
    "GEOPOLITICS": "POLITICS.GEOPOLITICS",
    "SPORTS": "SPORTS.GENERAL",
    "ESPORTS": "SPORTS.ESPORTS",
    "CRYPTO": "CRYPTO.GENERAL",
    "FINANCE": "FINANCE.MARKETS",
    "MACRO_ECONOMY": "ECONOMICS.MACRO",
    "TECH_SCIENCE": "TECH.SCIENCE",
    "CULTURE_ENTERTAINMENT": "CULTURE.ENTERTAINMENT",
    "WEATHER_CLIMATE": "WEATHER.CLIMATE",
    "MENTIONS_SOCIAL": "MENTIONS.SOCIAL",
    "GENERAL": "OTHER",
}


class SectorRoute(StrictModel):
    requested_sector_id: str
    routed_sector_id: str
    domain: DomainName
    agent: DomainAgentName
    political_case_type: str | None = None
    politics_case: PoliticsCase | None = None
    secondary_domain: DomainName | None = None
    secondary_agent: DomainAgentName | None = None
    method: RouteMethod
    route_version: str = ROUTE_VERSION
    confidence: float
    reason: str
    official_categories: list[OfficialCategoryRef] = Field(default_factory=list)
    official_tags: list[OfficialTagRef] = Field(default_factory=list)
    sports_metadata: list[SportsMetadataRef] = Field(default_factory=list)
    matched_families: list[DomainName] = Field(default_factory=list)


def route_classification(
    classification: CaseClassification,
    candidate: SignalCandidate,
    context: dict[str, Any],
) -> SectorRoute:
    """Retain official metadata without running a second classification."""
    metadata = OfficialClassificationExtractor().extract(
        context.get("market") or {}, embedded_market=candidate.evidence.get("market") or {}
    )
    primary = definition_for(classification.primary_domain.value)
    secondary = definition_for(classification.secondary_domains[0].value) if classification.secondary_domains else None
    return SectorRoute(
        requested_sector_id=candidate.sector_id,
        routed_sector_id=_SECTOR_BY_DOMAIN[primary.domain],
        domain=primary.domain,
        agent=primary.agent,
        political_case_type=classification.political_case_type.value if classification.political_case_type else None,
        politics_case=classification.politics_case,
        secondary_domain=secondary.domain if secondary else None,
        secondary_agent=secondary.agent if secondary else None,
        method="PREDICATE_CLASSIFIER",
        confidence=classification.confidence,
        reason=";".join(classification.reasons),
        official_categories=metadata.categories,
        official_tags=metadata.tags,
        sports_metadata=metadata.sports,
        matched_families=[primary.domain, *([secondary.domain] if secondary else [])],
    )
