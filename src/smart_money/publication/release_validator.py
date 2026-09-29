"""Final deterministic gate applied after narrative editing."""

from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Any

from pydantic import Field

from smart_money.contracts import StrictModel
from smart_money.research.engine_support import evidence_packet
from smart_money.research.evidence_verifier import complete_reviews, supported_review_ids
from smart_money.research.models import MasResult
from smart_money.research.wallet_profile import public_smart_money_wallets

__all__ = ["ReleaseValidation", "validate_release"]

_FORBIDDEN_UNCONFIRMED = ("坐实内幕", "内幕实锤", "无风险", "必然获利", "确定性收益")
_INTERNAL_TERMS = (
    "CrossMarketAgent",
    "ResolutionRulesAgent",
    "EvidenceVerifierAgent",
    "NarrativeEditorAgent",
    "PlannerAgent",
    "CryptoAgent",
    "WeatherClimateAgent",
    "PoliticsElectionsAgent",
    "PoliticsAgent",
    "GeopoliticsAgent",
    "SportsAgent",
    "EsportsAgent",
    "FinanceAgent",
    "MacroEconomyAgent",
    "TechScienceAgent",
    "CultureEntertainmentAgent",
    "MentionsSocialAgent",
    "Policy Engine",
    "deterministic-fallback",
    "deterministic fallback",
    "EVIDENCE_CONTRACT_INCOMPLETE",
    "BOOTSTRAP_CANDIDATE",
    "missing_fields",
    "WALLET_PROFILE_INCOMPLETE",
    "CRYPTO_DOMAIN_ANALYSIS_INCOMPLETE",
    "READY_TO_PUBLISH",
    "REVIEW_REQUIRED",
    "VALIDATION_FAILED",
    "SUPPRESSED",
    "INSUFFICIENT",
    "SUPPORTS_OUTCOME",
    "OPPOSES_OUTCOME",
    "CAPTURED_BEFORE_SIGNAL",
    "DISCOVERED_AFTER_SIGNAL",
    "证据契约",
    "T1",
    "T2",
    "T3",
)
_NUMBER_RE = re.compile(
    r"(?<![\w])(?P<currency>[$¥￥])?(?P<number>-?\d[\d,]*(?:\.\d+)?)"
    r"(?P<suffix>%|％|[kKmM]|倍|枚|¢)?"
)
_URL_RE = re.compile(r"https?://\S+")
_ADDRESS_RE = re.compile(r"0x[a-fA-F0-9.]{6,}")
_LONG_ASCII_RE = re.compile(r"[A-Za-z0-9_./:=?&%-]{181,}")


