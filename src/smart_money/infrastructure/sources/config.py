"""Validate the single, explicitly maintained external source catalogue."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, StrictBool, TypeAdapter, model_validator

from smart_money.contracts import StrictModel


class SourceAccess(StrictModel):
    mode: Literal["web", "adapter", "manual"]
    adapter: str | None
    docs_url: str | None
    render: Literal["http", "browser"] = "http"

    @model_validator(mode="after")
    def validate_adapter(self) -> SourceAccess:
        if (self.mode == "adapter") != bool(self.adapter):
            raise ValueError("Only adapter mode requires a registered adapter name")
        if self.mode != "web" and self.render != "http":
            raise ValueError("Only web sources can request browser rendering")
        return self


class SourceDiscovery(StrictModel):
    kind: Literal["feed", "listing", "sitemap"]
    url: str


class ExternalSource(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    enabled: StrictBool
    market_types: list[str]
    regions: list[str]
    entities: list[str]
    role: Literal["official_record", "issuer", "original_research", "newsroom", "data_provider", "platform"]
    provides: list[str]
    domains: list[str]
    entry_urls: list[str]
    access: SourceAccess
    discovery: list[SourceDiscovery] = Field(default_factory=list)
    notes: str

    @model_validator(mode="after")
    def validate_locations(self) -> ExternalSource:
        if not self.domains or not self.entry_urls or not self.provides:
            raise ValueError("Source domains, entry URLs and capabilities must be explicit")
        for host in self.domains:
            parsed = urlsplit("https://" + host)
            if (
                not host
                or host != host.lower()
                or parsed.hostname != host
                or any(char in host for char in "/:*@?#[] \\")
                or "." not in host
            ):
                raise ValueError("domains must contain exact hostnames")
        if self.discovery and self.access.mode != "web":
            raise ValueError("Only web sources support document discovery")
        urls = [*self.entry_urls, *(d.url for d in self.discovery)]
        urls += [self.access.docs_url] if self.access.docs_url else []
        for url in urls:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in self.domains
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or "\\" in url
                or any(ord(c) <= 32 for c in url)
            ):
                raise ValueError("Source URLs must use HTTPS and a declared hostname")
        return self


def load_external_sources(path: str | Path | None = None) -> list[ExternalSource]:
    """Read one file; an absent implicit catalogue admits no external sources."""
    configured = path or os.environ.get("SMART_MONEY_SOURCES_FILE")
    target = Path(configured).expanduser() if configured else Path("external_sources.json")
    if not target.exists() and not configured:
        return []
    sources = TypeAdapter(list[ExternalSource]).validate_json(target.read_text(encoding="utf-8"))
    ids = [source.id for source in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate external source id")
    return sources


# Field-to-capability semantics live here; concrete publishers live only in JSON.
FIELD_CAPABILITIES: dict[str, set[str]] = {
    "exact_fixture_identity": {"match_report", "match_preview", "team_statement"},
    "scheduled_start_time": {"match_report", "match_preview"},
    "official_scoreboard": {"official_scoreboard"},
    "roster_or_lineup": {"squad_announcement", "team_statement", "match_preview"},
    "injury_or_availability": {"injury_update", "injury_report"},
    "current_policy_range": {"rate_decision"},
    "exact_meeting_time": {"meeting_calendar"},
    "official_speaker_timeline": {"policy_statement", "meeting_minutes"},
    "pending_data_before_decision": {"meeting_calendar"},
    "polling_data": {"poll_results"},
    "polling_methodology": {"poll_methodology"},
    "poll_release_schedule": {"poll_release_schedule"},
    "project_identity": {"project_announcement"},
    "token_identity": {"project_announcement"},
    "official_token_status": {"project_announcement"},
    "tge_time": {"project_announcement"},
    "contract_address_status": {"project_announcement"},
    "claim_status": {"project_announcement"},
    "transferability_requirement": {"project_announcement"},
    "exchange_listing_status": {"project_announcement"},
    "total_supply": {"project_announcement"},
    "circulating_supply": {"project_announcement"},
    "price_path": {"candles", "trade_history"},
    "window_high": {"candles"},
    "window_low": {"candles"},
    "spot_price": {"market_price"},
}


def source_fingerprint(source: ExternalSource) -> str:
    raw = json.dumps(source.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def match_sources(
    sources: list[ExternalSource],
    fields: list[str],
    market: dict,
) -> tuple[set[str], list[dict]]:
    """Match each checked fact question; missing context never grants publisher scope."""
    selected: set[str] = set()
    audit = []
    questions = {q["field"]: q for q in market.get("rule_questions", []) if isinstance(q, dict) and q.get("field")}
    for field in fields:
        question = questions.get(field, {})
        required_ids = set(question.get("source_ids", []))
        # A settlement publisher does not own unrelated roster/injury background.
        background = field in {"roster_or_lineup", "injury_or_availability"}
        urls = question.get("urls") or (
            [] if background else re.findall(r"https?://[^\s<>()\\]+", str(market.get("rules_current") or ""))
        )
        hosts = {urlsplit(url).hostname for url in urls}
        regions = set(question.get("regions") or market.get("regions") or [])
        entities = {str(e).casefold() for e in question.get("entities") or market.get("entities") or []}
        capability = FIELD_CAPABILITIES.get(field, {field})
        matched = []
        rejected = {}
        for source in sources:
            bound = source.id in required_ids or bool(hosts & set(source.domains))
            if required_ids and source.id not in required_ids:
                continue
            if not required_ids and hosts and not bound:
                continue
            if not source.enabled:
                reason = "DISABLED"
            elif not capability.intersection(source.provides):
                reason = "CAPABILITY_MISMATCH"
            elif regions and "GLOBAL" not in source.regions and not regions.intersection(source.regions):
                reason = "REGION_MISMATCH"
            elif not regions and "GLOBAL" not in source.regions and not bound:
                reason = "REGION_UNRESOLVED"
            elif source.entities and entities and not entities.intersection(e.casefold() for e in source.entities):
                reason = "ENTITY_MISMATCH"
            elif source.entities and not entities and not bound:
                reason = "ENTITY_UNRESOLVED"
            elif (
                capability & {"rate_decision", "meeting_calendar", "policy_statement", "meeting_minutes"}
                and source.role != "official_record"
            ):
                reason = "ROLE_MISMATCH"
            elif capability & {"poll_results", "poll_methodology"} and source.role != "original_research":
                reason = "ROLE_MISMATCH"
            elif capability & {"project_announcement"} and source.role != "issuer":
                reason = "ROLE_MISMATCH"
            else:
                matched.append(source.id)
                selected.add(source.id)
                continue
            rejected[source.id] = reason
        audit.append(
            {
                "field": field,
                "provides": sorted(capability),
                "sources": matched,
                "requiredSources": sorted(required_ids),
                "rejected": rejected,
                "status": "MATCHED" if matched else "SOURCE_GAP",
            }
        )
    return selected, audit
