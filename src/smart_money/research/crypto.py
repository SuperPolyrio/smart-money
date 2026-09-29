"""Question-specific deterministic contracts and calculations for CryptoAgent.

This module never fetches URLs and never asks an LLM to calculate a fact.  It
uses the unique classification and checked canonical evidence fields.
General market context is kept separate from the market predicate.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from statistics import pstdev
from typing import Any, Literal

from pydantic import Field

from smart_money.contracts import StrictModel
from smart_money.contracts import parse_utc as _datetime
from smart_money.research.contract_activity_recipes import EVIDENCE_CONTRACTS
from smart_money.research.contracts import CaseClassification, EvidenceItem, MarketArchetype
from smart_money.research.evidence import contract_field_values
from smart_money.research.models import SignalCandidate, WalletTradeInterpretation


class CryptoCaseType(str, Enum):  # noqa: UP042 - Python 3.10 compatibility
    SHORT_WINDOW_UP_DOWN = "SHORT_WINDOW_UP_DOWN"
    PRICE_TOUCH_OR_RANGE = "PRICE_TOUCH_OR_RANGE"
    PRICE_ENDPOINT = "PRICE_ENDPOINT"
    TOKEN_LAUNCH = "TOKEN_LAUNCH"
    FDV_AFTER_LAUNCH = "FDV_AFTER_LAUNCH"
    PUBLIC_SALE = "PUBLIC_SALE"
    CORPORATE_BTC_ACTION = "CORPORATE_BTC_ACTION"
    CRYPTO_REGULATION = "CRYPTO_REGULATION"
    EXCHANGE_LISTING = "EXCHANGE_LISTING"
    PROTOCOL_EVENT = "PROTOCOL_EVENT"
    GENERAL_CRYPTO = "GENERAL_CRYPTO"


class CryptoCase(StrictModel):
    case_version: str = "crypto-case-v2"
    case_type: CryptoCaseType
    asset: str | None = None
    project_or_company: str | None = None
    metric: str | None = None
    threshold: float | None = None
    comparison_operator: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    endpoint_time: datetime | None = None
    timezone: str | None = None
    resolution_source: str | None = None
    resolution_pair: str | None = None
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class MarketQuestionAnalysis(StrictModel):
    status: str
    market_predicate: str
    calculation_status: str
    condition_satisfied: bool | None = None
    distance_to_condition: float | None = None
    candidate_outcome_alignment: str = "INSUFFICIENT"
    calculated_fields: dict[str, Any] = Field(default_factory=dict)
    field_evidence_ids: dict[str, list[str]] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)


class GeneralCryptoContext(StrictModel):
    status: str = "OPTIONAL"
    observations: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class CryptoEvidencePacket(StrictModel):
    packet_version: str = "crypto-evidence-v2"
    as_of: datetime
    calculated_at: datetime
    crypto_case: CryptoCase
    market_question_analysis: MarketQuestionAnalysis
    general_market_context: GeneralCryptoContext
    wallet_trade_interpretation: WalletTradeInterpretation
    point_in_time_complete: bool
    source_evidence_ids: list[str] = Field(default_factory=list)


CRYPTO_ARCHETYPES: dict[MarketArchetype, CryptoCaseType] = {
    MarketArchetype.PRICE_DIRECTION_SHORT_WINDOW: CryptoCaseType.SHORT_WINDOW_UP_DOWN,
    MarketArchetype.PRICE_THRESHOLD_TOUCH: CryptoCaseType.PRICE_TOUCH_OR_RANGE,
    MarketArchetype.PRICE_THRESHOLD_ENDPOINT: CryptoCaseType.PRICE_ENDPOINT,
    MarketArchetype.CRYPTO_TOKEN_LAUNCH: CryptoCaseType.TOKEN_LAUNCH,
    MarketArchetype.CRYPTO_FDV_AFTER_LAUNCH: CryptoCaseType.FDV_AFTER_LAUNCH,
    MarketArchetype.CRYPTO_CORPORATE_BTC_ACTION: CryptoCaseType.CORPORATE_BTC_ACTION,
    MarketArchetype.CRYPTO_REGULATION: CryptoCaseType.CRYPTO_REGULATION,
    MarketArchetype.CRYPTO_EXCHANGE_LISTING: CryptoCaseType.EXCHANGE_LISTING,
    MarketArchetype.CRYPTO_PUBLIC_SALE: CryptoCaseType.PUBLIC_SALE,
    MarketArchetype.CRYPTO_PROTOCOL_EVENT: CryptoCaseType.PROTOCOL_EVENT,
    MarketArchetype.GENERAL_CRYPTO: CryptoCaseType.GENERAL_CRYPTO,
}


def crypto_case(classification: CaseClassification, fields: dict[str, Any]) -> CryptoCase:
    """Project validated parameters; never classify or extract from a title."""
    case_type = CRYPTO_ARCHETYPES[classification.market_archetype]
    return CryptoCase(
        case_type=case_type,
        asset=fields.get("asset_identity"),
        project_or_company=fields.get("project_identity") or fields.get("company_identity"),
        threshold=_number(fields.get("threshold", fields.get("fdv_threshold"))),
        comparison_operator=fields.get("comparison_operator"),
        window_start=_datetime(fields.get("exact_window_start") or fields.get("window_start")),
        window_end=_datetime(fields.get("exact_window_end") or fields.get("window_end")),
        endpoint_time=_datetime(fields.get("endpoint_time")),
        timezone=fields.get("window_timezone") or fields.get("endpoint_timezone"),
        resolution_source=fields.get("resolution_price_source") or fields.get("resolution_source"),
        resolution_pair=fields.get("resolution_pair"),
        confidence=classification.confidence,
        reasons=list(classification.reasons),
    )


def compare_values(value: float, threshold: float, operator: str | None) -> bool | None:
    if operator == ">":
        return value > threshold
    if operator == ">=":
        return value >= threshold
    if operator == "<":
        return value < threshold
    if operator == "<=":
        return value <= threshold
    if operator == "==":
        return value == threshold
    return None


def _number(value: Any) -> float | None:
    try:
        number = float(value) if value is not None and not isinstance(value, bool) else None
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _point_prices(path: Any) -> list[tuple[datetime, float]]:
    points: list[tuple[datetime, float]] = []
    if not isinstance(path, list):
        return points
    for point in path:
        if not isinstance(point, dict):
            continue
        at = _datetime(point.get("end_at") or point.get("at") or point.get("timestamp") or point.get("time"))
        price = _number(point.get("price") or point.get("close") or point.get("value"))
        if at is not None and price is not None and price > 0:
            points.append((at, price))
    return sorted(points)


def _returns_volatility(prices: list[float]) -> float | None:
    returns = [math.log(current / previous) for previous, current in zip(prices, prices[1:]) if previous > 0]
    return pstdev(returns) * 100 if len(returns) >= 2 else None


def candle_coverage(path: list[dict[str, Any]], start: datetime, end: datetime) -> float:
    """Coverage of complete OHLC intervals, including leading and internal gaps."""
    if end <= start:
        return 0.0
    cursor, covered = start, 0.0
    for point in sorted(path, key=lambda p: str(p["at"])):
        left, right = _datetime(point.get("at")), _datetime(point.get("end_at"))
        if left is None or right is None or not start <= left < right <= end:
            continue
        covered += max(0.0, (right - max(cursor, left)).total_seconds())
        cursor = max(cursor, right)
    return min(1.0, covered / (end - start).total_seconds())


def _derive_fields(case: CryptoCase, fields: dict[str, Any], *, as_of: datetime) -> dict[str, Any]:
    derived = dict(fields)
    path = _point_prices(fields.get("minute_or_finer_path") or fields.get("price_path"))
    if case.case_type == CryptoCaseType.SHORT_WINDOW_UP_DOWN:
        start = _datetime(fields.get("exact_window_start")) or case.window_start
        end = _datetime(fields.get("exact_window_end")) or case.window_end
        eligible = [
            (at, price)
            for at, price in path
            if start is not None and end is not None and start <= at <= min(end, as_of)
        ]
        prices = [price for _, price in eligible]
        if prices:
            if eligible[0][0] == start:
                derived.setdefault("start_price", prices[0])
            if eligible[-1][0] == end:
                derived.setdefault("end_price", prices[-1])
            derived.setdefault("window_high", max(prices))
            derived.setdefault("window_low", min(prices))
            derived.setdefault("realized_window_volatility", _returns_volatility(prices))
        start_price = _number(derived.get("start_price"))
        end_price = _number(derived.get("end_price"))
        if start is None or start > as_of:
            start_price = None
            derived.pop("start_price", None)
        if end is None or end > as_of:
            end_price = None
            derived.pop("end_price", None)
            derived.pop("realized_window_return", None)
        if start_price and end_price is not None:
            derived["realized_window_return"] = (end_price / start_price - 1) * 100
        if start is not None:
            derived.setdefault("exact_window_start", start.isoformat())
        if end is not None:
            derived.setdefault("exact_window_end", end.isoformat())
        derived.setdefault("window_timezone", case.timezone)
        derived.setdefault("resolution_price_source", case.resolution_source)
        derived.setdefault("resolution_pair", case.resolution_pair)
        derived.setdefault("asset_identity", case.asset)
        derived["calculation_status"] = (
            "FINAL_WINDOW"
            if end is not None and end <= as_of and start_price is not None and end_price is not None
            else "PARTIAL_WINDOW"
            if eligible
            else "UNAVAILABLE"
        )
    elif case.case_type == CryptoCaseType.PRICE_TOUCH_OR_RANGE:
        start = _datetime(fields.get("window_start")) or case.window_start
        end = _datetime(fields.get("window_end")) or case.window_end
        eligible = [
            (at, price)
            for at, price in path
            if start is not None and end is not None and start <= at <= min(end, as_of)
        ]
        prices = [price for _, price in eligible]
        threshold = case.threshold
        operator = case.comparison_operator
        if prices:
            derived.setdefault("window_high", max(prices))
            derived.setdefault("window_low", min(prices))
            derived.setdefault("realized_volatility", _returns_volatility(prices))
        current = _number(fields.get("current_price")) or (prices[-1] if prices else None)
        if current is not None and threshold is not None:
            derived["distance_to_threshold"] = threshold - current
        hits = [
            (at, price)
            for at, price in eligible
            if threshold is not None and compare_values(price, threshold, operator) is True
        ]
        if path:
            # A sampled price can prove a touch, but gaps or close-only samples
            # cannot prove that no touch occurred between observations.
            derived["threshold_hit"] = True if hits else None
            derived["threshold_hit_at"] = hits[0][0].isoformat() if hits else None
        if start and end and end > start and path:
            expected_minutes = max(1, int((end - start).total_seconds() // 60) + 1)
            observed_minutes = len({at.replace(second=0, microsecond=0) for at, _ in eligible})
            derived["minute_path_coverage"] = min(1.0, observed_minutes / expected_minutes)
        if end is None or end > as_of:
            if derived.get("threshold_hit") is False:
                derived["threshold_hit"] = None
                derived["threshold_hit_at"] = None
        if end:
            derived["remaining_time"] = max(0.0, (end - as_of).total_seconds())
        derived.setdefault("asset_identity", case.asset)
        derived.setdefault("threshold", threshold)
        derived.setdefault("window_start", start.isoformat() if start else None)
        derived.setdefault("window_end", end.isoformat() if end else None)
        derived.setdefault("resolution_price_source", case.resolution_source)
    elif case.case_type == CryptoCaseType.PRICE_ENDPOINT:
        endpoint = _datetime(fields.get("endpoint_time")) or case.endpoint_time
        current = _number(fields.get("current_price"))
        endpoint_price = _number(fields.get("endpoint_price"))
        if endpoint is None or endpoint > as_of:
            endpoint_price = None
            derived.pop("endpoint_price", None)
        threshold = case.threshold
        reference = endpoint_price if endpoint_price is not None else current
        if reference is not None and threshold is not None:
            derived["distance_to_threshold"] = threshold - reference
        derived.setdefault("endpoint_time", endpoint.isoformat() if endpoint else None)
        derived.setdefault("endpoint_timezone", case.timezone)
        derived.setdefault("resolution_source", case.resolution_source)
        derived["data_status"] = (
            "FINAL_ENDPOINT" if endpoint and endpoint <= as_of and endpoint_price is not None else "FORECAST"
        )
    elif case.case_type == CryptoCaseType.FDV_AFTER_LAUNCH:
        supply = _number(fields.get("total_supply"))
        spot = _number(fields.get("spot_price_at_measurement"))
        if supply is not None and spot is not None:
            derived["calculated_fdv"] = supply * spot
            derived["calculation_formula"] = "total_supply * spot_price_at_measurement"
            threshold = _number(fields.get("fdv_threshold")) or case.threshold
            if threshold is not None:
                comparison = compare_values(supply * spot, threshold, case.comparison_operator)
                if comparison is not None:
                    derived["threshold_status"] = "MET" if comparison else "NOT_MET"
        elif _number(fields.get("premarket_price")) is not None:
            derived["threshold_status"] = "PREMARKET_ONLY_NOT_MEASURABLE"
    return derived


def _candidate_alignment(candidate: SignalCandidate, satisfied: bool | None, fields: dict[str, Any]) -> str:
    outcome = str(candidate.outcome or "").strip().lower()
    if satisfied is None:
        return "INSUFFICIENT"
    backs_yes = outcome in {"yes", "up", "above", "will happen"}
    backs_no = outcome in {"no", "down", "below", "will not happen"}
    if backs_yes:
        return "SUPPORTS" if satisfied else "CONTRADICTS"
    if backs_no:
        return "CONTRADICTS" if satisfied else "SUPPORTS"
    return str(fields.get("candidate_outcome_alignment") or "INSUFFICIENT")


def _interpret_trade(candidate: SignalCandidate, question: MarketQuestionAnalysis) -> WalletTradeInterpretation:
    side = str(candidate.side or "").upper()
    trade = candidate.evidence.get("trade") or {}
    action = str(trade.get("action") or trade.get("position_action") or "").upper()
    position: Literal["INCREASE", "REDUCE", "CLOSE", "UNKNOWN"] = "UNKNOWN"
    if action in {"OPEN", "ADD", "INCREASE", "OPEN_OR_ADD"}:
        position = "INCREASE"
    elif action in {"REDUCE", "DECREASE"}:
        position = "REDUCE"
    elif action in {"CLOSE", "EXIT"}:
        position = "CLOSE"
    alignment: Literal["ALIGNED", "CONTRADICTED", "MIXED", "UNKNOWN"] = "UNKNOWN"
    if question.status == "COMPLETE" and side in {"BUY", "SELL"}:
        supported = question.candidate_outcome_alignment
        if supported in {"SUPPORTS", "CONTRADICTS"}:
            alignment = "ALIGNED" if (supported == "SUPPORTS") == (side == "BUY") else "CONTRADICTED"
        elif supported == "MIXED":
            alignment = "MIXED"
    direction: Literal["UP", "DOWN", "UNKNOWN"] = "UNKNOWN"
    outcome = str(candidate.outcome or "").lower()
    if side in {"BUY", "SELL"} and outcome in {"up", "higher", "down", "lower"}:
        direction = "UP" if (outcome in {"up", "higher"}) == (side == "BUY") else "DOWN"
    interpretation = f"该成交与规则指定市场问题计算的关系为 {alignment}。"
    if position == "UNKNOWN":
        interpretation += "缺少交易前持仓，不能确认这是主动建仓。"
    return WalletTradeInterpretation(
        observed_trade=f"{side or 'UNKNOWN'} {candidate.outcome or 'UNKNOWN'}",
        position_effect=position,
        market_direction_equivalent=direction,
        crypto_agent_bias=question.candidate_outcome_alignment,
        alignment=alignment,
        entry_probability=candidate.entry_price,
        interpretation=interpretation,
        alternative_explanations=["减持或平仓", "做市库存调整", "其他市场或不可见账户中的对冲"],
    )


def build_crypto_evidence_packet(
    candidate: SignalCandidate,
    classification: CaseClassification,
    evidence: list[EvidenceItem],
    *,
    calculated_at: datetime | None = None,
) -> CryptoEvidencePacket:
    fields, field_ids, pit_fields = contract_field_values(evidence)
    case = crypto_case(classification, fields)
    raw_keys = set(fields)
    fields = _derive_fields(case, fields, as_of=candidate.as_of)
    base_evidence_ids = list(dict.fromkeys(evidence_id for ids in field_ids.values() for evidence_id in ids))
    base_is_pit = bool(raw_keys) and all(pit_fields.get(key, False) for key in raw_keys)
    for key in set(fields) - raw_keys:
        if fields.get(key) is not None and base_evidence_ids:
            field_ids.setdefault(key, list(base_evidence_ids))
            pit_fields.setdefault(key, base_is_pit)
    required = list(EVIDENCE_CONTRACTS[classification.market_archetype])
    missing = [key for key in required if fields.get(key) is None]
    missing_evidence = [key for key in required if fields.get(key) is not None and not field_ids.get(key)]
    missing = list(dict.fromkeys([*missing, *missing_evidence]))
    # Optional state factors must be represented, but an explicit unavailable
    # object is acceptable because they never prove the market predicate.
    if case.case_type == CryptoCaseType.PRICE_TOUCH_OR_RANGE:
        missing = [
            key for key in missing if key not in {"etf_flow_snapshot", "funding_snapshot", "liquidation_snapshot"}
        ]
        for optional in ("etf_flow_snapshot", "funding_snapshot", "liquidation_snapshot"):
            fields.setdefault(optional, {"status": "UNAVAILABLE"})
    source_ids = list(dict.fromkeys(evidence_id for ids in field_ids.values() for evidence_id in ids))
    pit_missing = [key for key in required if key in fields and not pit_fields.get(key, False)]
    point_in_time_complete = not missing and not pit_missing
    status = "COMPLETE" if point_in_time_complete else "CURRENT_ONLY" if not missing and fields else "INCOMPLETE"
    satisfied: bool | None = None
    distance: float | None = None
    if case.case_type == CryptoCaseType.SHORT_WINDOW_UP_DOWN:
        result = _number(fields.get("realized_window_return"))
        satisfied = compare_values(result, 0, case.comparison_operator) if result is not None else None
        distance = result
    elif case.case_type == CryptoCaseType.PRICE_TOUCH_OR_RANGE:
        satisfied = fields.get("threshold_hit") if isinstance(fields.get("threshold_hit"), bool) else None
        distance = _number(fields.get("distance_to_threshold"))
    elif case.case_type == CryptoCaseType.PRICE_ENDPOINT:
        price = _number(fields.get("endpoint_price"))
        satisfied = (
            compare_values(price, case.threshold, case.comparison_operator)
            if price is not None and case.threshold is not None
            else None
        )
        distance = _number(fields.get("distance_to_threshold"))
    elif case.case_type == CryptoCaseType.FDV_AFTER_LAUNCH:
        threshold_status = fields.get("threshold_status")
        satisfied = threshold_status == "MET" if threshold_status in {"MET", "NOT_MET"} else None
        calculated = _number(fields.get("calculated_fdv"))
        distance = calculated - case.threshold if calculated is not None and case.threshold is not None else None
    elif case.case_type == CryptoCaseType.CORPORATE_BTC_ACTION:
        satisfied = fields.get("confirmed_sale") if isinstance(fields.get("confirmed_sale"), bool) else None
    market_question = MarketQuestionAnalysis(
        status=status,
        market_predicate=str(
            fields.get("market_predicate")
            or fields.get("resolution_definition")
            or classification.market_archetype.value
        ),
        calculation_status=str(fields.get("calculation_status") or fields.get("data_status") or status),
        condition_satisfied=satisfied,
        distance_to_condition=distance,
        candidate_outcome_alignment=_candidate_alignment(candidate, satisfied, fields),
        calculated_fields=fields,
        field_evidence_ids=field_ids,
        evidence_ids=source_ids,
        missing_fields=missing,
        invalidation_conditions=[
            "规则指定数据源或结算定义发生变化",
            "关键来源被更正或被证明与目标实体不匹配",
            *(
                ["结算窗口尚未结束，后续路径可能改变结果"]
                if case.window_end and case.window_end > candidate.as_of
                else []
            ),
        ],
    )
    general = GeneralCryptoContext(
        status=(
            "AVAILABLE"
            if any(key in fields for key in ("return_1h_pct", "return_24h_pct", "return_7d_pct"))
            else "OPTIONAL"
        ),
        observations={
            key: fields[key]
            for key in (
                "return_1h_pct",
                "return_24h_pct",
                "return_7d_pct",
                "funding_snapshot",
                "etf_flow_snapshot",
                "liquidation_snapshot",
            )
            if key in fields
        },
        evidence_ids=source_ids,
        limitations=["通用行情只能提供背景，不能替代规则指定的结算计算。"],
    )
    return CryptoEvidencePacket(
        as_of=candidate.as_of,
        calculated_at=calculated_at or datetime.now(timezone.utc),
        crypto_case=case,
        market_question_analysis=market_question,
        general_market_context=general,
        wallet_trade_interpretation=_interpret_trade(candidate, market_question),
        point_in_time_complete=point_in_time_complete,
        source_evidence_ids=source_ids,
    )
