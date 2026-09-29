"""One claim verifier: deterministic provenance gates plus located semantic reviews."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from typing import Any

from smart_money.research.contract_activity_recipes import CONTRACT_ACTIVITY_RECIPES
from smart_money.research.contract_field_extractors import (
    RULE_PARAMETER_FIELDS,
    _validation_error,
    numeric_value_is_quoted,
)
from smart_money.research.contracts import EvidenceItem
from smart_money.research.models import (
    ClaimDraft,
    EvidenceClaimReview,
    EvidenceReference,
    EvidenceVerificationReport,
    VerifiedClaim,
)

SOURCE_KEYS = {
    "SMART_MONEY_SIGNAL": "signal",
    "POLYMARKET_MARKET_DIM": "market",
    "POLYMARKET_RULE_SNAPSHOT": "rules",
    "POLYMARKET_RULE_HISTORY": "rules",
    "DOMAIN_EXTERNAL_EVIDENCE": "osint",
    "CROSS_MARKET_CONTEXT": "cross_market",
}


def deterministic_claims(
    run_id: str,
    evidence: list[EvidenceItem],
    claims: list[ClaimDraft],
    *,
    research_cutoff: datetime | None = None,
) -> list[VerifiedClaim]:
    result = []
    for index, claim in enumerate(claims, start=1):
        ids = [
            item.evidence_id
            for item in evidence
            if not item.prompt_injection_flags
            and (item.entity_match_score >= 0.7 or "entity_match_score" not in item.source_snapshot)
            and (
                item.evidence_id in claim.evidence_ids
                if claim.evidence_ids
                else SOURCE_KEYS.get(item.source_type or "") in claim.evidence_keys
            )
            and (
                item.temporal_relation_to_signal in {"PIT_CONFIRMED", "PIT_RECONSTRUCTED"}
                if claim.time_scope == "TRADE_TIME"
                else research_cutoff is not None and item.retrieved_at <= research_cutoff
            )
        ]
        result.append(
            VerifiedClaim(
                claim_id=claim.claim_id or f"claim_{run_id.removeprefix('mas_')}_{index}",
                statement=claim.statement,
                modality=claim.modality,
                confidence=claim.confidence,
                time_scope=claim.time_scope,
                supporting_evidence_ids=ids,
                status="UNSUPPORTED",
                depends_on=claim.depends_on,
                required_qualifications=claim.required_qualifications,
                critical=claim.modality not in {"HYPOTHESIS", "UNKNOWN"},
            )
        )
    return result


def fallback_report(claims: list[VerifiedClaim]) -> EvidenceVerificationReport:
    return EvidenceVerificationReport(
        summary="语义核验未完成，全部主张保留为未验证。",
        reviews=[
            EvidenceClaimReview(
                claim_index=index,
                verdict="AMBIGUOUS",
                rationale="引用可解析不等于原文支持；语义核验尚未完成。",
                evidence_ids=claim.supporting_evidence_ids,
            )
            for index, claim in enumerate(claims, start=1)
        ],
    )


def reference_is_located(reference: EvidenceReference, item: EvidenceItem) -> bool:
    """A citation must point into the preserved source, never another role's summary."""
    if not reference.locator.startswith("/") or not reference.quote.strip():
        return False
    root = reference.locator.split("/", 2)[1]
    if root in {"contract_field_audit", "semantic_field_audit"} or (
        item.source_type == "POLYMARKET_RULE_SNAPSHOT" and root == "contract_fields"
    ):
        return False
    if item.source_type == "DOMAIN_EXTERNAL_EVIDENCE":
        if root == "contract_fields":
            field = reference.locator.split("/")[2] if reference.locator.count("/") >= 2 else ""
            if field not in item.structured_payload.get("contract_fields", {}):
                return False
        elif root not in {"raw_text", "raw_data"}:
            return False
    value: Any = item.source_snapshot
    try:
        for part in reference.locator[1:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) and key.isdecimal() else value[key]
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    if isinstance(value, str):
        return reference.quote in value
    return reference.quote == json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def supported_review_ids(
    review: EvidenceClaimReview,
    allowed_ids: list[str],
    evidence: list[EvidenceItem],
) -> list[str]:
    items = {item.evidence_id: item for item in evidence if not item.prompt_injection_flags}
    allowed = set(allowed_ids)
    references = review.references
    if not references or any(
        ref.evidence_id not in allowed
        or ref.evidence_id not in items
        or not reference_is_located(ref, items[ref.evidence_id])
        or (items[ref.evidence_id].entity_match_score < 0.7 and not review.entity_matches_market)
        for ref in references
    ):
        return []
    located = {ref.evidence_id for ref in references}
    # References already identify each source; reject conflicting legacy declarations.
    return sorted(located) if not review.evidence_ids or located == set(review.evidence_ids) else []


def complete_reviews(report: EvidenceVerificationReport, count: int) -> bool:
    return len(report.reviews) == count and {r.claim_index for r in report.reviews} == set(range(1, count + 1))


