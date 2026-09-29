"""Source registry, PIT normalization, independence dedup, and contracts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from smart_money.contracts import parse_utc as _date
from smart_money.research.contract_activity_recipes import EVIDENCE_CONTRACTS
from smart_money.research.contract_field_extractors import EXTRACTOR_VERSION, field_audit_binding
from smart_money.research.contracts import (
    CaseClassification,
    EvidenceContractResult,
    EvidenceItem,
    SourceIntelligenceDecision,
)
from smart_money.research.models import SignalCandidate

GENERIC_REQUIRED = ("market_predicate", "resolution_definition", "event_state", "signal_time")


class EvidenceSystem:
    version = "evidence-system-v2"

    def build(
        self,
        run_id: str,
        candidate: SignalCandidate,
        classification: CaseClassification,
        context: dict[str, Any],
        osint: list[dict[str, Any]],
        *,
        observed_at: datetime,
    ) -> tuple[list[EvidenceItem], EvidenceContractResult]:
        """Create canonical evidence directly from captured source snapshots."""
        sources: list[tuple[str, str, str | None, dict[str, Any], datetime, datetime | None]] = [
            (
                "signal",
                "SMART_MONEY_SIGNAL",
                "T1",
                candidate.model_dump(mode="json"),
                candidate.as_of,
                candidate.as_of,
            ),
            (
                "market",
                "POLYMARKET_MARKET_DIM",
                "T1",
                context.get("_market_source_snapshot", context.get("market")) or {},
                _date(context.get("market_obtained_at")) or observed_at,
                _date(context.get("market_obtained_at")),
            ),
            (
                "rules",
                "POLYMARKET_RULE_SNAPSHOT",
                "T1",
                context.get("rules") or {},
                _date((context.get("rules") or {}).get("snapshot_at")) or observed_at,
                _date((context.get("rules") or {}).get("snapshot_at")),
            ),
            (
                "rule_history",
                "POLYMARKET_RULE_HISTORY",
                "T1",
                {"snapshots": context.get("rule_history") or []},
                observed_at,
                None,
            ),
            (
                "cross_market",
                "CROSS_MARKET_CONTEXT",
                "T1",
                {
                    "related_markets": context.get("related_markets") or [],
                    "related_wallet_signals": context.get("related_wallet_signals") or [],
                },
                observed_at,
                None,
            ),
        ]
        for index, row in enumerate(osint, start=1):
            published = _date(row.get("published_at"))
            retrieved = _date(row.get("retrieved_at") or row.get("first_seen_at"))
            valid = (
                (_date(row.get("valid_as_of")) or published)
                if row.get("temporal_relation") == "BEFORE_SIGNAL"
                else None
            )
            sources.append(
                (
                    f"osint_{index}",
                    "DOMAIN_EXTERNAL_EVIDENCE",
                    row.get("source_tier"),
                    row,
                    retrieved or observed_at,
                    valid,
                )
            )
        items = [
            self._from_snapshot(
                candidate,
                classification,
                evidence_id=f"ev_{run_id.removeprefix('mas_')}_{key}",
                source_type=source_type,
                source_tier=tier,
                snapshot=payload,
                observed_at=observed,
                valid_as_of=valid,
            )
            for key, source_type, tier, payload, observed, valid in sources
            if payload
        ]
        deduped = self._deduplicate(items)
        return deduped, self.evaluate_contract(classification, deduped)

    @staticmethod
    def source_intelligence_decision(
        items: list[EvidenceItem],
        contract: EvidenceContractResult,
        *,
        search_budget_limit: int,
        search_budget_used: int = 0,
        activity_runs: list[dict[str, Any]] | None = None,
    ) -> SourceIntelligenceDecision:
        independent = sorted(
            {
                item.source_independence_group
                for item in items
                if item.source_independence_group and not item.prompt_injection_flags
            }
        )
        unresolved = list(dict.fromkeys([*contract.missing_fields, *contract.pit_missing_fields]))
        return SourceIntelligenceDecision(
            selected_source_ids=sorted({item.source_id for item in items}),
            freshness_failures=[],
            freshness_status="UNCONFIRMED",
            critical_gaps=unresolved,
            requested_activities=[f"FETCH_CONTRACT_FIELD:{field}" for field in unresolved],
            independent_source_groups=independent,
            search_budget_used=search_budget_used,
            search_budget_limit=search_budget_limit,
            llm_invocation_required=False,
            evidence_contract_already_satisfied=contract.passed,
            activity_runs=activity_runs or [],
        )

    def _from_snapshot(
        self,
        candidate: SignalCandidate,
        classification: CaseClassification,
        *,
        evidence_id: str,
        source_type: str,
        source_tier: str | None,
        snapshot: dict[str, Any],
        observed_at: datetime,
        valid_as_of: datetime | None,
    ) -> EvidenceItem:
        from smart_money.infrastructure.sources.documents import hydrate_document

        snapshot = hydrate_document(snapshot)
        artifact_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest()
        )
        raw = json.loads(json.dumps(snapshot, default=str))
        payload = _project_contract_fields(classification, source_type, raw)
        source_class = self._source_id(source_type, source_tier, payload)
        metadata = payload.get("source_metadata") or {}
        source_id = str(payload.get("source_id") or metadata.get("source_id") or source_class)
        url = self._canonical_url(str(payload.get("url") or "")) or None
        published_at = _date(payload.get("published_at"))
        # Publication time is not first-seen time.  External evidence discovered
        # after the signal must remain reconstructed/current-only instead of
        # becoming PIT merely because the article itself has an older timestamp.
        first_seen = _date(payload.get("first_seen_at")) or _date(payload.get("retrieved_at")) or observed_at
        retrieved = _date(payload.get("retrieved_at")) or observed_at
        valid = valid_as_of or first_seen
        temporal = self._temporal_status(valid_as_of, published_at, first_seen, candidate.as_of)
        modified = _date(payload.get("modified_at"))
        if modified and modified > candidate.as_of:
            temporal = "CURRENT_ONLY"
        text = str(payload.get("raw_text") or payload.get("summary") or payload.get("title") or "")
        flags = _injection_flags(text)
        tier = (
            cast(Literal["T0", "T1", "T2", "T3", "T4", "T5"], source_tier)
            if source_tier in {"T0", "T1", "T2", "T3", "T4", "T5"}
            else "T5"
        )
        origin = str(payload.get("origin_source_id") or payload.get("source_name") or source_id)
        cluster = str(
            payload.get("canonical_story_cluster_id")
            or hashlib.sha256(
                f"{origin}:{payload.get('title') or payload.get('content_hash') or artifact_hash}".lower().encode()
            ).hexdigest()[:20]
        )
        return EvidenceItem(
            evidence_id=evidence_id,
            source_type=source_type,
            observed_at=observed_at,
            source_valid_as_of=valid_as_of,
            source_snapshot=raw,
            source_id=source_id,
            source_tier=tier,
            artifact_hash=artifact_hash,
            canonical_url=url,
            entity_ids=[str(value) for value in payload.get("entity_ids", [])]
            or ([] if source_type == "DOMAIN_EXTERNAL_EVIDENCE" else [candidate.market_id, candidate.wallet.lower()]),
            market_id=candidate.market_id,
            published_at=published_at,
            first_seen_at=first_seen,
            retrieved_at=retrieved,
            valid_as_of=valid,
            effective_from=_date(payload.get("effective_from")),
            effective_to=_date(payload.get("effective_to")),
            source_independence_group=(str(payload.get("source_independence_group") or origin)),
            canonical_story_cluster_id=cluster,
            origin_source_id=origin,
            syndication_parent_id=payload.get("syndication_parent_id"),
            entity_match_score=float(
                payload.get("entity_match_score", 0 if source_type == "DOMAIN_EXTERNAL_EVIDENCE" else 1.0)
            ),
            rule_relevance_score=float(
                payload.get(
                    "rule_relevance_score",
                    1.0 if source_type in {"POLYMARKET_RULE_SNAPSHOT", "POLYMARKET_MARKET_DIM"} else 0.5,
                )
            ),
            temporal_relation_to_signal=temporal,
            structured_payload=payload,
            sanitized_text=_sanitize(text) or None,
            prompt_injection_flags=flags,
        )

    @staticmethod
    def _source_id(source_type: str, source_tier: str | None, payload: dict[str, Any]) -> str:
        if source_type in {"POLYMARKET_RULE_SNAPSHOT", "POLYMARKET_RULE_HISTORY"}:
            return "polymarket_rules"
        if source_type == "SMART_MONEY_SIGNAL":
            return "polygon_orderfilled"
        if source_type in {"POLYMARKET_MARKET_DIM", "CROSS_MARKET_CONTEXT"}:
            return "polymarket_market"
        tier = str(source_tier or payload.get("source_tier") or "")
        return {
            "T1": "domain_official",
            "T2": "trusted_media",
            "T3": "specialist_data",
        }.get(tier, "specialist_data")

    @staticmethod
    def _temporal_status(
        valid_as_of: datetime | None,
        published_at: datetime | None,
        first_seen: datetime,
        as_of: datetime,
    ) -> Literal["PIT_CONFIRMED", "PIT_RECONSTRUCTED", "CURRENT_ONLY", "POST_SIGNAL_CONTEXT", "UNKNOWN"]:
        if published_at and published_at > as_of:
            return "POST_SIGNAL_CONTEXT"
        if first_seen > as_of:
            return "CURRENT_ONLY"
        if valid_as_of and valid_as_of <= as_of:
            return "PIT_CONFIRMED"
        return "UNKNOWN"

    @staticmethod
    def _deduplicate(items: list[EvidenceItem]) -> list[EvidenceItem]:
        unique: dict[tuple[str | None, str, str], EvidenceItem] = {}
        for item in items:
            # Independence counts groups; evidence retention counts exact snapshots.
            # Revised or contradictory documents must reach the conflict checker.
            key = (item.source_type, item.source_id, item.artifact_hash)
            unique.setdefault(key, item)
        return sorted(unique.values(), key=lambda item: (item.valid_as_of, item.evidence_id))

    @staticmethod
    def evaluate_contract(
        classification: CaseClassification,
        items: list[EvidenceItem],
    ) -> EvidenceContractResult:
        required = list(EVIDENCE_CONTRACTS.get(classification.market_archetype, GENERIC_REQUIRED))
        fields, references, pit = contract_field_values(items)
        field_evidence_ids = {field: references[field] for field in required if field in fields}
        satisfied = list(field_evidence_ids)
        pit_ids = {
            item.evidence_id
            for item in usable_contract_evidence(items)
            if item.temporal_relation_to_signal in {"PIT_CONFIRMED", "PIT_RECONSTRUCTED"}
        }
        pit_field_evidence_ids = {
            field: [ref for ref in references[field] if ref in pit_ids] for field in satisfied if pit.get(field)
        }
        pit_eligible = [field for field in satisfied if field in pit_field_evidence_ids]
        missing = [field for field in required if field not in satisfied]
        pit_missing = [field for field in satisfied if field not in pit_eligible]
        return EvidenceContractResult(
            contract_id=f"{classification.market_archetype.value}.v2",
            required_fields=required,
            satisfied_fields=satisfied,
            missing_fields=missing,
            pit_eligible_fields=pit_eligible,
            pit_missing_fields=pit_missing,
            field_evidence_ids=field_evidence_ids,
            pit_field_evidence_ids=pit_field_evidence_ids,
            passed=not missing and set(satisfied).issubset(pit_eligible),
        )

    @staticmethod
    def _canonical_url(value: str) -> str:
        if not value:
            return ""
        parts = urlsplit(value)
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _contract_fields(payload: dict[str, Any]) -> dict[str, Any]:
    fields = payload.get("contract_fields")
    return fields if isinstance(fields, dict) else {}


def _project_contract_fields(
    classification: CaseClassification,
    source_type: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Expose only contract facts deterministically present in an evidence item.

    The projection never changes the item's timestamps, source, or PIT status. It
    merely gives the contract checker stable field names for data that was
    already present in the captured payload.
    """

    payload = dict(snapshot)
    projected: dict[str, Any] = {}
    raw_explicit_fields = payload.get("contract_fields")
    explicit_fields: dict[str, Any] = raw_explicit_fields if isinstance(raw_explicit_fields, dict) else {}
    payload.pop("contract_fields", None)
    raw_field_audit = payload.get("contract_field_audit")
    field_audit: dict[str, Any] = raw_field_audit if isinstance(raw_field_audit, dict) else {}
    raw_accepted_audit = field_audit.get("accepted")
    accepted_audit: dict[str, Any] = raw_accepted_audit if isinstance(raw_accepted_audit, dict) else {}
    if field_audit.get("extractor_version") == EXTRACTOR_VERSION and field_audit.get("binding") == field_audit_binding(
        snapshot
    ):
        projected.update({field: explicit_fields[field] for field in accepted_audit if field in explicit_fields})

    def put(field: str, value: Any) -> None:
        if value not in (None, "", [], {}) and field not in projected:
            projected[field] = value

    # A snapshot is readable evidence, not proof of fields guessed from its title,
    # first URL, market lifecycle dates or a missing timezone.
    if source_type == "POLYMARKET_RULE_SNAPSHOT":
        put("resolution_definition", payload.get("rules_text"))

    if source_type == "SMART_MONEY_SIGNAL":
        put("signal_time", payload.get("signal_at") or payload.get("as_of"))

    if source_type == "CROSS_MARKET_CONTEXT":
        related = payload.get("related_markets") or []
        if related:
            put("related_deadline_markets", related)

    if projected:
        payload["contract_fields"] = projected
    return payload


