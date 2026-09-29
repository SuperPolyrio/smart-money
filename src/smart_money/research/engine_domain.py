"""Domain-specific report and specialist evidence preparation."""

from __future__ import annotations

import re
from typing import Any

from smart_money.research.contracts import (
    CaseClassification,
    EvidenceItem,
)
from smart_money.research.crypto import CryptoEvidencePacket, build_crypto_evidence_packet
from smart_money.research.domain_experts import definition_for, deterministic_report
from smart_money.research.engine_contract import EngineHost
from smart_money.research.engine_support import (
    _quality_packet,
    _select_fields,
    evidence_packet,
)
from smart_money.research.models import (
    DomainExpertReport,
    SignalCandidate,
)
from smart_money.research.sector_router import SectorRoute


class DomainAnalysisMixin(EngineHost):
    def _domain_analysis(
        self,
        candidate: SignalCandidate,
        context: dict[str, Any],
        evidence: list[EvidenceItem],
        sector_route: SectorRoute,
        crypto_packet: CryptoEvidencePacket | None = None,
    ) -> DomainExpertReport:
        osint = evidence_packet(evidence)
        definition = definition_for(sector_route.domain)
        crypto_payload = crypto_packet.model_dump(mode="json") if crypto_packet else None
        blind_politics_analysis = definition.agent == "PoliticsAgent"
        fallback = deterministic_report(definition.domain, osint, crypto_packet=crypto_payload)
        runtime_name = re.sub(r"(?<!^)(?=[A-Z])", "-", definition.agent).lower()
        candidate_payload: dict[str, Any] = {
            "candidate_id": candidate.candidate_id,
            "as_of": candidate.as_of.isoformat(),
            "market_id": candidate.market_id,
            "sector_id": candidate.sector_id,
            "signal_type": candidate.signal_type,
        }
        if not blind_politics_analysis:
            candidate_payload.update(
                {
                    "wallet": candidate.wallet,
                    "side": candidate.side,
                    "outcome": candidate.outcome,
                    "entry_price": candidate.entry_price,
                    "notional": candidate.notional,
                    "trade": _select_fields(
                        candidate.evidence.get("trade") or {},
                        ("action", "position_action", "role"),
                    ),
                }
            )
        quality_payload = _quality_packet(candidate, context, osint)
        if blind_politics_analysis:
            quality_payload = {
                "available_sources": [source for source in quality_payload["available_sources"] if source != "signal"],
                "missing": [
                    field for field in quality_payload["missing"] if field in {"market_metadata", "rule_snapshot"}
                ],
                "wallet_input_blinded": True,
            }
        domain_payload = {
            "candidate": candidate_payload,
            "market": _select_fields(
                context.get("market") or {},
                (
                    "condition_id",
                    "title",
                    "end_at",
                    "outcomes",
                    "outcome_prices",
                    "tags",
                    "official_category",
                    "closed",
                    "resolved",
                    "winning_outcome",
                ),
            ),
            "domain": definition.domain,
            "sector_route": sector_route.model_dump(mode="json"),
            "domain_evidence": osint,
            "rule_questions": self.runtime.get("ruleQuestions", []),
            "evidence_contract": _select_fields(
                self.runtime.get("evidenceContract", {}),
                ("passed", "required_fields", "missing_fields", "pit_missing_fields"),
            ),
            "crypto_evidence_packet": crypto_payload,
            "analysis_mode": "BLIND_DOMAIN_FIRST" if blind_politics_analysis else "TRADE_AWARE",
            "data_quality": quality_payload,
        }
        report = self._call(
            runtime_name,
            definition.prompt + f" You are the dedicated {definition.domain} specialist. Analyze only "
            "supplied canonical domain_evidence, reading original content from source_snapshot and "
            "using only checked contract_fields for calculations. Cross-domain background must be relevant "
            "to a specific rule question. "
            + " Build a domain judgment for this exact market and signal time. market_state must describe the "
            "underlying event or asset, signal_interpretation must explain whether that state supports the "
            "observed "
            "trade direction without assuming it is correct. Return candidate claims, not a final article. "
            "Return at most three distinct findings, including contrary material where available. Do not repeat "
            "wallet/trade facts or re-extract rules as domain discoveries; missing event evidence belongs in unknowns. "
            "Give each claim a stable local claim_id, original references and required_qualifications; "
            "For a directly evidenced required contract field, attach contract_field and field_value to the "
            "FACT claim. These are proposals, never accepted fields. Read original material even when the "
            "evidence contract is incomplete; state the missing fields and do not invent computed metrics. "
            "an inference must list the claim_ids of its factual premises in depends_on. "
            "Use supplied computed "
            "metrics as canonical facts; do not recompute them or introduce unsupported numbers. Mark INSUFFICIENT "
            "when exact entity, date, source, or metric alignment is missing. "
            + "Current or post-signal evidence may assess the event now but must not be described as the wallet's "
            "entry-time information. A SELL does not prove a bearish opening position when prior inventory is "
            "unknown.",
            domain_payload,
            DomainExpertReport,
            fallback,
        )
        if report.agent != definition.agent or report.domain != definition.domain:
            self.runtime["agents"].setdefault(runtime_name, {}).update(
                status="FAILED", verification="rejected-wrong-domain-agent"
            )
            return fallback
        if definition.domain == "CRYPTO" and crypto_packet is not None:
            report = report.model_copy(
                update={
                    "alignment": fallback.alignment,
                    "wallet_trade_interpretation": fallback.wallet_trade_interpretation,
                }
            )
        return report

    def _not_invoked_domain_report(
        self,
        sector_route: SectorRoute,
        osint: list[dict[str, Any]],
        *,
        reason: str,
        unresolved: list[str],
    ) -> DomainExpertReport:
        definition = definition_for(sector_route.domain)
        runtime_name = re.sub(r"(?<!^)(?=[A-Z])", "-", definition.agent).lower()
        self.runtime["agents"][runtime_name] = {
            "source": "not-invoked",
            "status": "NOT_EXECUTED",
            "reason": reason,
            "missingFields": unresolved,
        }
        report = deterministic_report(sector_route.domain, osint)
        if not unresolved:
            return report
        return report.model_copy(
            update={
                "alignment": "INSUFFICIENT",
                "risk_flags": list(dict.fromkeys([*report.risk_flags, "EVIDENCE_CONTRACT_INCOMPLETE"])),
                "unknowns": list(
                    dict.fromkeys(
                        [
                            *report.unknowns,
                            *[f"缺少合同字段或信号时点证据：{field}" for field in unresolved],
                        ]
                    )
                ),
            }
        )

    def _prepare_crypto_packet(
        self,
        candidate: SignalCandidate,
        classification: CaseClassification,
        evidence: list[EvidenceItem],
    ) -> CryptoEvidencePacket | None:
        if classification.primary_domain != "CRYPTO":
            return None
        packet = build_crypto_evidence_packet(candidate, classification, evidence)
        self.runtime["cryptoAnalysis"] = {
            **packet.model_dump(mode="json"),
            "executionMode": "DETERMINISTIC_REQUIRED_TASK",
        }
        return packet