def apply_report(
    claims: list[VerifiedClaim],
    report: EvidenceVerificationReport,
    evidence: list[EvidenceItem],
) -> tuple[list[VerifiedClaim], EvidenceVerificationReport]:
    if not complete_reviews(report, len(claims)):
        report = fallback_report(claims)
    by_index = {review.claim_index: review for review in report.reviews}
    final, normalized = [], []
    for index, claim in enumerate(claims, start=1):
        review = by_index[index]
        ids = supported_review_ids(review, claim.supporting_evidence_ids, evidence)
        supported = (
            review.verdict == "SUPPORTED"
            and bool(ids)
            and not report.future_leakage_detected
            and set(claim.required_qualifications) <= set(review.preserved_qualifications)
        )
        final.append(
            claim.model_copy(
                update={
                    "supporting_evidence_ids": ids,
                    "status": "VERIFIED" if supported else "UNSUPPORTED",
                }
            )
        )
        normalized.append(
            review.model_copy(
                update={
                    "evidence_ids": ids,
                    "verdict": review.verdict if supported or review.verdict != "SUPPORTED" else "AMBIGUOUS",
                }
            )
        )
    # A short fixed-point pass validates references without another graph/report layer.
    proven: set[str] = set()
    if len({claim.claim_id for claim in final}) == len(final):
        for _ in final:
            previous = len(proven)
            proven.update(c.claim_id for c in final if c.status == "VERIFIED" and set(c.depends_on) <= proven)
            if len(proven) == previous:
                break
    for index, claim in enumerate(final):
        if claim.status == "VERIFIED" and claim.claim_id not in proven:
            final[index] = claim.model_copy(update={"status": "UNSUPPORTED"})
            normalized[index] = normalized[index].model_copy(
                update={
                    "verdict": "AMBIGUOUS",
                    "rationale": "Claim dependency is missing, unsupported, duplicate or cyclic.",
                }
            )
    return final, report.model_copy(update={"reviews": normalized})


def project_verified_fields(
    evidence: list[EvidenceItem],
    drafts: list[ClaimDraft],
    claims: list[VerifiedClaim],
    report: EvidenceVerificationReport,
    required_fields: list[str],
) -> list[EvidenceItem]:
    """Promote located, verified field claims into the existing canonical items."""
    computed = set().union(
        *(
            CONTRACT_ACTIVITY_RECIPES[name].supported_fields
            for name in (
                "CRYPTO_PRICE_WINDOW",
                "CRYPTO_THRESHOLD_PATH",
                "CRYPTO_SPOT_SNAPSHOT",
            )
        )
    )
    computed.update({"calculated_fdv", "calculation_formula", "threshold_status"})
    items = {item.evidence_id: item for item in evidence}
    proposals: dict[str, dict[str, list[tuple[Any, dict[str, Any]]]]] = {}
    for draft, claim, review in zip(drafts, claims, report.reviews, strict=True):
        field = draft.contract_field
        if (
            claim.status != "VERIFIED"
            or draft.modality != "FACT"
            or field is None
            or field not in required_fields
            or field in computed
            or _validation_error(field, draft.field_value)
            or len(claim.supporting_evidence_ids) != 1
            or not numeric_value_is_quoted(draft.field_value, "\n".join(ref.quote for ref in review.references))
        ):
            continue
        for evidence_id in claim.supporting_evidence_ids:
            if field in RULE_PARAMETER_FIELDS and items[evidence_id].source_type != "POLYMARKET_RULE_SNAPSHOT":
                continue
            proposals.setdefault(evidence_id, {}).setdefault(field, []).append(
                (
                    draft.field_value,
                    {
                        "claim": draft.model_dump(mode="json"),
                        "review": review.model_dump(mode="json"),
                    },
                )
            )
    result = []
    for item in evidence:
        payload = deepcopy(item.structured_payload)
        fields = dict(payload.get("contract_fields") or {})
        # Literal rule parameters may guide bounded retrieval, but semantic rejection
        # must remove them from final calculations and contract satisfaction.
        if (payload.get("contract_field_audit") or {}).get("activity_id") == "RULES_ANALYST_QUOTED_PARAMETERS":
            fields = {}
        if item.entity_match_score < 0.7:
            fields = {}
        audit = {}
        for field, values in proposals.get(item.evidence_id, {}).items():
            all_values = [value for value, _ in values]
            if field in fields:
                all_values.append(fields[field])
            conflict = any(value != all_values[0] for value in all_values[1:])
            if conflict:
                fields.pop(field, None)
            else:
                fields[field] = all_values[0]
            audit[field] = {
                "artifact_hash": item.artifact_hash,
                "conflict": conflict,
                "reviews": [detail for _, detail in values],
            }
        payload["contract_fields"] = fields
        if audit:
            payload["semantic_field_audit"] = audit
        result.append(
            item.model_copy(
                update={
                    "structured_payload": payload,
                    # Unknown material identity is only bound after located semantic review.
                    # Previously unbound structured fields were cleared above.
                    "entity_match_score": 1.0 if audit else item.entity_match_score,
                }
            )
        )
    return result
