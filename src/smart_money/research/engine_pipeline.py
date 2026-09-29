"""Direct analysis stages: classification, evidence, specialists and verification."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from smart_money.infrastructure.llm.gateway import payload_hash
from smart_money.research.contract_activities import GapResolution
from smart_money.research.contract_activity_recipes import EVIDENCE_CONTRACTS
from smart_money.research.contract_field_extractors import quoted_rule_fields
from smart_money.research.contracts import (
    CapabilityDecision,
    CaseClassification,
    EvidenceContractResult,
    EvidenceItem,
)
from smart_money.research.crypto import CryptoEvidencePacket
from smart_money.research.engine_domain import DomainAnalysisMixin
from smart_money.research.engine_reports import ReportBuilderMixin
from smart_money.research.engine_support import _json
from smart_money.research.engine_verification import VerificationMixin
from smart_money.research.evidence import GENERIC_REQUIRED
from smart_money.research.evidence_verifier import project_verified_fields
from smart_money.research.models import (
    DomainExpertReport,
    EvidenceVerificationReport,
    RulesReport,
    SignalCandidate,
    SkepticReport,
    SpecialistReport,
    VerifiedClaim,
)
from smart_money.research.sector_router import SectorRoute, route_classification


@dataclass(frozen=True)
class CaseBundle:
    context: dict[str, Any]
    classification: CaseClassification
    capability: CapabilityDecision
    sector_route: SectorRoute


@dataclass
class EvidenceBundle:
    osint: list[dict[str, Any]]
    evidence: list[EvidenceItem]
    evidence_contract: EvidenceContractResult
    crypto_packet: CryptoEvidencePacket | None


@dataclass
class ReportBundle:
    wallet_report: SpecialistReport
    rules_report: RulesReport
    domain_report: DomainExpertReport


@dataclass(frozen=True)
class VerificationBundle:
    skeptic: SkepticReport
    claims: list[VerifiedClaim]
    verification_report: EvidenceVerificationReport


class PipelineMixin(DomainAnalysisMixin, ReportBuilderMixin, VerificationMixin):
    def _interpret_input(self, candidate: SignalCandidate, case: CaseBundle) -> tuple[SpecialistReport, RulesReport]:
        fields = list(EVIDENCE_CONTRACTS.get(case.classification.market_archetype, GENERIC_REQUIRED))
        with ThreadPoolExecutor(max_workers=2) as pool:
            wallet = pool.submit(self._wallet_forensics, candidate, case.context)
            rules = pool.submit(self._rules_analysis, candidate, case.context, fields)
            return wallet.result(), rules.result()

    def _load_case(self, run_id: str, candidate: SignalCandidate) -> CaseBundle:
        context = self.context
        classification = CaseClassification.model_validate(self.case_classifier.classify(candidate, context))
        capability = self.capability_registry.resolve(classification)
        sector_route = route_classification(
            classification,
            candidate,
            context,
        )
        self.runtime["sectorRouter"] = sector_route.model_dump(mode="json")
        self.runtime["caseClassification"] = classification.model_dump(mode="json")
        self.runtime["capabilityDecision"] = capability.model_dump(mode="json")
        self.runtime["predicateRoute"] = {
            "domain": sector_route.domain,
            "agent": sector_route.agent,
            "routeVersion": "case-classifier-v2",
            "reasons": classification.reasons,
            "conflicts": classification.conflicts,
        }
        return CaseBundle(context, classification, capability, sector_route)

    def _collect_evidence(
        self,
        run_id: str,
        candidate: SignalCandidate,
        case: CaseBundle,
        *,
        previous: EvidenceBundle | None = None,
        requested_fields: list[str] | None = None,
    ) -> EvidenceBundle:
        context, classification = case.context, case.classification
        if questions := self.runtime.get("ruleQuestions"):
            context = {**context, "rules": quoted_rule_fields(questions, context.get("rules") or {})}

        evidence_context = {**context, "_case_classification": classification.model_dump(mode="json")}
        evidence_context["rule_questions"] = self.runtime.get("ruleQuestions", [])
        osint = list(previous.osint) if previous else list(self.supplied_evidence)
        self.runtime["osint"] = {
            "status": "field-driven" if self.osint_enabled else "supplied-only",
            "resultCount": len(osint),
            "sourceStatus": dict(self.evidence_router.source_tool_registry.source_status),
        }
        observed_at = datetime.now(timezone.utc)
        evidence, evidence_contract = self.evidence_system.build(
            run_id,
            candidate,
            classification,
            context,
            osint,
            observed_at=observed_at,
        )

        evidence_context["_evidence_items"] = evidence

        def execute_activity(
            activity_id: str,
            requested_fields: list[str],
        ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            return self.evidence_router.execute_contract_activity(
                activity_id,
                requested_fields,
                candidate,
                evidence_context,
            )

        def merge_rows(additional_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            rows = list(osint)
            known = {_json(row) for row in rows}
            for row in additional_rows:
                identity = _json(row)
                if identity not in known:
                    rows.append(row)
                    known.add(identity)
            return rows

        existing_ids = {item.evidence_id for item in evidence}

        def evaluate_rows(additional_rows: list[dict[str, Any]]) -> tuple[EvidenceContractResult, list[str]]:
            combined, contract = self.evidence_system.build(
                run_id,
                candidate,
                classification,
                context,
                merge_rows(additional_rows),
                observed_at=observed_at,
            )
            evidence_context["_evidence_items"] = combined
            return contract, [item.evidence_id for item in combined if item.evidence_id not in existing_ids]

        prior_budget = self.runtime.get("researchBudget", {}) if previous else {}
        budget_limit = prior_budget.get(
            "limit", max(0, min(8, int(os.environ.get("POLYDATA_MAS_CONTRACT_SEARCH_BUDGET", "4"))))
        )
        budget_used = prior_budget.get("used", 0)
        collection_contract = evidence_contract
        if requested_fields is not None:
            gaps = set(evidence_contract.missing_fields + evidence_contract.pit_missing_fields)
            requested = set(requested_fields) & gaps
            collection_contract = evidence_contract.model_copy(
                update={
                    "missing_fields": [field for field in evidence_contract.missing_fields if field in requested],
                    "pit_missing_fields": [
                        field for field in evidence_contract.pit_missing_fields if field in requested
                    ],
                }
            )
        if self.osint_enabled:
            gap_resolution = self.contract_gap_resolver.resolve(
                classification,
                collection_contract,
                cache_key=payload_hash(
                    {
                        "input": self.runtime.get("inputIdentity"),
                        "candidate": candidate.model_dump(mode="json"),
                        "context": evidence_context,
                        "catalog": [
                            s.model_dump(mode="json") for s in self.evidence_router.source_tool_registry.catalog
                        ],
                    }
                ),
                search_budget_limit=max(0, budget_limit - budget_used),
                executor=execute_activity,
                evaluator=evaluate_rows,
            )
        else:
            gap_resolution = GapResolution(
                initial_missing_fields=list(
                    dict.fromkeys([*evidence_contract.missing_fields, *evidence_contract.pit_missing_fields])
                ),
                final_contract=evidence_contract,
                additional_rows=[],
                activity_runs=[],
                search_budget_used=0,
                search_budget_limit=0,
                stopped_reason="OFFLINE",
            )
        if gap_resolution.additional_rows:
            osint = merge_rows(gap_resolution.additional_rows)
            evidence, evidence_contract = self.evidence_system.build(
                run_id,
                candidate,
                classification,
                context,
                osint,
                observed_at=observed_at,
            )
        self.runtime["evidenceContract"] = evidence_contract.model_dump(mode="json")
        self.runtime["supplementGapResolution" if previous else "contractGapResolution"] = gap_resolution.model_dump(
            mode="json"
        )
        self.runtime["researchBudget"] = {
            "limit": budget_limit,
            "used": budget_used + gap_resolution.search_budget_used,
            "unit": "activity-cost",
            "shared_request_cost_deadline_contract": "UNCONFIRMED",
        }
        source_decision = self.evidence_system.source_intelligence_decision(
            evidence,
            evidence_contract,
            search_budget_limit=gap_resolution.search_budget_limit,
            search_budget_used=gap_resolution.search_budget_used,
            activity_runs=[item.model_dump(mode="json") for item in gap_resolution.activity_runs],
        )
        self.runtime["sourceIntelligence"] = source_decision.model_dump(mode="json")
        if evidence_contract.passed:
            crypto_packet = self._prepare_crypto_packet(
                candidate,
                classification,
                evidence,
            )
        else:
            crypto_packet = None
        return EvidenceBundle(osint, evidence, evidence_contract, crypto_packet)

    def _run_reports(
        self,
        run_id: str,
        candidate: SignalCandidate,
        case: CaseBundle,
        gathered: EvidenceBundle,
        initial: tuple[SpecialistReport, RulesReport],
    ) -> ReportBundle:
        wallet_report, rules_report = initial
        # Research may interpret available sources even when public release is unsupported.
        # Capability and completeness remain final publication-policy decisions.
        if any(
            not item.prompt_injection_flags
            and any(item.source_snapshot.get(key) for key in ("rules_text", "raw_text", "raw_data"))
            for item in gathered.evidence
        ):
            domain_report = self._domain_analysis(
                candidate, case.context, gathered.evidence, case.sector_route, gathered.crypto_packet
            )
        else:
            domain_report = self._not_invoked_domain_report(
                case.sector_route,
                gathered.osint,
                reason="ORIGINAL_MATERIAL_MISSING",
                unresolved=list(gathered.evidence_contract.missing_fields),
            )
        for report in (wallet_report, rules_report, domain_report):
            for claim in report.claims:
                if not claim.claim_id:
                    claim.claim_id = "claim_" + payload_hash({"agent": report.agent, "claim": claim.model_dump()})[7:31]
        return ReportBundle(wallet_report, rules_report, domain_report)

    def _verify_reports(
        self,
        run_id: str,
        candidate: SignalCandidate,
        case: CaseBundle,
        gathered: EvidenceBundle,
        reports: ReportBundle,
    ) -> VerificationBundle:
        skeptic = self._skeptic(
            candidate, reports.wallet_report, reports.rules_report, reports.domain_report, gathered.evidence
        )
        requested = sorted(
            set(skeptic.requested_fields)
            & set(gathered.evidence_contract.missing_fields + gathered.evidence_contract.pit_missing_fields)
        )
        self.runtime["supplement"] = {"rounds": 0, "requestedFields": requested, "status": "NOT_NEEDED"}
        budget = self.runtime.get("researchBudget", {})
        if requested:
            self.runtime["supplement"]["status"] = "OFFLINE" if not self.osint_enabled else "BUDGET_EXHAUSTED"
        if requested and self.osint_enabled and budget.get("used", 0) < budget.get("limit", 0):
            self.runtime["supplement"].update(rounds=1, status="ATTEMPTED")
            supplemented = self._collect_evidence(
                run_id, candidate, case, previous=gathered, requested_fields=requested
            )
            gathered.osint = supplemented.osint
            gathered.evidence = supplemented.evidence
            gathered.evidence_contract = supplemented.evidence_contract
            gathered.crypto_packet = supplemented.crypto_packet
            updated = self._run_reports(
                run_id, candidate, case, gathered, (reports.wallet_report, reports.rules_report)
            )
            reports.domain_report = updated.domain_report
            skeptic = self._skeptic(
                candidate, reports.wallet_report, reports.rules_report, reports.domain_report, gathered.evidence
            )
        self.context.setdefault("research_cutoff", datetime.now(timezone.utc).isoformat())
        self.runtime["researchCutoff"] = self.context["research_cutoff"]
        drafts = [*reports.wallet_report.claims, *reports.rules_report.claims, *reports.domain_report.claims]
        claims, verification_report = self._evidence_verifier(
            run_id,
            gathered.evidence,
            drafts,
            skeptic,
        )
        gathered.evidence = project_verified_fields(
            gathered.evidence, drafts, claims, verification_report, gathered.evidence_contract.required_fields
        )
        gathered.evidence_contract = self.evidence_system.evaluate_contract(case.classification, gathered.evidence)
        self.runtime["evidenceContract"] = gathered.evidence_contract.model_dump(mode="json")
        source_state = self.runtime["sourceIntelligence"]
        self.runtime["sourceIntelligence"] = self.evidence_system.source_intelligence_decision(
            gathered.evidence,
            gathered.evidence_contract,
            search_budget_limit=source_state["search_budget_limit"],
            search_budget_used=source_state["search_budget_used"],
            activity_runs=source_state["activity_runs"],
        ).model_dump(mode="json")
        gathered.crypto_packet = (
            self._prepare_crypto_packet(candidate, case.classification, gathered.evidence)
            if gathered.evidence_contract.passed
            else None
        )
        if gathered.crypto_packet is None:
            self.runtime.pop("cryptoAnalysis", None)
        return VerificationBundle(skeptic, claims, verification_report)
