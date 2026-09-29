"""Deterministic parsers from registered source artifacts to contract facts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from smart_money.contracts import StrictModel
from smart_money.infrastructure.sources.tools import DomainFact, FetchedArtifact

DOMAIN_SOURCE_PARSER_VERSION = "domain-source-parsers-v2"

CRYPTO_EXPLICIT_PARSER_IDS = (
    "crypto.company-treasury.v1",
    "crypto.derivatives.v1",
    "crypto.etf-flow.v1",
    "crypto.exchange-listing.v1",
    "crypto.liquidations.v1",
    "crypto.onchain-treasury.v1",
    "crypto.premarket.v1",
    "crypto.price-window.v1",
    "crypto.project-official.v1",
    "crypto.regulatory-process.v1",
    "crypto.resolution-rules.v1",
    "crypto.sec-filings.v1",
    "crypto.spot.v1",
    "crypto.threshold-path.v1",
    "crypto.token-supply.v1",
)


class ArtifactParserResult(StrictModel):
    parser_id: str
    status: str
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    facts: list[DomainFact] = Field(default_factory=list)
    title: str | None = None
    published_at: datetime | None = None
    rejected_fields: dict[str, str] = Field(default_factory=dict)


Parser = Callable[[FetchedArtifact, dict[str, Any], set[str]], ArtifactParserResult]


def _crypto_parser(parser_id: str) -> Parser:
    def parse(artifact: FetchedArtifact, market: dict[str, Any], requested: set[str]) -> ArtifactParserResult:
        return _parse_crypto_explicit(artifact, market, requested, parser_id)

    return parse


class DomainSourceParserRegistry:
    def __init__(self) -> None:
        self.parsers: dict[str, Parser] = {}

    def register(self, parser_id: str, parser: Parser) -> None:
        if parser_id in self.parsers:
            raise ValueError(f"DUPLICATE_DOMAIN_SOURCE_PARSER:{parser_id}")
        self.parsers[parser_id] = parser

    def parse(
        self,
        parser_id: str,
        artifact: FetchedArtifact,
        market: dict[str, Any],
        requested_fields: list[str],
    ) -> ArtifactParserResult:
        try:
            parser = self.parsers[parser_id]
        except KeyError as exc:
            raise ValueError(f"UNREGISTERED_DOMAIN_SOURCE_PARSER:{parser_id}") from exc
        return parser(artifact, market, set(requested_fields))


def build_default_domain_source_parser_registry() -> DomainSourceParserRegistry:
    registry = DomainSourceParserRegistry()
    registry.register("macro.fed-decision.v1", _parse_macro_fed)
    registry.register("macro.release-calendar.v1", _parse_macro_calendar)
    registry.register("finance.sec-ipo.v1", _parse_sec_ipo)
    registry.register("politics.generic.v1", _parse_politics_generic)
    registry.register("politics.appointment.v1", _parse_politics_appointment)
    registry.register("tech.official-release.v1", _parse_tech_release)
    registry.register("tech.testing-signal.v1", _parse_tech_testing)
    registry.register("geopolitics.event-context.v1", _parse_geopolitics)
    registry.register("geopolitics.primary-confirmation.v1", _parse_geopolitics)
    for parser_id in CRYPTO_EXPLICIT_PARSER_IDS:
        registry.register(parser_id, _crypto_parser(parser_id))
    registry.register(
        "sports.fixture-scoreboard.v1",
        lambda artifact, market, requested: _parse_sports(artifact, market, requested, "sports.fixture-scoreboard.v1"),
    )
    registry.register(
        "sports.roster-availability.v1",
        lambda artifact, market, requested: _parse_sports(artifact, market, requested, "sports.roster-availability.v1"),
    )
    registry.register(
        "sports.market-odds.v1",
        lambda artifact, market, requested: _parse_sports(artifact, market, requested, "sports.market-odds.v1"),
    )
    registry.register("esports.series.v1", _parse_esports)
    return registry


def _parse_crypto_explicit(
    artifact: FetchedArtifact,
    market: dict[str, Any],
    requested: set[str],
    parser_id: str,
) -> ArtifactParserResult:
    """Extract declared structured fields; narrative interpretation belongs to the agents."""
    payload = _payload_dict(artifact)
    return _result(
        parser_id,
        artifact,
        requested,
        {field: _find(payload, field, _snake_to_camel(field)) for field in requested},
        {field: _snake_to_camel(field) for field in requested},
    )


def _parse_macro_fed(
    artifact: FetchedArtifact,
    _market: dict[str, Any],
    requested: set[str],
) -> ArtifactParserResult:
    payload = _payload_dict(artifact)
    values: dict[str, Any] = {}
    policy_range = _find(payload, "currentPolicyRange", "targetRange", "policyRange", "rateRange")
    values["current_policy_range"] = policy_range
    values["exact_meeting_time"] = _find(payload, "meetingTime", "decisionTime", "eventTime")
    values["market_implied_policy_distribution"] = _find(
        payload,
        "marketImpliedPolicyDistribution",
        "policyProbabilities",
        "probabilities",
    )
    values["official_speaker_timeline"] = _find(
        payload,
        "speakerTimeline",
        "speakerSchedule",
        "speakers",
    )
    aliases = {
        "current_policy_range": "currentPolicyRange",
        "exact_meeting_time": "meetingTime",
        "market_implied_policy_distribution": "policyProbabilities",
        "official_speaker_timeline": "speakerTimeline",
    }
    return _result("macro.fed-decision.v1", artifact, requested, values, aliases)


def _parse_macro_calendar(
    artifact: FetchedArtifact,
    _market: dict[str, Any],
    requested: set[str],
) -> ArtifactParserResult:
    payload = _payload_dict(artifact)
    releases = _find(payload, "pendingData", "upcomingReleases", "releaseCalendar", "releases")
    if isinstance(releases, dict) and isinstance(releases.get("results"), list):
        releases = releases["results"]
    return _result(
        "macro.release-calendar.v1",
        artifact,
        requested,
        {"pending_data_before_decision": releases},
        {"pending_data_before_decision": "upcomingReleases"},
    )


def _parse_sec_ipo(
    artifact: FetchedArtifact,
    _market: dict[str, Any],
    requested: set[str],
) -> ArtifactParserResult:
    payload = _payload_dict(artifact)
    issuer = payload.get("name") or _find(payload, "issuerName", "companyName")
    recent = _find(payload, "recent")
    filings = _sec_recent_filings(recent)
    forms = [str(row.get("form") or "").upper() for row in filings]
    if "424B4" in forms:
        status = "FINAL_PROSPECTUS_FILED"
        remaining = ["exchange trading or listing confirmation", "offering completion confirmation"]
    elif "EFFECT" in forms:
        status = "REGISTRATION_EFFECTIVE"
        remaining = ["final prospectus or pricing", "exchange listing", "offering completion"]
    elif any(form in {"S-1/A", "F-1/A"} for form in forms):
        status = "REGISTRATION_AMENDED_NOT_EFFECTIVE"
        remaining = ["SEC effectiveness", "final pricing", "exchange listing", "offering completion"]
    elif any(form in {"S-1", "F-1"} for form in forms):
        status = "REGISTRATION_FILED_NOT_EFFECTIVE"
        remaining = ["SEC effectiveness", "final pricing", "exchange listing", "offering completion"]
    else:
        status = None
        remaining = None
    latest = filings[0] if filings else {}
    values = {
        "issuer_identity": issuer,
        "official_filing_status": status,
        "exchange_or_regulator_source": artifact.canonical_url,
        "remaining_conditions": remaining,
    }
    result = _result(
        "finance.sec-ipo.v1",
        artifact,
        requested,
        values,
        {
            "issuer_identity": "issuerName",
            "official_filing_status": "filingStatus",
            "remaining_conditions": "remainingConditions",
        },
    )
    return result.model_copy(
        update={
            "title": f"SEC filing status for {issuer}" if issuer else result.title,
            "published_at": _datetime(latest.get("filingDate")) or result.published_at,
            "source_metadata": {
                **result.source_metadata,
                "secLatestFiling": latest or None,
                "secFormsChecked": forms[:20],
            },
        }
    )


def _parse_politics_appointment(
    artifact: FetchedArtifact,
    market: dict[str, Any],
    requested: set[str],
) -> ArtifactParserResult:
    payload = _payload_dict(artifact)
    status = _find(payload, "official_status", "appointmentStatus")
    identity = _find(payload, "person_and_office_identity", "personAndOffice")
    return _result(
        "politics.appointment.v1",
        artifact,
        requested,
        {
            "person_and_office_identity": identity,
            "authoritative_appointing_source": artifact.canonical_url,
            "official_status": status,
        },
        {
            "person_and_office_identity": "personAndOffice",
            "official_status": "appointmentStatus",
        },
    )


def _parse_politics_generic(
    artifact: FetchedArtifact,
    market: dict[str, Any],
    requested: set[str],
) -> ArtifactParserResult:
    """Parse one shared politics schema regardless of the market case type."""

    payload = _payload_dict(artifact)
    entities = _find(
        payload,
        "politicalEntities",
        "entities",
        "candidates",
        "candidate",
        "personAndOffice",
        "billOrCaseIdentity",
    )
    actor_status = _find(
        payload,
        "actorStatus",
        "candidateStatus",
        "appointmentStatus",
        "officialStatus",
        "billStatus",
        "caseStatus",
    )
    values = {
        "political_entities": entities,
        "jurisdiction": _find(payload, "jurisdiction", "state", "country", "district"),
        "official_process": _find(
            payload,
            "officialProcess",
            "electionProcess",
            "appointmentProcess",
            "legislativeProcess",
            "courtProcess",
        ),
        "actor_status": actor_status,
        "official_timeline": _find(
            payload,
            "officialTimeline",
            "electionCalendar",
            "campaignCalendar",
            "legislativeCalendar",
            "courtCalendar",
            "timeline",
        ),
        "polling_snapshot": _find(payload, "pollingSnapshot", "polls", "polling", "approvalPolling"),
        "campaign_finance_snapshot": _find(
            payload,
            "campaignFinanceSnapshot",
            "campaignFinance",
            "receiptsAndDisbursements",
            "fundraising",
        ),
        "court_or_legal_status": _find(
            payload,
            "courtOrLegalStatus",
            "courtStatus",
            "legalStatus",
            "docketStatus",
            "ruling",
        ),
        "official_statements": _find(
            payload,
            "officialStatements",
            "officialStatement",
            "statements",
        ),
    }
    aliases = {
        "political_entities": "politicalEntities",
        "jurisdiction": "jurisdiction",
        "official_process": "officialProcess",
        "actor_status": "actorStatus",
        "official_timeline": "officialTimeline",
        "polling_snapshot": "pollingSnapshot",
        "campaign_finance_snapshot": "campaignFinanceSnapshot",
        "court_or_legal_status": "courtOrLegalStatus",
        "official_statements": "officialStatements",
    }
    result = _result("politics.generic.v1", artifact, requested, values, aliases)
    published_at = _datetime(_find(payload, "publishedAt", "published_at", "updatedAt", "updated_at", "date"))
    return result.model_copy(update={"published_at": published_at or result.published_at})


def _parse_tech_release(
    artifact: FetchedArtifact,
    _market: dict[str, Any],
    requested: set[str],
) -> ArtifactParserResult:
    payload = artifact.structured_payload
    release = None
    if isinstance(payload, list):
        release = next((row for row in payload if isinstance(row, dict)), None)
    elif isinstance(payload, dict):
        release = payload
    text = _artifact_text(artifact)
    status = None
    published_at = None
    title = None
    if isinstance(release, dict) and any(
        key in release for key in ("tag_name", "prerelease", "draft", "published_at", "html_url")
    ):
        title = str(release.get("name") or release.get("tag_name") or "").strip() or None
        published_at = _datetime(release.get("published_at"))
        if release.get("draft") is True:
            status = "DRAFT_NOT_RELEASED"
        elif release.get("prerelease") is True:
            status = "PRERELEASE_AVAILABLE"
        elif release.get("published_at") or release.get("html_url"):
            status = "PUBLIC_RELEASE_AVAILABLE"
        text = f"{title or ''} {release.get('body') or ''} {artifact.text or ''}"
    milestones = _remaining_milestones(text)
    result = _result(
        "tech.official-release.v1",
        artifact,
        requested,
        {
            "official_product_status": status,
            "latest_official_statement": _source_reference(artifact, title),
            "remaining_milestones": milestones,
        },
        {
            "official_product_status": "productStatus",
            "remaining_milestones": "remainingMilestones",
        },
    )
    return result.model_copy(
        update={
            "title": title or result.title,
            "published_at": published_at or result.published_at,
        }
    )


def _parse_tech_testing(
    artifact: FetchedArtifact,
    market: dict[str, Any],
    requested: set[str],
) -> ArtifactParserResult:
    return _result(
        "tech.testing-signal.v1",
        artifact,
        requested,
        {
            "credible_leak_or_testing_evidence": _find(
                _payload_dict(artifact), "credible_leak_or_testing_evidence", "credibleTestingSignal"
            )
        },
        {"credible_leak_or_testing_evidence": "credibleTestingSignal"},
    )


def _parse_geopolitics(
    artifact: FetchedArtifact,
    market: dict[str, Any],
    requested: set[str],
) -> ArtifactParserResult:
    payload = _payload_dict(artifact)
    parser_id = (
        "geopolitics.primary-confirmation.v1"
        if "independent_primary_confirmation" in requested
        else "geopolitics.event-context.v1"
    )
    aliases = {
        "actor_and_target_identity": "actorAndTarget",
        "geographic_scope": "geographicScope",
        "independent_primary_confirmation": "primaryConfirmation",
    }
    return _result(
        parser_id,
        artifact,
        requested,
        {field: _find(payload, field, alias) for field, alias in aliases.items()},
        aliases,
    )


def _parse_sports(
    artifact: FetchedArtifact,
    _market: dict[str, Any],
    requested: set[str],
    parser_id: str,
) -> ArtifactParserResult:
    payload = _payload_dict(artifact)
    events = payload.get("events") if isinstance(payload.get("events"), list) else None
    event = next((row for row in (events or []) if isinstance(row, dict)), payload)
    competitions = event.get("competitions") if isinstance(event, dict) else None
    competition = next((row for row in (competitions or []) if isinstance(row, dict)), {})
    raw_competitors = competition.get("competitors")
    competitors: list[Any] = raw_competitors if isinstance(raw_competitors, list) else []
    home = _competitor(competitors, "home")
    away = _competitor(competitors, "away")
    status = _find(event, "status")
    if isinstance(status, dict):
        status = _find(status, "description", "detail", "name", "state")
    score_data = {
        "home": home.get("score"),
        "away": away.get("score"),
        "status": status,
    }
    score = score_data if any(value not in (None, "") for value in score_data.values()) else None
    values = {
        "exact_fixture_identity": (
            f"{away.get('name')} vs {home.get('name')}" if home.get("name") and away.get("name") else None
        ),
        "scheduled_start_time": event.get("date") if isinstance(event, dict) else None,
        "official_scoreboard": score,
        "roster_or_lineup": _find(payload, "rosters", "roster", "lineups", "lineup", "starters"),
        "injury_or_availability": _find(payload, "injuries", "injury", "availability", "playerStatus"),
        "market_odds_snapshot": _find(competition, "odds"),
    }
    aliases = {
        "exact_fixture_identity": "fixtureIdentity",
        "scheduled_start_time": "event_at",
        "official_scoreboard": "score",
        "roster_or_lineup": "roster",
        "injury_or_availability": "injuries",
        "market_odds_snapshot": "odds",
    }
    result = _result(parser_id, artifact, requested, values, aliases)
    metadata = dict(result.source_metadata)
    if home.get("name"):
        metadata["homeTeam"] = home["name"]
    if away.get("name"):
        metadata["awayTeam"] = away["name"]
    return result.model_copy(update={"source_metadata": metadata})


def _parse_esports(
    artifact: FetchedArtifact,
    _market: dict[str, Any],
    requested: set[str],
) -> ArtifactParserResult:
    payload = _payload_dict(artifact)
    root = _find(payload, "series", "match", "event")
    if not isinstance(root, dict):
        root = payload
    teams = _find(root, "teams", "competitors", "participants")
    team_names = []
    if isinstance(teams, list):
        for team in teams[:2]:
            if isinstance(team, dict):
                name = _find(team, "name", "displayName", "shortName")
            else:
                name = team
            if name:
                team_names.append(str(name))
    team_a = _find(root, "teamA", "homeTeam") or (team_names[0] if team_names else None)
    team_b = _find(root, "teamB", "awayTeam") or (team_names[1] if len(team_names) > 1 else None)
    values = {
        "exact_series_identity": f"{team_a} vs {team_b}" if team_a and team_b else None,
        "best_of_format": _find(root, "bestOf", "best_of", "seriesFormat"),
        "scheduled_start_time": _find(root, "startTime", "eventTime", "scheduledAt", "date"),
        "official_scoreboard": _find(root, "score", "seriesScore", "status", "state"),
        "roster_and_substitutions": _find(root, "rosters", "roster", "lineup", "substitutions"),
        "patch_or_map_context": _find(root, "patch", "gamePatch", "maps", "mapPool"),
    }
    result = _result(
        "esports.series.v1",
        artifact,
        requested,
        values,
        {
            "exact_series_identity": "seriesIdentity",
            "best_of_format": "bestOf",
            "scheduled_start_time": "event_at",
            "official_scoreboard": "score",
            "roster_and_substitutions": "rosters",
            "patch_or_map_context": "patch",
        },
    )
    return result.model_copy(
        update={
            "source_metadata": {
                **result.source_metadata,
                "teamA": team_a,
                "teamB": team_b,
            }
        }
    )


def _result(
    parser_id: str,
    artifact: FetchedArtifact,
    requested: set[str],
    values: dict[str, Any],
    aliases: dict[str, str],
) -> ArtifactParserResult:
    metadata: dict[str, Any] = {
        "contract_parser_id": parser_id,
        "contract_parser_version": DOMAIN_SOURCE_PARSER_VERSION,
    }
    facts: list[DomainFact] = []
    rejected: dict[str, str] = {}
    for field in sorted(requested):
        value = values.get(field)
        if value in (None, "", [], {}):
            rejected[field] = "FIELD_NOT_EXPLICIT_IN_ARTIFACT"
            continue
        alias = aliases.get(field)
        if alias:
            metadata[alias] = value
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(f"{artifact.artifact_id}|{field}|{serialized}".encode()).hexdigest()[:24]
        facts.append(
            DomainFact(
                fact_id=f"fact_{digest}",
                field_name=field,
                value=value,
                artifact_id=artifact.artifact_id,
                parser_id=parser_id,
                observed_at=artifact.retrieved_at,
            )
        )
    structured = artifact.structured_payload if isinstance(artifact.structured_payload, dict) else {}
    title = structured.get("title") if isinstance(structured.get("title"), str) else None
    return ArtifactParserResult(
        parser_id=parser_id,
        status="ok" if facts else "empty",
        source_metadata=metadata,
        facts=facts,
        title=title,
        rejected_fields=rejected,
    )


def _payload_dict(artifact: FetchedArtifact) -> dict[str, Any]:
    if isinstance(artifact.structured_payload, dict):
        return artifact.structured_payload
    if isinstance(artifact.structured_payload, list):
        return {"items": artifact.structured_payload}
    return {}


def _snake_to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _source_reference(artifact: FetchedArtifact, title: str | None) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "title": title,
        "url": artifact.canonical_url,
        "source_id": artifact.source_id,
    }


def _artifact_text(artifact: FetchedArtifact) -> str:
    payload = artifact.structured_payload
    structured = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) if payload is not None else ""
    return re.sub(r"\s+", " ", f"{artifact.text or ''} {structured}").strip()


def _find(value: Any, *names: str) -> Any:
    targets = {_normalize(name) for name in names}
    if isinstance(value, dict):
        for key, nested in value.items():
            if _normalize(str(key)) in targets and nested not in (None, "", [], {}):
                return nested
        for nested in value.values():
            found = _find(nested, *names)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(value, list):
        for nested in value[:100]:
            found = _find(nested, *names)
            if found not in (None, "", [], {}):
                return found
    return None


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _sec_recent_filings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    raw_forms = value.get("form")
    forms: list[Any] = raw_forms if isinstance(raw_forms, list) else []
    rows = []
    for index, form in enumerate(forms[:100]):
        rows.append(
            {
                "form": form,
                "filingDate": _indexed(value, "filingDate", index),
                "accessionNumber": _indexed(value, "accessionNumber", index),
                "primaryDocument": _indexed(value, "primaryDocument", index),
            }
        )
    return rows


def _indexed(value: dict[str, Any], key: str, index: int) -> Any:
    items = value.get(key)
    return items[index] if isinstance(items, list) and index < len(items) else None


def _remaining_milestones(text: str) -> list[str] | None:
    milestones = [item.strip() for item in re.findall(r"-\s*\[\s\]\s*([^\n]{3,160})", text)]
    if milestones:
        return milestones[:10]
    matches = re.findall(
        r"\b(?:coming soon|planned|not yet available|will be available)[^.]{0,120}",
        text,
        re.I,
    )
    return [re.sub(r"\s+", " ", item).strip() for item in matches[:10]] or None


def _competitor(competitors: list[Any], side: str) -> dict[str, Any]:
    selected = next(
        (row for row in competitors if isinstance(row, dict) and str(row.get("homeAway") or "").lower() == side),
        {},
    )
    raw_team = selected.get("team")
    team: dict[str, Any] = raw_team if isinstance(raw_team, dict) else {}
    return {
        "name": team.get("displayName") or team.get("name") or team.get("shortDisplayName"),
        "score": selected.get("score"),
    }


def _datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
