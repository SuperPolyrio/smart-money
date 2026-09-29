"""Deterministic field extraction and validation for Evidence Contract activities."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from smart_money.research.contract_activity_recipes import CONTRACT_ACTIVITY_RECIPES


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


EXTRACTOR_VERSION = "contract-field-extractor-v3"

REGISTERED_FIELD_EXTRACTORS: dict[str, frozenset[str]] = {
    "RULES_RESOLUTION_SOURCE": frozenset(
        {
            "resolution_definition",
            "completion_definition",
            "deadline_and_timezone",
            "appointment_or_removal_definition",
            "exact_definition_of_release",
            "qualifying_action_definition",
            "exact_bucket_definition",
            "settlement_observation_source",
            "observation_timezone",
            "threshold_definition",
            "window_start",
            "window_end",
            "resolution_price_source",
            "threshold",
            "endpoint_time",
            "qualifying_mention_definition",
            "counting_window",
            "deduplication_rule",
        }
    ),
    "RELATED_MARKET_CONTEXT": frozenset({"related_deadline_markets"}),
    "MACRO_BLS_RELEASES": frozenset({"latest_inflation_release", "latest_labor_release"}),
    "WEATHER_REGISTERED_DATA": frozenset(
        {"station_identity", "location_identity", "forecast_distribution", "forecast_horizon"}
    ),
    "MENTIONS_REGISTERED_DATA": frozenset({"speaker_identity", "authoritative_transcript_or_feed"}),
}
REGISTERED_FIELD_EXTRACTORS.update(
    {activity_id: recipe.supported_fields for activity_id, recipe in CONTRACT_ACTIVITY_RECIPES.items()}
)
RULE_PARAMETER_FIELDS = (
    REGISTERED_FIELD_EXTRACTORS["RULES_RESOLUTION_SOURCE"] | REGISTERED_FIELD_EXTRACTORS["CRYPTO_RESOLUTION_RULES"]
)


@dataclass(frozen=True)
class FieldDecision:
    value: Any = None
    source_key: str | None = None
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.reason is None and self.value not in (None, "", [], {})


def extract_contract_fields(
    activity_id: str,
    requested_fields: list[str],
    row: dict[str, Any],
    market: dict[str, Any],
) -> dict[str, Any]:
    """Attach only validated, field-specific values and an acceptance audit."""

    result = dict(row)
    accepted: dict[str, Any] = {}
    accepted_audit: dict[str, Any] = {}
    rejected_audit: dict[str, Any] = {}
    for field in dict.fromkeys(requested_fields):
        decision = _extract(activity_id, field, result, market)
        validation_error = decision.reason or _validation_error(field, decision.value)
        if validation_error:
            rejected_audit[field] = {
                "extractor": EXTRACTOR_VERSION,
                "reason": validation_error,
                "source_key": decision.source_key,
            }
            continue
        accepted[field] = decision.value
        accepted_audit[field] = {
            "extractor": EXTRACTOR_VERSION,
            "source_key": decision.source_key,
        }
    if accepted:
        result["contract_fields"] = accepted
    else:
        result.pop("contract_fields", None)
    result["contract_field_audit"] = {
        "activity_id": activity_id,
        "extractor_version": EXTRACTOR_VERSION,
        "accepted": accepted_audit,
        "rejected": rejected_audit,
    }
    result["contract_field_audit"]["binding"] = field_audit_binding(result)
    return result


def field_audit_binding(row: dict[str, Any]) -> str:
    """Bind acceptance to the captured input, extracted values and field locators."""
    audit = _as_dict(row.get("contract_field_audit"))
    bound = {
        "snapshot": {key: value for key, value in row.items() if key != "contract_field_audit"},
        "audit": {key: value for key, value in audit.items() if key != "binding"},
    }
    return hashlib.sha256(
        json.dumps(bound, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def quoted_rule_fields(questions: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, Any]:
    """Copy literal, typed rule parameters; interpretation remains a verifier task."""
    text = str(rules.get("rules_text") or "")
    accepted: dict[str, Any] = {}
    locators: dict[str, Any] = {}
    conflicts: set[str] = set()
    for question in questions:
        field, value = question["field"], question.get("value")
        quote, literal = question["rule_quote"], question.get("value_quote")
        if field not in RULE_PARAMETER_FIELDS or not quote or quote not in text or not literal or literal not in quote:
            continue
        if _validation_error(field, value):
            continue
        # No title parsing, natural-language operator guessing, date/year or timezone completion.
        expected = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if literal != expected:
            continue
        if not numeric_value_is_quoted(value, quote):
            continue
        if field in accepted and accepted[field] != value:
            conflicts.add(field)
        accepted[field] = value
        locators[field] = {"extractor": EXTRACTOR_VERSION, "source_key": "/rules_text", "quote": quote}
    for field in conflicts:
        accepted.pop(field)
        locators.pop(field)
    row = {
        **rules,
        "contract_fields": accepted,
        "contract_field_audit": {
            "activity_id": "RULES_ANALYST_QUOTED_PARAMETERS",
            "extractor_version": EXTRACTOR_VERSION,
            "accepted": locators,
            "rejected": {field: {"reason": "CONFLICTING_RULE_VALUES"} for field in conflicts},
        },
    }
    row["contract_field_audit"]["binding"] = field_audit_binding(row)
    return row


def numeric_value_is_quoted(value: Any, quote: str) -> bool:
    """Numeric proposals copy source values; semantic support cannot authorize arithmetic."""
    if isinstance(value, dict):
        return all(numeric_value_is_quoted(v, quote) for v in value.values())
    if isinstance(value, list):
        return all(numeric_value_is_quoted(v, quote) for v in value)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return True
    literal = json.dumps(value)
    return math.isfinite(value) and bool(re.search(rf"(?<![\w.+-]){re.escape(literal)}(?!\w|\.\d)", quote))


def summarize_field_audit(rows: list[dict[str, Any]], requested_fields: list[str]) -> dict[str, Any]:
    accepted: dict[str, list[dict[str, Any]]] = {}
    rejected: dict[str, list[dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        audit = _as_dict(row.get("contract_field_audit"))
        for field, detail in (audit.get("accepted") or {}).items():
            accepted.setdefault(str(field), []).append({"row": index, **dict(detail)})
        for field, detail in (audit.get("rejected") or {}).items():
            rejected.setdefault(str(field), []).append({"row": index, **dict(detail)})
    unresolved = [field for field in dict.fromkeys(requested_fields) if field not in accepted]
    return {
        "extractorVersion": EXTRACTOR_VERSION,
        "accepted": accepted,
        "rejected": rejected,
        "unresolvedFields": unresolved,
    }


def _extract(activity_id: str, field: str, row: dict[str, Any], market: dict[str, Any]) -> FieldDecision:
    if field not in REGISTERED_FIELD_EXTRACTORS.get(activity_id, frozenset()):
        return FieldDecision(reason="UNREGISTERED_FIELD_EXTRACTOR")
    recipe = CONTRACT_ACTIVITY_RECIPES.get(activity_id)
    extractor_family = recipe.extractor_family if recipe else activity_id
    if extractor_family == "CRYPTO":
        metadata = _as_dict(row.get("source_metadata"))
        if (
            row.get("retrieval_method") == "REGISTERED_CRYPTO_PRICE_ADAPTER"
            and activity_id in {"CRYPTO_PRICE_WINDOW", "CRYPTO_THRESHOLD_PATH", "CRYPTO_SPOT_SNAPSHOT"}
            and row.get("raw_data") is not None
            and metadata.get("source_artifact_ids")
        ):
            return FieldDecision(_as_dict(row.get("contract_fields")).get(field), f"contract_fields.{field}")
        if recipe and metadata.get("contract_parser_id") == recipe.parser_id:
            raw = _as_dict(row.get("raw_data"))
            head, *tail = field.split("_")
            camel = head + "".join(part.title() for part in tail)
            for key in (field, camel):
                if key in raw:
                    return FieldDecision(raw[key], f"raw_data.{key}")
        return FieldDecision(reason=f"ORIGINAL_STRUCTURED_FIELD_REQUIRED:{field}")
    extractor = {
        "RULES_RESOLUTION_SOURCE": _rules_field,
        "RELATED_MARKET_CONTEXT": _related_field,
        "MACRO_BLS_RELEASES": _explicit_only,
        "MACRO": _macro_field,
        "FINANCE": _finance_field,
        "POLITICS": _politics_field,
        "TECH": _tech_field,
        "GEOPOLITICS": _geopolitics_field,
        "SPORTS": _sports_field,
        "ESPORTS": _esports_field,
        "WEATHER_REGISTERED_DATA": _weather_field,
        "MENTIONS_REGISTERED_DATA": _mentions_field,
    }.get(extractor_family)
    if extractor is None:
        return FieldDecision(reason="UNREGISTERED_FIELD_EXTRACTOR")
    return extractor(field, row, market)


def _explicit_only(field: str, _row: dict[str, Any], _market: dict[str, Any]) -> FieldDecision:
    return FieldDecision(reason=f"EXPLICIT_STRUCTURED_FIELD_MISSING:{field}")


def _rules_field(field: str, row: dict[str, Any], market: dict[str, Any]) -> FieldDecision:
    if field == "resolution_definition" and row.get("raw_text"):
        return FieldDecision(row["raw_text"], "row.raw_text")
    return FieldDecision(reason=f"VALIDATED_RULE_FIELD_REQUIRED:{field}")


def _related_field(field: str, row: dict[str, Any], _market: dict[str, Any]) -> FieldDecision:
    if field != "related_deadline_markets":
        return FieldDecision(reason=f"NO_RELATED_MARKET_EXTRACTOR:{field}")
    value = row.get("related_markets")
    return FieldDecision(value, "related_markets", "RELATED_MARKETS_MISSING" if not value else None)


def _macro_field(field: str, row: dict[str, Any], _market: dict[str, Any]) -> FieldDecision:
    aliases = {
        "current_policy_range": ("currentPolicyRange", "targetRange", "policyRange", "rateRange"),
        "exact_meeting_time": ("meetingTime", "decisionTime", "eventTime", "event_at"),
        "market_implied_policy_distribution": (
            "marketImpliedPolicyDistribution",
            "policyProbabilities",
            "probabilities",
        ),
        "official_speaker_timeline": ("speakerTimeline", "speakers", "speakerSchedule"),
        "pending_data_before_decision": ("pendingData", "upcomingReleases", "releaseCalendar"),
    }
    return _from_metadata(row, aliases.get(field, ()), field)


def _finance_field(field: str, row: dict[str, Any], _market: dict[str, Any]) -> FieldDecision:
    aliases = {
        "issuer_identity": ("issuer", "issuerName", "companyName"),
        "official_filing_status": ("filingStatus", "registrationStatus", "formStatus"),
        "remaining_conditions": ("remainingConditions", "conditions", "remainingMilestones"),
    }
    if field == "exchange_or_regulator_source":
        return _official_source(row, field)
    if field == "official_filing_status" and str(row.get("source_tier")) != "T1":
        return FieldDecision(reason="OFFICIAL_SOURCE_REQUIRED")
    return _from_metadata(row, aliases.get(field, ()), field)


def _politics_field(field: str, row: dict[str, Any], _market: dict[str, Any]) -> FieldDecision:
    aliases = {
        "political_entities": (
            "politicalEntities",
            "entities",
            "candidates",
            "candidate",
            "personAndOffice",
            "billOrCaseIdentity",
        ),
        "jurisdiction": ("jurisdiction", "state", "country", "district"),
        "official_process": (
            "officialProcess",
            "electionProcess",
            "appointmentProcess",
            "legislativeProcess",
            "courtProcess",
        ),
        "actor_status": (
            "actorStatus",
            "candidateStatus",
            "appointmentStatus",
            "officialStatus",
            "billStatus",
            "caseStatus",
        ),
        "official_timeline": (
            "officialTimeline",
            "electionCalendar",
            "campaignCalendar",
            "legislativeCalendar",
            "courtCalendar",
            "timeline",
        ),
        "polling_snapshot": ("pollingSnapshot", "polls", "polling", "approvalPolling"),
        "campaign_finance_snapshot": (
            "campaignFinanceSnapshot",
            "campaignFinance",
            "receiptsAndDisbursements",
            "fundraising",
        ),
        "court_or_legal_status": (
            "courtOrLegalStatus",
            "courtStatus",
            "legalStatus",
            "docketStatus",
            "ruling",
        ),
        "official_statements": ("officialStatements", "officialStatement", "statements"),
    }
    official_only = {
        "official_process",
        "actor_status",
        "official_timeline",
        "campaign_finance_snapshot",
        "court_or_legal_status",
        "official_statements",
    }
    if field in official_only and str(row.get("source_tier")) != "T1":
        return FieldDecision(reason="OFFICIAL_SOURCE_REQUIRED")
    return _from_metadata(row, aliases.get(field, ()), field)


def _tech_field(field: str, row: dict[str, Any], _market: dict[str, Any]) -> FieldDecision:
    aliases = {
        "official_product_status": ("productStatus", "releaseStatus", "launchStatus"),
        "remaining_milestones": ("remainingMilestones", "milestones", "launchChecklist"),
    }
    if field in aliases:
        if field == "official_product_status" and str(row.get("source_tier")) != "T1":
            return FieldDecision(reason="OFFICIAL_SOURCE_REQUIRED")
        return _from_metadata(row, aliases[field], field)
    if field == "latest_official_statement":
        if str(row.get("source_tier")) != "T1":
            return FieldDecision(reason="OFFICIAL_SOURCE_REQUIRED")
        return _source_fact(row, field)
    if field == "credible_leak_or_testing_evidence":
        metadata = _as_dict(row.get("source_metadata"))
        if not metadata.get("credibleTestingSignal"):
            return FieldDecision(reason="REGISTERED_TESTING_SIGNAL_NOT_PARSED")
        return _from_metadata(row, ("credibleTestingSignal",), field)
    return FieldDecision(reason=f"NO_TECH_EXTRACTOR:{field}")


def _geopolitics_field(field: str, row: dict[str, Any], _market: dict[str, Any]) -> FieldDecision:
    aliases = {
        "actor_and_target_identity": ("actorAndTarget", "actor", "target"),
        "geographic_scope": ("location", "geographicScope", "country", "region"),
    }
    if field in aliases:
        return _from_metadata(row, aliases[field], field)
    if field == "independent_primary_confirmation":
        if str(row.get("source_tier")) not in {"T1", "T2"}:
            return FieldDecision(reason="T1_OR_T2_CONFIRMATION_REQUIRED")
        metadata = _as_dict(row.get("source_metadata"))
        if not metadata.get("primaryConfirmation"):
            return FieldDecision(reason="PRIMARY_CONFIRMATION_NOT_PARSED")
        return _from_metadata(row, ("primaryConfirmation",), field)
    return FieldDecision(reason=f"NO_GEOPOLITICS_EXTRACTOR:{field}")


def _sports_field(field: str, row: dict[str, Any], market: dict[str, Any]) -> FieldDecision:
    metadata = _as_dict(row.get("source_metadata"))
    parser_id = str(metadata.get("contract_parser_id") or "")
    if not parser_id.startswith("sports."):
        return FieldDecision(reason="REGISTERED_SPORTS_SOURCE_REQUIRED")
    if field == "exact_fixture_identity":
        teams = _metadata_values(row, ("homeTeam", "awayTeam", "teamA", "teamB"))
        value = " vs ".join(str(value) for value in teams[:2]) if len(teams) >= 2 else None
        return FieldDecision(value, "source_metadata.teams", "EXACT_FIXTURE_IDENTITY_MISSING" if not value else None)
    if field == "scheduled_start_time":
        return _from_metadata(row, ("event_at", "commenceTime", "eventTime", "startTime"), field)
    if field == "official_scoreboard":
        metadata = _as_dict(row.get("source_metadata"))
        score = metadata.get("score")
        if score in (None, "", [], {}):
            score = _metadata_value(row, ("score", "homeScore", "awayScore", "matchStatus", "status"))
        return FieldDecision(
            score,
            "source_metadata.score",
            "SCOREBOARD_VALUE_MISSING" if score in (None, "", [], {}) else None,
        )
    if field == "roster_or_lineup":
        return _from_metadata(row, ("roster", "lineup", "startingLineup", "starters"), field)
    if field == "injury_or_availability":
        return _from_metadata(row, ("injury", "injuries", "availability", "playerStatus"), field)
    if field == "market_odds_snapshot":
        metadata = _as_dict(row.get("source_metadata"))
        registered_odds = str(metadata.get("contract_parser_id") or "") == "sports.market-odds.v1"
        if not registered_odds:
            return FieldDecision(reason="REGISTERED_ODDS_SOURCE_REQUIRED")
        odds = _metadata_subset(row, ("odds", "price", "probability", "consensus", "spread"))
        return FieldDecision(
            odds,
            "source_metadata.odds",
            "STRUCTURED_ODDS_MISSING" if not odds else None,
        )
    return FieldDecision(reason=f"NO_SPORTS_EXTRACTOR:{field}:{market.get('title') or ''}")


def _esports_field(field: str, row: dict[str, Any], _market: dict[str, Any]) -> FieldDecision:
    metadata = _as_dict(row.get("source_metadata"))
    registered = str(metadata.get("contract_parser_id") or "") == "esports.series.v1"
    if not registered:
        return FieldDecision(reason="REGISTERED_ESPORTS_SOURCE_REQUIRED")
    aliases = {
        "best_of_format": ("bestOf", "best_of", "seriesFormat", "format"),
        "scheduled_start_time": ("event_at", "startTime", "eventTime"),
        "official_scoreboard": ("score", "seriesScore", "status", "state"),
        "roster_and_substitutions": ("roster", "rosters", "substitutions", "lineup"),
        "patch_or_map_context": ("patch", "gamePatch", "maps", "mapPool", "map"),
    }
    if field == "exact_series_identity":
        teams = _metadata_values(row, ("teamA", "teamB", "homeTeam", "awayTeam"))
        value = " vs ".join(str(value) for value in teams[:2]) if len(teams) >= 2 else None
        return FieldDecision(value, "source_metadata.series", "EXACT_SERIES_IDENTITY_MISSING" if not value else None)
    return _from_metadata(row, aliases.get(field, ()), field)


def _weather_field(field: str, row: dict[str, Any], _market: dict[str, Any]) -> FieldDecision:
    aliases = {
        "station_identity": ("icao", "station", "stationId"),
        "location_identity": ("city", "location", "cityId"),
        "forecast_distribution": ("bins", "markets", "probabilities", "scenarios"),
    }
    if field in aliases:
        return _from_metadata(row, aliases[field], field)
    if field == "forecast_horizon":
        explicit = _from_metadata(row, ("forecastHorizon", "forecast_horizon_minutes"), field)
        if explicit.accepted:
            return explicit
        for key in ("daily", "hourly"):
            values, source_key = _metadata_value_with_key(row, (key,))
            horizon = _forecast_horizon(values)
            if horizon:
                return FieldDecision(horizon, source_key)
        return FieldDecision(reason="STRUCTURED_FIELD_MISSING:forecast_horizon")
    return FieldDecision(reason=f"NO_WEATHER_EXTRACTOR:{field}")


def _mentions_field(field: str, row: dict[str, Any], _market: dict[str, Any]) -> FieldDecision:
    if field == "speaker_identity":
        return _from_metadata(row, ("speaker", "account", "author"), field)
    if field == "authoritative_transcript_or_feed":
        return _official_source(row, field)
    return FieldDecision(reason=f"NO_MENTIONS_EXTRACTOR:{field}")


def _official_source(row: dict[str, Any], field: str) -> FieldDecision:
    if str(row.get("source_tier")) != "T1":
        return FieldDecision(reason="OFFICIAL_SOURCE_REQUIRED")
    url = row.get("url") or row.get("publisher_url")
    return FieldDecision(url, "row.url", f"OFFICIAL_URL_MISSING:{field}" if not url else None)


def _source_fact(row: dict[str, Any], field: str) -> FieldDecision:
    if not row.get("url") or not (row.get("raw_text") or row.get("raw_data")):
        return FieldDecision(reason=f"SOURCE_FACT_IDENTITY_MISSING:{field}")
    return FieldDecision(
        {
            "title": row.get("title"),
            "summary": row.get("summary"),
            "url": row.get("url"),
            "published_at": row.get("published_at"),
            "source_name": row.get("source_name"),
        },
        "row.source_fact",
    )


def _from_metadata(row: dict[str, Any], aliases: tuple[str, ...], field: str) -> FieldDecision:
    if not aliases:
        return FieldDecision(reason=f"NO_FIELD_ALIASES:{field}")
    value, key = _metadata_value_with_key(row, aliases)
    return FieldDecision(value, key, f"STRUCTURED_FIELD_MISSING:{field}" if value in (None, "", [], {}) else None)


def _metadata_value(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    return _metadata_value_with_key(row, aliases)[0]


def _metadata_value_with_key(row: dict[str, Any], aliases: tuple[str, ...]) -> tuple[Any, str | None]:
    metadata = _as_dict(row.get("source_metadata"))
    for alias in aliases:
        value = metadata.get(alias)
        if value not in (None, "", [], {}):
            return value, f"source_metadata.{alias}"
    return None, None


def _metadata_values(row: dict[str, Any], aliases: tuple[str, ...]) -> list[Any]:
    metadata = _as_dict(row.get("source_metadata"))
    return [metadata[alias] for alias in aliases if metadata.get(alias) not in (None, "", [], {})]


def _metadata_subset(row: dict[str, Any], terms: tuple[str, ...]) -> dict[str, Any]:
    metadata = _as_dict(row.get("source_metadata"))
    return {key: metadata[key] for key in terms if metadata.get(key) not in (None, "", [], {})}


def _forecast_horizon(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    timestamps = []
    for item in value:
        if not isinstance(item, dict):
            continue
        timestamp = item.get("date") or item.get("timestamp") or item.get("time")
        if timestamp not in (None, ""):
            timestamps.append(timestamp)
    if not timestamps:
        return None
    return {"start": timestamps[0], "end": timestamps[-1], "points": len(timestamps)}


def _validation_error(field: str, value: Any) -> str | None:
    if value in (None, "", [], {}):
        return "EMPTY_VALUE"
    if isinstance(value, float) and not math.isfinite(value):
        return "NON_FINITE_VALUE"
    if field in {"confirmed_sale", "threshold_hit"} and not isinstance(value, bool):
        return "BOOLEAN_VALUE_REQUIRED"
    if field == "comparison_operator" and not (isinstance(value, str) and value in {">", ">=", "<", "<=", "=="}):
        return "EXPLICIT_COMPARISON_OPERATOR_REQUIRED"
    if field in {
        "exact_meeting_time",
        "scheduled_start_time",
        "window_start",
        "window_end",
        "endpoint_time",
        "exact_window_start",
        "exact_window_end",
        "measurement_time",
        "tge_time",
        "deposit_open_time",
        "trading_open_time",
        "withdrawal_open_time",
        "sale_start",
        "sale_end",
        "effective_time",
    } and not _is_datetime(value):
        return "INVALID_DATETIME"
    if field in {
        "settlement_observation_source",
        "exchange_or_regulator_source",
        "authoritative_appointing_source",
        "authoritative_transcript_or_feed",
    } and not _is_http_url(value):
        return "INVALID_SOURCE_URL"
    if field in {"resolution_price_source", "resolution_source", "price_source", "supply_source"} and not (
        isinstance(value, (str, dict)) and bool(value)
    ):
        return "REGISTERED_SOURCE_ID_REQUIRED"
    if field in {
        "threshold",
        "point_in_time_price",
        "volatility_estimate",
        "start_price",
        "end_price",
        "window_high",
        "window_low",
        "realized_window_return",
        "realized_window_volatility",
        "current_price",
        "distance_to_threshold",
        "minute_path_coverage",
        "realized_volatility",
        "remaining_time",
        "endpoint_price",
        "volatility_to_endpoint",
        "total_supply",
        "circulating_supply",
        "premarket_price",
        "spot_price_at_measurement",
        "calculated_fdv",
        "fdv_threshold",
    } and not (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return "NUMERIC_VALUE_REQUIRED"
    if field == "deadline_and_timezone" and not (
        isinstance(value, dict) and value.get("deadline") and value.get("timezone")
    ):
        return "DEADLINE_AND_TIMEZONE_REQUIRED"
    if field in {
        "forecast_distribution",
        "market_implied_policy_distribution",
        "high_low_path",
        "minute_or_finer_path",
        "scenario_distribution",
        "onchain_transfers",
        "exchange_listings",
        "official_timeline",
    } and not isinstance(value, (dict, list)):
        return "STRUCTURED_DISTRIBUTION_REQUIRED"
    if field.endswith("definition") or field == "deduplication_rule":
        if not isinstance(value, str) or len(value.strip()) < 10:
            return "DEFINITION_TEXT_TOO_SHORT"
    return None


def _is_datetime(value: Any) -> bool:
    if isinstance(value, datetime):
        return value.tzinfo is not None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).tzinfo is not None
    except (TypeError, ValueError):
        return False


def _is_http_url(value: Any) -> bool:
    parts = urlsplit(str(value))
    return parts.scheme in {"http", "https"} and bool(parts.netloc)
