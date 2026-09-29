"""Host contract shared by the MAS stage components.

The concrete engine supplies input context and model calls; the components
provide domain methods. Keeping the contract below the mixins avoids runtime
imports back into the engine.
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from smart_money.infrastructure.budget import ResearchBudget
from smart_money.infrastructure.llm.gateway import LLMGateway
from smart_money.publication.policy import PublicationPolicyEngine
from smart_money.research.classifier import CapabilityRegistry, CaseClassifier
from smart_money.research.contract_activities import ContractGapResolver
from smart_money.research.domain_evidence import DomainEvidenceRouter
from smart_money.research.evidence import EvidenceSystem
from smart_money.research.structured_output import StructuredOutputRunner

T = TypeVar("T", bound=BaseModel)


class EngineHost:
    budget: ResearchBudget
    context: dict[str, Any]
    supplied_evidence: list[dict[str, Any]]
    gateway: LLMGateway
    runtime: dict[str, Any]
    llm_enabled: bool
    osint_enabled: bool
    evidence_router: DomainEvidenceRouter
    policy_engine: PublicationPolicyEngine
    case_classifier: CaseClassifier
    capability_registry: CapabilityRegistry
    evidence_system: EvidenceSystem
    contract_gap_resolver: ContractGapResolver
    structured_output: StructuredOutputRunner

    def _call(self, name: str, system: str, payload: dict[str, Any], model: type[T], fallback: T) -> T:
        raise NotImplementedError