def _injection_flags(text: str) -> list[str]:
    patterns = {
        "IGNORE_INSTRUCTIONS": r"ignore (?:all|previous|prior) instructions",
        "SYSTEM_PROMPT_REQUEST": r"(?:show|reveal|print).{0,20}system prompt",
        "TOOL_EXECUTION_REQUEST": r"(?:run|execute).{0,20}(?:shell|sql|command|tool)",
    }
    return [name for name, pattern in patterns.items() if re.search(pattern, text, re.I)]


def _sanitize(text: str) -> str:
    cleaned = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", cleaned).strip()[:6000]


def usable_contract_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
    return [item for item in items if not item.prompt_injection_flags and item.entity_match_score >= 0.7]


def contract_field_values(
    items: list[EvidenceItem],
) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, bool]]:
    """Consume checked projections only; conflicting values stay unresolved."""
    fields: dict[str, Any] = {}
    references: dict[str, list[str]] = {}
    pit: dict[str, bool] = {}
    conflicts: set[str] = set()
    for item in usable_contract_evidence(items):
        for field, value in _contract_fields(item.structured_payload).items():
            if value is None or field in conflicts:
                continue
            if field in fields and fields[field] != value:
                conflicts.add(field)
                fields.pop(field)
                references.pop(field)
                pit.pop(field)
                continue
            fields[field] = value
            references.setdefault(field, []).append(item.evidence_id)
            pit[field] = pit.get(field, False) or item.temporal_relation_to_signal in {
                "PIT_CONFIRMED",
                "PIT_RECONSTRUCTED",
            }
    return fields, references, pit
