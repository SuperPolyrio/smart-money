"""Registered official source retrieval and contract field extraction."""

from __future__ import annotations

from typing import Any

from smart_money.research.contract_field_extractors import extract_contract_fields, summarize_field_audit


def _parsed_field_names(rows: list[dict[str, Any]]) -> set[str]:
    fields: set[str] = set()
    for row in rows:
        explicit = row.get("contract_fields")
        if isinstance(explicit, dict):
            fields.update(str(field) for field in explicit if explicit[field] not in (None, "", [], {}))
        metadata = row.get("source_metadata")
        facts = metadata.get("domain_facts") if isinstance(metadata, dict) else None
        if isinstance(facts, list):
            fields.update(
                str(fact.get("field_name"))
                for fact in facts
                if isinstance(fact, dict) and fact.get("field_name") and fact.get("value") not in (None, "", [], {})
            )
    return fields


def _contract_activity_result(
    activity_id: str,
    requested_fields: list[str],
    rows: list[dict[str, Any]],
    market: dict[str, Any],
    runtime: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    annotated = [extract_contract_fields(activity_id, requested_fields, row, market) for row in rows]
    audit = summarize_field_audit(annotated, requested_fields)
    audit["sourceStatus"] = runtime.get("sourceStatus", {})
    audit["retrievalReason"] = runtime.get("reason")
    if runtime.get("researchGateway"):
        audit["researchGateway"] = runtime["researchGateway"]
    return annotated, {**runtime, "fieldAudit": audit}
