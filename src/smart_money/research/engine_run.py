"""Produce one report, then validate its actual draft before the sole policy decision."""

from __future__ import annotations

from typing import TYPE_CHECKING

from smart_money.infrastructure.llm.gateway import aggregate_execution_mode
from smart_money.publication.release_validator import validate_release
from smart_money.research.models import MasResult, PolicyDecision, SignalCandidate
from smart_money.research.wallet_profile import wallet_display_label

if TYPE_CHECKING:
    from smart_money.research.engine import MasEngine


def run_analysis(self: MasEngine, run_id: str, candidate: SignalCandidate) -> MasResult:
    case = self._load_case(run_id, candidate)
    initial = self._interpret_input(candidate, case)
    gathered = self._collect_evidence(run_id, candidate, case)
    reports = self._run_reports(run_id, candidate, case, gathered, initial)
    verified = self._verify_reports(run_id, candidate, case, gathered, reports)
    publication = self._editor(verified.claims, wallet_display_label(candidate), verified.skeptic)
    result = MasResult(
        result_schema_version=3,
        run_id=run_id,
        classification=case.classification,
        capability=case.capability,
        evidence_contract=gathered.evidence_contract,
        candidate=candidate,
        wallet_report=reports.wallet_report,
        rules_report=reports.rules_report,
        domain_report=reports.domain_report,
        skeptic_report=verified.skeptic,
        evidence=gathered.evidence,
        claims=verified.claims,
        verification_report=verified.verification_report,
        publication=publication,
        agent_runtime=self.runtime,
        policy=PolicyDecision(
            status="REVIEW_REQUIRED",
            publication_type="RESEARCH_ONLY",
            long_form_allowed=False,
            trade_time_edge_claims_allowed=False,
            reasons=["FINAL_CHECKS_PENDING"],
            wallet_display_label=wallet_display_label(candidate),
            risk_level=verified.skeptic.overall_risk,
        ),
    )
    result.draft_review = self._review_draft(result)
    self.runtime["executionMode"] = aggregate_execution_mode(self.runtime).value
    result.agent_runtime = dict(self.runtime)
    validation = validate_release(result)
    self.runtime["releaseValidation"] = validation.model_dump(mode="json")
    reasons = self._analysis_review_reasons(
        candidate,
        case.context,
        verified.claims,
        reports.domain_report,
        verified.skeptic,
        gathered.osint,
        verified.verification_report,
        gathered.crypto_packet,
    )
    self.runtime["sharedBudget"] = self.budget.snapshot()
    retrieval = self.evidence_router.source_tool_registry.retrieval
    self.runtime["sharedBudget"]["retrieval"] = {
        "requests": retrieval.requests,
        "documents": sorted(retrieval.documents),
        "sources": sorted(retrieval.sources),
        "candidates": retrieval.candidates,
        "seconds_used": retrieval.seconds_used,
        "attempts": retrieval.attempts,
    }
    if not self.runtime["sharedBudget"]["confirmed"]:
        reasons.append("SHARED_RESEARCH_BUDGET_UNCONFIRMED")
    required = (
        "rules-osint",
        self._domain_runtime_key(case.sector_route),
        "skeptic",
        "evidence-verifier",
        "narrative-editor",
        "draft-verifier",
    )
    incomplete = any(
        (stage := self.runtime["agents"].get(name, {})).get("source") not in {"llm"} or stage.get("status") != "SUCCESS"
        for name in required
    )
    policy = self.policy_engine.decide(
        candidate,
        case.capability,
        gathered.evidence_contract,
        verified.claims,
        verified.verification_report,
        execution_mode=self.runtime["executionMode"],
        services_degraded=bool(self.runtime.get("servicesDegraded")),
        required_agent_semantic_task_incomplete=incomplete,
        analysis_review_reasons=reasons,
        release_validation=validation,
    )
    result.policy = PolicyDecision(**policy.model_dump(), risk_level=verified.skeptic.overall_risk)
    self.runtime["publicationPolicy"] = result.policy.model_dump(mode="json", exclude={"risk_level"})
    result.agent_runtime = dict(self.runtime)
    return result