class ReleaseValidation(StrictModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def draft_statements(result: MasResult) -> dict[str, str]:
    """Enumerate actual text, including title and every retained output form."""
    return {
        f"{field}:{index}": paragraph.strip()
        for field, value in result.publication.model_dump().items()
        if field not in {"confidence", "claim_refs"} and isinstance(value, str)
        for index, paragraph in enumerate(re.split(r"\n\s*\n", value), start=1)
        if paragraph.strip()
    }


def draft_review_payload(result: MasResult) -> dict[str, Any]:
    return {
        "candidate": result.candidate.model_dump(mode="json"),
        "classification": result.classification.model_dump(mode="json"),
        "evidence_contract": result.evidence_contract.model_dump(mode="json"),
        "evidence": evidence_packet(result.evidence),
        "claims": [claim.model_dump(mode="json") for claim in result.claims],
        "verification": result.verification_report.model_dump(mode="json"),
        "skeptic": result.skeptic_report.model_dump(mode="json"),
        "wallet": result.wallet_report.model_dump(mode="json"),
        "rules": result.rules_report.model_dump(mode="json"),
        "domain": result.domain_report.model_dump(mode="json") if result.domain_report else None,
        "publication": result.publication.model_dump(mode="json"),
        "input_identity": result.agent_runtime.get("inputIdentity"),
        "stages": {
            name: value for name, value in result.agent_runtime.get("agents", {}).items() if name != "draft-verifier"
        },
        "statements": [
            {"claim_index": index, "key": key, "text": text, "claim_ids": result.publication.claim_refs.get(key, [])}
            for index, (key, text) in enumerate(draft_statements(result).items(), start=1)
        ],
    }


def draft_input_hash(result: MasResult) -> str:
    payload = json.dumps(draft_review_payload(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def draft_review_errors(result: MasResult) -> list[str]:
    review = result.draft_review
    if review is None:
        return ["DRAFT_SEMANTIC_REVIEW_MISSING"]
    errors = []
    if review.input_hash != draft_input_hash(result):
        errors.append("DRAFT_SEMANTIC_REVIEW_STALE")
    runtime = result.agent_runtime.get("agents", {}).get("draft-verifier", {})
    if runtime.get("source") not in {"llm"} or runtime.get("status") != "SUCCESS":
        errors.append("DRAFT_SEMANTIC_REVIEW_INCOMPLETE")
    statements = draft_statements(result)
    if not complete_reviews(review.report, len(statements)):
        return [*errors, "DRAFT_SEMANTIC_REVIEW_COVERAGE"]
    if review.report.future_leakage_detected or review.report.conflicts:
        errors.append("DRAFT_SEMANTIC_CONFLICT")
    allowed = {claim.claim_id: claim for claim in result.claims if claim.status == "VERIFIED"}
    if set(result.publication.claim_refs) != set(statements):
        errors.append("DRAFT_CITATION_COVERAGE")
    by_index = {item.claim_index: item for item in review.report.reviews}
    for index, key in enumerate(statements, start=1):
        refs = result.publication.claim_refs.get(key, [])
        ids = list(dict.fromkeys(e for ref in refs if ref in allowed for e in allowed[ref].supporting_evidence_ids))
        item = by_index[index]
        if not refs or any(ref not in allowed for ref in refs):
            errors.append(f"DRAFT_CITATION_INVALID:{key}")
        if item.verdict != "SUPPORTED" or not supported_review_ids(item, ids, result.evidence):
            errors.append(f"DRAFT_STATEMENT_UNSUPPORTED:{key}")
        qualifications = {q for ref in refs if ref in allowed for q in allowed[ref].required_qualifications}
        if not qualifications <= set(item.preserved_qualifications):
            errors.append(f"DRAFT_QUALIFICATION_MISSING:{key}")
    return errors


def _numeric_value(number: str, suffix: str | None) -> float:
    value = float(number.replace(",", ""))
    if suffix in {"%", "％"}:
        return value / 100
    if suffix in {"k", "K"}:
        return value * 1_000
    if suffix in {"m", "M"}:
        return value * 1_000_000
    if suffix == "¢":
        return value / 100
    return value


def _numbers(value: Any) -> list[tuple[str, float]]:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    scrubbed = _ADDRESS_RE.sub(" ", _URL_RE.sub(" ", value))
    return [
        (match.group(0), _numeric_value(match.group("number"), match.group("suffix")))
        for match in _NUMBER_RE.finditer(scrubbed)
    ]


def _supported_numbers(result: MasResult) -> list[float]:
    source_payload = {
        "candidate": result.candidate.model_dump(mode="json"),
        "evidence": [
            {
                "source_type": item.source_type,
                "structured_payload": item.structured_payload,
            }
            for item in result.evidence
        ],
        "verified_claims": [claim.statement for claim in result.claims if claim.status == "VERIFIED"],
    }
    return [number for _, number in _numbers(source_payload)]


def _number_supported(number: float, supported: list[float]) -> bool:
    return any(abs(number - candidate) <= max(1e-9, abs(candidate) * 1e-12) for candidate in supported)


def _normalized_paragraph(value: str) -> str:
    return re.sub(r"[\W_]+", "", value).lower()


def _external_evidence_relevant(result: MasResult) -> bool:
    for item in result.evidence:
        if item.source_type != "DOMAIN_EXTERNAL_EVIDENCE":
            continue
        payload = item.structured_payload
        relevance = payload.get("relevance")
        if relevance is None:
            relevance = payload.get("relevance_score")
        if relevance is not None:
            try:
                if float(relevance) >= 0.45:
                    return True
            except (TypeError, ValueError):
                pass
        relation = str(payload.get("temporal_relation") or "")
        if relation in {"BEFORE_SIGNAL", "AT_SIGNAL"} and (payload.get("url") or payload.get("source_name")):
            return True
    return False


def _direction_errors(result: MasResult, title: str) -> list[str]:
    errors: list[str] = []
    side = str(result.candidate.side or "").upper()
    if side == "BUY" and re.search(r"(卖出|做空|减仓|退出)", title):
        errors.append("TITLE_DIRECTION_CONTRADICTS_BUY")
    if side == "SELL" and re.search(r"(买入|做多|加仓)", title):
        errors.append("TITLE_DIRECTION_CONTRADICTS_SELL")
    outcome = str(result.candidate.outcome or "").strip().lower()
    if outcome in {"yes", "是"} and re.search(r"(买入|押注).{0,12}(\bno\b|否)", title, re.I):
        errors.append("TITLE_OUTCOME_CONTRADICTS_YES")
    if outcome in {"no", "否"} and re.search(r"(买入|押注).{0,12}(\byes\b|是)", title, re.I):
        errors.append("TITLE_OUTCOME_CONTRADICTS_NO")
    return errors


def validate_release(result: MasResult) -> ReleaseValidation:
    publication = result.publication
    errors: list[str] = draft_review_errors(result)
    warnings: list[str] = []
    required = {
        "title": publication.title,
        "risk": publication.risk,
    }
    for name, value in required.items():
        if not value.strip():
            errors.append(f"MISSING_{name.upper()}")
    if not any((publication.brief.strip(), publication.body_text.strip(), publication.content_text.strip())):
        errors.append("MISSING_BODY")
    if not any(claim.status == "VERIFIED" for claim in result.claims):
        errors.append("NO_VERIFIED_CLAIMS")
    if result.policy.publication_type == "SMART_MONEY_SIGNAL":
        if not public_smart_money_wallets(result.candidate):
            errors.append("SMART_MONEY_WALLET_PROFILE_INCOMPLETE")
    domain_name = str(result.domain_report.domain if result.domain_report else "").upper()
    crypto_runtime = result.agent_runtime.get("cryptoAnalysis") if isinstance(result.agent_runtime, dict) else None
    if domain_name == "CRYPTO" and isinstance(crypto_runtime, dict):
        question = crypto_runtime.get("market_question_analysis") or {}
        if not isinstance(question, dict) or question.get("status") != "COMPLETE":
            errors.append("CRYPTO_MARKET_QUESTION_INCOMPLETE")
    public_parts = (
        publication.title,
        publication.brief,
        publication.body_text,
        publication.content_text,
        publication.short_summary,
        publication.verification_summary,
        publication.risk_note,
        publication.market_read,
        publication.wallet_read,
        publication.why_it_matters,
        publication.rules_context,
        publication.domain_analysis,
        publication.external_context,
        publication.risk,
    )
    prose = "\n".join(part.strip() for part in public_parts if part.strip())
    if not public_smart_money_wallets(result.candidate) and re.search(r"已验证.{0,8}(专家|聪明钱)", prose):
        errors.append("WALLET_IDENTITY_NOT_SUPPORTED")
    if not bool(result.candidate.evidence.get("confirmed")):
        for phrase in _FORBIDDEN_UNCONFIRMED:
            if phrase in prose:
                errors.append(f"UNCONFIRMED_OVERCLAIM:{phrase}")
    for term in _INTERNAL_TERMS:
        present = bool(re.search(rf"\b{re.escape(term)}\b", prose)) if term in {"T1", "T2", "T3"} else term in prose
        if present:
            errors.append(f"INTERNAL_TERM:{term}")
    if re.search(r"\b[A-Za-z][A-Za-z0-9]*(?:Agent|Verifier)\b", prose):
        errors.append("INTERNAL_AGENT_NAME")
    if re.search(r"\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]{2,})+\b", prose):
        errors.append("INTERNAL_POLICY_REASON_CODE")
    if re.search(r"\b\d+\s*/\s*\d+\b", prose) and re.search(r"(验证|审核|claim|evidence)", prose, re.I):
        errors.append("INTERNAL_VERIFICATION_SCORE")
    if len(prose) > 12_000:
        errors.append("PUBLICATION_TOO_LONG")
    paragraphs = list(dict.fromkeys(part.strip() for part in public_parts if part.strip()))
    normalized = [_normalized_paragraph(part) for part in paragraphs]
    for index, left in enumerate(normalized):
        for right in normalized[index + 1 :]:
            if min(len(left), len(right)) < 36:
                continue
            similarity = SequenceMatcher(None, left, right).ratio()
            if similarity >= 0.94:
                errors.append("SEMANTIC_DUPLICATE_PARAGRAPH")
            elif similarity >= 0.86:
                warnings.append("POSSIBLE_DUPLICATE_PARAGRAPH")
    supported_numbers = _supported_numbers(result)
    for token, number in _numbers(prose):
        if not _number_supported(number, supported_numbers):
            errors.append(f"UNSUPPORTED_NUMBER:{token}")
    body = "\n".join(paragraphs[1:])
    if len(body) >= 80 and len(re.findall(r"[\u4e00-\u9fff]", body)) < 20:
        errors.append("CHINESE_READABILITY_TOO_LOW")
    if _LONG_ASCII_RE.search(body):
        errors.append("UNBROKEN_MACHINE_TEXT")
    external_context = publication.external_context.strip()
    external_context_claims_evidence = bool(
        external_context and not re.search(r"(未获得|没有获得|证据不足|无法判断|没有满足)", external_context)
    )
    if external_context_claims_evidence and not _external_evidence_relevant(result):
        errors.append("EXTERNAL_CONTEXT_NOT_RELEVANT")
    errors.extend(_direction_errors(result, publication.title))
    rules = publication.rules_context.strip()
    if rules:
        ascii_letters = len(re.findall(r"[A-Za-z]", rules))
        chinese = len(re.findall(r"[\u4e00-\u9fff]", rules))
        if ascii_letters > max(120, chinese * 3):
            errors.append("RULES_SUMMARY_MOSTLY_ENGLISH")
    return ReleaseValidation(
        valid=not errors,
        errors=list(dict.fromkeys(errors)),
        warnings=list(dict.fromkeys(warnings)),
    )
