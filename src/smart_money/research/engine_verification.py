"""Skeptic, claim verification, and publication policy stages."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from smart_money.contracts import parse_utc
from smart_money.publication.release_validator import draft_input_hash, draft_review_payload
from smart_money.research.contract_field_extractors import _validation_error, numeric_value_is_quoted
from smart_money.research.contracts import EvidenceItem
from smart_money.research.crypto import CryptoEvidencePacket
from smart_money.research.engine_contract import EngineHost
from smart_money.research.engine_support import evidence_packet
from smart_money.research.evidence_verifier import (
    apply_report,
    deterministic_claims,
    fallback_report,
    reference_is_located,
)
from smart_money.research.models import (
    ClaimDraft,
    DomainExpertReport,
    DraftSemanticReview,
    EvidenceClaimReview,
    EvidenceVerificationReport,
    MasResult,
    PublicationDraft,
    SignalCandidate,
    SkepticReport,
    SpecialistReport,
    VerifiedClaim,
)


class VerificationMixin(EngineHost):
    def _skeptic(
        self,
        candidate: SignalCandidate,
        wallet: SpecialistReport,
        rules: SpecialistReport,
        domain: DomainExpertReport,
        evidence: list[EvidenceItem],
    ) -> SkepticReport:
        fallback = SkepticReport(overall_risk="high")
        report = self._call(
            "skeptic",
            "You are SkepticAgent. Stress-test the proposed signal without deleting supported observations. Consider "
            "external hedges, partial position visibility, follower behavior, exit-before-resolution behavior, sector "
            "mismatch, sample size, liquidity, stale prices, and settlement ambiguity only when supported or missing. "
            "Rank counter-hypotheses by severity and state what evidence would resolve each uncertainty. "
            "Every counter_hypothesis must identify existing claim_ids, its evidence and required qualification. "
            "Challenge what a claim actually asserts: a confirmed trade does not assert expertise or a profitable "
            "strategy. Return no objection when you have none tied to an asserted claim; do not invent a generic "
            "risk checklist. Missing event evidence alone does not contradict a factual trade or rule. "
            "Do not invent contrary stories. requested_fields may contain only unresolved contract field names "
            "whose answer could change a specific claim; no free search or recursive investigation.",
            {
                "candidate_id": candidate.candidate_id,
                "wallet_report": wallet.model_dump(mode="json"),
                "rules_report": rules.model_dump(mode="json"),
                "domain_report": domain.model_dump(mode="json"),
                "evidence": evidence_packet(evidence),
                "missing_fields": self.runtime.get("evidenceContract", {}).get("missing_fields", []),
            },
            SkepticReport,
            fallback,
        )
        known = {c.claim_id for r in (wallet, rules, domain) for c in r.claims}
        items = {item.evidence_id: item for item in evidence}
        if any(
            not h.claim_ids
            or not set(h.claim_ids) <= known
            or any(
                ref.evidence_id not in items or not reference_is_located(ref, items[ref.evidence_id])
                for ref in h.references
            )
            for h in report.counter_hypotheses
        ):
            self.runtime["agents"]["skeptic"].update(status="FAILED", reason="UNLOCATED_OBJECTION")
            return fallback
        return report

    def _evidence_verifier(
        self,
        run_id: str,
        evidence: list[EvidenceItem],
        claim_drafts: list[ClaimDraft],
        skeptic: SkepticReport,
    ) -> tuple[list[VerifiedClaim], EvidenceVerificationReport]:
        baseline = deterministic_claims(
            run_id, evidence, claim_drafts, research_cutoff=parse_utc(self.context.get("research_cutoff"))
        )
        for claim in baseline:
            claim.required_qualifications = list(
                dict.fromkeys(
                    [
                        *claim.required_qualifications,
                        *(
                            h.required_qualification
                            for h in skeptic.counter_hypotheses
                            if claim.claim_id in h.claim_ids and h.required_qualification
                        ),
                    ]
                )
            )
        fallback = fallback_report(baseline)
        report = self._verify_batches(
            "evidence-verifier",
            "You are an independent EvidenceVerifierAgent. You did not participate in the investigation or writing. "
            "Review each proposed claim against the raw evidence items, not against another agent's summary. Return "
            "exactly one review for every claim_index. Mark SUPPORTED only when the cited evidence directly entails "
            "the statement at the claimed precision and time_scope. TRADE_TIME requires pre-trade evidence; "
            "RESEARCH_UPDATE must be explicitly later context, never proof of prior knowledge. Mark UNSUPPORTED for "
            "missing support, CONTRADICTED for conflicting evidence, and AMBIGUOUS when the statement is stronger than "
            "the evidence. Detect future leakage. For every supporting evidence_id supply references with a JSON "
            "pointer locator into source_snapshot and an exact quote (preserve its original language). An ID, "
            "summary, title, or search snippet alone is not the original factual support. Check negations, "
            "conditions, attribution, objects, units, causal inference and the skeptic's objections. "
            "Set entity_matches_market true only when the located original passage binds the material to this "
            "market's exact subject. If contract_field is proposed, independently check that field_value itself "
            "is entailed, including its type, stage, units and scope; prose support alone is insufficient. "
            "List in preserved_qualifications only the required_qualifications whose meaning the claim actually "
            "preserves; missing restrictions make the claim AMBIGUOUS. "
            "Do not add claims, repair prose, or promote publication confidence.",
            {
                "claims": [
                    {
                        "claim_index": index,
                        **claim.model_dump(mode="json"),
                        "deterministic_evidence_ids": baseline[index - 1].supporting_evidence_ids,
                        "required_qualifications": baseline[index - 1].required_qualifications,
                    }
                    for index, claim in enumerate(claim_drafts, start=1)
                ],
                "evidence": evidence_packet(evidence),
                "skeptic": skeptic.model_dump(mode="json"),
            },
            fallback,
        )
        for review in report.reviews:
            if 1 <= review.claim_index <= len(claim_drafts):
                proposal = claim_drafts[review.claim_index - 1]
                if proposal.contract_field and (
                    _validation_error(proposal.contract_field, proposal.field_value)
                    or not numeric_value_is_quoted(proposal.field_value, "\n".join(r.quote for r in review.references))
                ):
                    review.verdict = "AMBIGUOUS"
                    review.rationale = "Field value failed type or original numeric quote checks."
        claims, report = apply_report(baseline, report, evidence)
        counts: dict[str, int] = {}
        for review in report.reviews:
            counts[review.verdict] = counts.get(review.verdict, 0) + 1
        self.runtime["agents"].setdefault("evidence-verifier", {})["verdictCounts"] = counts
        return claims, report

    def _verify_batches(
        self, name: str, prompt: str, payload: dict[str, Any], fallback: EvidenceVerificationReport
    ) -> EvidenceVerificationReport:
        # Bound output per request without dropping claims or renewing the shared research budget.
        key = "claims" if name == "evidence-verifier" else "statements"
        rows = payload[key]
        combined = EvidenceVerificationReport(summary="逐条原文核验。")
        executions = []
        for start in range(0, len(rows), 3):
            batch = rows[start : start + 3]
            report = self._call(name, prompt, {**payload, key: batch}, EvidenceVerificationReport, fallback)
            execution = deepcopy(self.runtime["agents"].get(name, {}))
            indexes = {row["claim_index"] for row in batch}
            if len(report.reviews) != len(batch) or {r.claim_index for r in report.reviews} != indexes:
                execution.update(status="FAILED", reason="INCOMPLETE_REVIEW_COVERAGE")
                report = EvidenceVerificationReport(
                    summary=fallback.summary,
                    conflicts=report.conflicts,
                    future_leakage_detected=report.future_leakage_detected,
                    reviews=[
                        EvidenceClaimReview(claim_index=i, verdict="AMBIGUOUS", rationale=fallback.summary)
                        for i in sorted(indexes)
                    ],
                )
            executions.append(execution)
            combined.reviews.extend(report.reviews)
            combined.conflicts.extend(report.conflicts)
            combined.future_leakage_detected |= report.future_leakage_detected
        if executions:
            self.runtime["agents"][name] = {
                **executions[-1],
                "status": "SUCCESS" if all(e.get("status") == "SUCCESS" for e in executions) else "FAILED",
                "batches": executions,
            }
        else:
            self.runtime["agents"][name] = {"source": "not-invoked", "status": "NOT_EXECUTED", "reason": "NO_CLAIMS"}
        return combined

    def _review_draft(self, result: MasResult) -> DraftSemanticReview:
        binding = draft_input_hash(result)
        fallback = EvidenceVerificationReport(summary="实际成稿的原文语义复核未完成。")
        if self.runtime["agents"].get("narrative-editor", {}).get("status") != "SUCCESS":
            self.runtime["agents"]["draft-verifier"] = {
                "source": "not-invoked",
                "status": "NOT_EXECUTED",
                "reason": "NO_RESEARCH_DRAFT",
            }
            return DraftSemanticReview(input_hash=binding, report=fallback)
        payload = draft_review_payload(result)
        # The binding covers the complete report; inference needs original sources and the actual draft,
        # not another copy of every upstream report, parser projection and model audit.
        payload = {key: payload[key] for key in ("statements", "publication", "claims", "skeptic")}
        used = {ref for refs in result.publication.claim_refs.values() for ref in refs}
        payload["claims"] = [c for c in payload["claims"] if c["claim_id"] in used]
        ids = {ref for c in payload["claims"] for ref in c["supporting_evidence_ids"]}
        payload["evidence"] = evidence_packet([item for item in result.evidence if item.evidence_id in ids])
        report = self._verify_batches(
            "draft-verifier",
            "You are ClaimVerifier, rechecking the actual final draft against the SAME original evidence. "
            "Review exactly every numbered statement, including title, summaries and all output variants. "
            "Return one review per claim_index. SUPPORTED requires the actual wording to be fully entailed by "
            "its cited verified claims AND their raw source_snapshot, including all negations, time boundaries, "
            "qualifications, attribution, objects, units and necessary counter-evidence. Reject new motives, "
            "causation, stronger identity, dropped limitations, or treating an announcement as completion. "
            "A hypothetical limit must remain hypothetical. Do not inherit a prior pass merely because IDs or "
            "numbers are unchanged. Cite references with evidence_id, JSON pointer locators into "
            "source_snapshot and exact original-language quotes. Missing raw material is AMBIGUOUS. "
            "List in preserved_qualifications the cited claims required_qualifications that this wording actually "
            "preserves. Missing restrictions fail the review. Do not search, rewrite or approve publication.",
            payload,
            fallback,
        )
        return DraftSemanticReview(input_hash=binding, report=report)

    def _analysis_review_reasons(
        self,
        candidate: SignalCandidate,
        context: dict[str, Any],
        claims: list[VerifiedClaim],
        domain: DomainExpertReport,
        skeptic: SkepticReport,
        osint: list[dict[str, Any]],
        verification: EvidenceVerificationReport,
        crypto_packet: CryptoEvidencePacket | None = None,
    ) -> list[str]:
        wallet = candidate.evidence.get("wallet") or {}
        structured_portfolio = wallet.get("profile_tier") == "STRUCTURED_PORTFOLIO_EXPERT"
        reasons = []
        if not any(claim.status == "VERIFIED" for claim in claims):
            reasons.append("NO_VERIFIED_CLAIMS")
        market = context.get("market") or {}
        rules_missing = not context.get("rules") and not market.get("rules_current")
        if rules_missing:
            reasons.append("RULE_SNAPSHOT_MISSING")
        if not osint and candidate.sector_id.split(".", 1)[0] in {
            "CRYPTO",
            "ECONOMICS",
            "FINANCE",
            "POLITICS",
            "SPORTS",
            "TECH",
            "WEATHER",
            "MACRO",
            "ECONOMICS",
            "CULTURE",
            "ENTERTAINMENT",
            "MENTIONS",
            "SOCIAL",
            "SCIENCE",
            "CLIMATE",
            "ESPORTS",
        }:
            reasons.append("DOMAIN_EVIDENCE_MISSING")
        elif domain.alignment == "INSUFFICIENT":
            reasons.append("DOMAIN_ANALYSIS_INSUFFICIENT")
        if domain.domain == "CRYPTO":
            question = crypto_packet.market_question_analysis if crypto_packet is not None else None
            if crypto_packet is None or question is None or question.status != "COMPLETE":
                reasons.append("CRYPTO_DOMAIN_ANALYSIS_INCOMPLETE")
            elif crypto_packet.point_in_time_complete is not True:
                reasons.append("CRYPTO_POINT_IN_TIME_EVIDENCE_MISSING")
        if skeptic.overall_risk == "high":
            reasons.append("HIGH_COUNTER_HYPOTHESIS_RISK")
        if verification.future_leakage_detected:
            reasons.append("EVIDENCE_FUTURE_LEAKAGE")
        if any(review.verdict in {"CONTRADICTED", "AMBIGUOUS"} for review in verification.reviews):
            reasons.append("EVIDENCE_CONFLICT_OR_AMBIGUITY")
        if structured_portfolio:
            reasons.append("PORTFOLIO_CONTEXT_REQUIRED")
        if any(
            profile.admission_status in {"SUSPENDED", "REJECTED", "DISABLED"} for profile in candidate.wallet_profiles
        ):
            reasons.append("SPECIALIST_ADMISSION_NOT_ACTIVE")
        return list(dict.fromkeys(reasons))

    def _editor(
        self,
        claims: list[VerifiedClaim],
        wallet_display_label: str,
        skeptic: SkepticReport,
    ) -> PublicationDraft:
        allowed = [claim for claim in claims if claim.status == "VERIFIED"]
        fallback = PublicationDraft(
            title="交易研究记录",
            brief="文案尚未完成核验。",
            market_read="",
            wallet_read="",
            why_it_matters="",
            risk="证据、领域判断与成稿需要完成核验。",
            confidence="low",
        )
        if not allowed:
            self.runtime["agents"]["narrative-editor"] = {"source": "not-invoked", "reason": "no-verified-claims"}
            return fallback
        return self._call(
            "narrative-editor",
            "You are the restricted editor. Use only the supplied verified claims. Preserve the wallet label, "
            "uncertainty, time limits and counter-evidence. Do not add facts, numbers, motives or causal claims. "
            "Write concise Chinese without internal report names or a required article length. "
            "Write one coherent article in content_text: introduce the account with the available sourced "
            "profile, describe the trade and position context, explain settlement conditions, relevant real-world "
            "developments and limited interpretations, then contrary evidence and unknowns. Use only sections "
            "supported by the supplied claims; never invent missing performance or trader intent. Keep title, "
            "brief and risk short. Leave body_text, short_summary, risk_note and optional read fields empty "
            "instead of copying the article into several fields. "
            "A draft is not published. For EVERY nonempty prose field, split paragraphs only on blank lines "
            "and supply claim_refs keyed field:1, field:2, etc., containing the allowed claim_ids supporting "
            "that exact paragraph. Title and summaries require citations too. Empty optional fields need no "
            "entry. URLs are not authored by you. Preserve TRADE_TIME versus RESEARCH_UPDATE explicitly.",
            {
                "claims": [claim.model_dump(mode="json") for claim in allowed],
                "wallet_display_label": wallet_display_label,
                "skeptic": skeptic.model_dump(mode="json"),
            },
            PublicationDraft,
            fallback,
        )
