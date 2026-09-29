"""Contract activity orchestration across admitted source families."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from smart_money.research.contract_activity_recipes import CONTRACT_ACTIVITY_RECIPES

if TYPE_CHECKING:
    from smart_money.research.domain_evidence import DomainEvidenceRouter


def execute_contract_activity(
    self: DomainEvidenceRouter,
    activity_id: str,
    requested_fields: list[str],
    candidate: Any,
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Execute one whitelisted deterministic contract activity."""

    market = dict(context.get("market") or {})
    market["rule_questions"] = context.get("rule_questions", [])
    selected_registered_source_ids: set[str] | None = None
    rules = context.get("rules") or {}
    if not market.get("rules_current") and rules.get("rules_text"):
        market["rules_current"] = rules["rules_text"]
    if activity_id in {"RULES_RESOLUTION_SOURCE", "CRYPTO_RESOLUTION_RULES", "RELATED_MARKET_CONTEXT"}:
        # Frozen rules and context already entered the canonical evidence system.
        # Do not create another snapshot with guessed fields or rewritten dates.
        return self._contract_activity_result(
            activity_id,
            requested_fields,
            [],
            market,
            {"status": "unresolved", "reason": "VALIDATED_LOCAL_FIELDS_MISSING", "requestCount": 0},
        )
    if activity_id in {"MACRO_BLS_RELEASES", "WEATHER_REGISTERED_DATA", "MENTIONS_REGISTERED_DATA"}:
        return self._contract_activity_result(
            activity_id,
            requested_fields,
            [],
            market,
            {"status": "unsupported", "reason": "REGISTERED_ADAPTER_UNAVAILABLE", "requestCount": 0},
        )
    if recipe := CONTRACT_ACTIVITY_RECIPES.get(activity_id):
        unexpected = sorted(set(requested_fields) - recipe.supported_fields)
        if unexpected:
            raise ValueError(f"UNSUPPORTED_ACTIVITY_FIELDS:{activity_id}:{','.join(unexpected)}")
        rows = []
        runtime: dict[str, Any] = {"status": "empty", "requestCount": 0}
        if recipe.domain == "CRYPTO":
            price_rows, runtime = self._registered_crypto_price_rows(
                activity_id, candidate, {**context, "market": market}
            )
            rows.extend(price_rows)
        unresolved = [field for field in requested_fields if field not in self._parsed_field_names(rows)]
        research_rows: list[dict[str, Any]] = []
        research_runtime: dict[str, Any] = {
            "status": "skipped",
            "reason": "CONTRACT_FIELDS_ALREADY_PRESENT",
            "requestCount": 0,
        }
        requests_already_used = int(runtime.get("requestCount") or 0)
        remaining_research_requests = max(0, 4 - requests_already_used)
        if unresolved and remaining_research_requests >= 1:
            research_rows, research_runtime, admitted_source_ids = self.research_gateway.collect(
                activity_id=activity_id,
                parser_id=recipe.parser_id,
                requested_fields=unresolved,
                market=market,
                as_of=candidate.as_of,
                max_requests=remaining_research_requests,
            )
            selected_registered_source_ids = admitted_source_ids
            rows.extend(research_rows)
        elif unresolved:
            research_runtime = {
                "status": "skipped",
                "reason": "ACTIVITY_REQUEST_BUDGET_EXHAUSTED",
                "requestCount": 0,
                "requestLimit": 4,
                "requestsAlreadyUsed": requests_already_used,
            }
        runtime = {
            **runtime,
            "requestCount": int(runtime.get("requestCount") or 0) + int(research_runtime.get("requestCount") or 0),
            "researchGateway": research_runtime,
            "researchArtifactCount": len(research_rows),
        }
    else:
        raise ValueError(f"UNREGISTERED_CONTRACT_ACTIVITY:{activity_id}")
    return self._contract_activity_result(
        activity_id,
        requested_fields,
        rows,
        market,
        {
            **runtime,
            "activityId": activity_id,
            "registeredSources": sorted(selected_registered_source_ids or []),
            "sourceStatus": dict(self.source_tool_registry.source_status),
            "parserId": (
                CONTRACT_ACTIVITY_RECIPES[activity_id].parser_id if activity_id in CONTRACT_ACTIVITY_RECIPES else None
            ),
        },
    )
