"""Domain-specialist prompts and deterministic evidence summaries for the MAS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from smart_money.research.models import (
    ClaimDraft,
    DomainAgentName,
    DomainExpertReport,
    DomainName,
    WalletTradeInterpretation,
)

Alignment = Literal["SUPPORTS", "CONTRADICTS", "MIXED", "INSUFFICIENT"]
TModel = TypeVar("TModel", bound=BaseModel)


# The local mypy executable runs on Python 3.10 while checking the 3.12 package.
def _optional_model(model: type[TModel], value: Any) -> TModel | None:  # noqa: UP047
    return model.model_validate(value) if value else None


def _alignment(value: Any) -> Alignment:
    if value == "SUPPORTS":
        return "SUPPORTS"
    if value == "CONTRADICTS":
        return "CONTRADICTS"
    if value == "MIXED":
        return "MIXED"
    return "INSUFFICIENT"


@dataclass(frozen=True)
class DomainExpertDefinition:
    domain: DomainName
    agent: DomainAgentName
    prompt: str


EXPERTS = {
    "CRYPTO": DomainExpertDefinition(
        domain="CRYPTO",
        agent="CryptoAgent",
        prompt=(
            "You are CryptoAgent. Use crypto_evidence_packet.market_question_analysis as the canonical "
            "deterministic calculation for the exact market predicate. Preserve crypto_case, the rule-named "
            "resolution source and window, calculated_fields, field_evidence_ids, missing fields, point-in-time "
            "status, and invalidation conditions. General market context is background only. Explain the "
            "wallet_trade_interpretation, distinguishing BUY/SELL and observed position changes from intent. "
            "Do not infer a calibrated probability, fair price, or trading edge from a realized condition or "
            "generic price momentum. Never claim a SELL proves intent when the prior position is unknown."
        ),
    ),
    "WEATHER_CLIMATE": DomainExpertDefinition(
        domain="WEATHER_CLIMATE",
        agent="WeatherClimateAgent",
        prompt=(
            "You are WeatherClimateAgent. Analyze station observations, forecasts, local climatology, "
            "threshold distance, "
            "measurement window, timezone, station identity, and forecast uncertainty. Distinguish an observation "
            "from a forecast and use only the station and metric named by the market rules."
        ),
    ),
    "POLITICS_ELECTIONS": DomainExpertDefinition(
        domain="POLITICS_ELECTIONS",
        agent="PoliticsAgent",
        prompt=(
            "You are PoliticsAgent, the single jurisdiction-neutral specialist for political markets. Treat "
            "politics_case as the canonical case context and handle elections, appointments or removals, legislation, "
            "executive action, courts, party control, polling, and institutional votes in countries, subnational "
            "governments, supranational bodies, and international organizations. Resolve the exact people, parties, "
            "office, institution, jurisdiction, decision stage, deadline, and resolution source before reasoning. "
            "An institutional vote is not proof that a downstream military, diplomatic, or economic action occurred. "
            "Use structured official process, actor status, timeline, polling, campaign-finance, court, and statement "
            "evidence only when supplied. Rank official records above media interpretation, keep polling separate from "
            "market probability, preserve point-in-time order, and fail closed rather than substituting US procedures "
            "for another jurisdiction. Form the political assessment before seeing or interpreting the wallet trade."
        ),
    ),
    "GEOPOLITICS": DomainExpertDefinition(
        domain="GEOPOLITICS",
        agent="GeopoliticsAgent",
        prompt=(
            "You are GeopoliticsAgent. Analyze the exact actors, geography, diplomatic or military event, official "
            "statements, sanctions, conflict timeline and plausible escalation or de-escalation paths. Separate "
            "confirmed events from claims by participants and match every conclusion to the resolution wording."
        ),
    ),
    "SPORTS": DomainExpertDefinition(
        domain="SPORTS",
        agent="SportsAgent",
        prompt=(
            "You are SportsAgent. Analyze the exact fixture and event date using score, match state, injuries, lineup, "
            "schedule, form, and odds only when supplied. Do not mix another game involving the same team. Distinguish "
            "pre-game information from live or final results and explain how it bears on the selected outcome. "
            "Check sport, sex, age group, opponent, competition, date/timezone and regulation/handicap scope. "
            "An announced squad is not a predicted lineup or an official starting lineup. Preserve missing identity, "
            "contrary evidence, original-language quotations and attribution; do not infer a wallet's private motive."
        ),
    ),
    "ESPORTS": DomainExpertDefinition(
        domain="ESPORTS",
        agent="EsportsAgent",
        prompt=(
            "You are EsportsAgent. Analyze the exact game title, tournament, series format, map score, roster, "
            "schedule and match state. Do not mix teams with similar names or conventional sports fixtures, and "
            "distinguish scheduled, live, postponed and final series according to the market rules."
        ),
    ),
    "FINANCE": DomainExpertDefinition(
        domain="FINANCE",
        agent="FinanceAgent",
        prompt=(
            "You are FinanceAgent. Analyze the named security, issuer, venue, filing, price benchmark, corporate "
            "calendar and relevant market structure. Separate official filings and exchange data from estimates, "
            "and never substitute a related asset for the exact resolution instrument."
        ),
    ),
    "MACRO_ECONOMY": DomainExpertDefinition(
        domain="MACRO_ECONOMY",
        agent="MacroEconomyAgent",
        prompt=(
            "You are MacroEconomyAgent. Analyze the named official release, consensus benchmark, prior value and "
            "revisions, policy calendar, rates or market pricing, threshold distance, and release-time uncertainty. "
            "Never substitute a media estimate for the official resolution series."
        ),
    ),
    "TECH_SCIENCE": DomainExpertDefinition(
        domain="TECH_SCIENCE",
        agent="TechScienceAgent",
        prompt=(
            "You are TechScienceAgent. Analyze official product or research status, release eligibility, rollout "
            "scope, version, study evidence and deadline. "
            "Distinguish announcements, previews, limited access and general availability according to the "
            "market rules, and use rumors only as explicitly weak background."
        ),
    ),
    "CULTURE_ENTERTAINMENT": DomainExpertDefinition(
        domain="CULTURE_ENTERTAINMENT",
        agent="CultureEntertainmentAgent",
        prompt=(
            "You are CultureEntertainmentAgent. Analyze the exact title, artist, award, chart, release, box office "
            "or cultural event, using the official organizer or named measurement source. Distinguish nominations, "
            "announcements, releases and final results, and do not treat social buzz as an official outcome."
        ),
    ),
    "MENTIONS_SOCIAL": DomainExpertDefinition(
        domain="MENTIONS_SOCIAL",
        agent="MentionsSocialAgent",
        prompt=(
            "You are MentionsSocialAgent. Analyze the exact speaker, platform, account, phrase, counting window and "
            "inclusion rules. Preserve quote context and distinguish original posts, reposts, deleted material, "
            "transcripts and third-party paraphrases."
        ),
    ),
    "GENERAL": DomainExpertDefinition(
        domain="GENERAL",
        agent="GeneralTagAgent",
        prompt=(
            "You are GeneralTagAgent. Explain the event state, timeline, threshold and strongest official evidence. "
            "State clearly when the available evidence is insufficient for a domain-specific judgment."
        ),
    ),
}


def definition_for(domain: str) -> DomainExpertDefinition:
    return EXPERTS.get(domain, EXPERTS["GENERAL"])


def _crypto_fallback(
    crypto_packet: dict[str, Any] | None = None,
) -> DomainExpertReport:
    question = (crypto_packet or {}).get("market_question_analysis") or {}
    crypto_case = (crypto_packet or {}).get("crypto_case") or {}
    if question:
        trade = (crypto_packet or {}).get("wallet_trade_interpretation") or {}
        case_type = str(crypto_case.get("case_type") or "GENERAL_CRYPTO")
        fields = question.get("calculated_fields") or {}
        predicate = str(question.get("market_predicate") or "该加密市场命题")
        status = str(question.get("status") or "INCOMPLETE")
        if status != "COMPLETE":
            return DomainExpertReport(
                agent="CryptoAgent",
                domain="CRYPTO",
                market_state=f"当前无法用规则指定来源完整计算「{predicate}」。",
                signal_interpretation="问题专属数据尚不完整，因此不能用通用行情替代该市场的结算判断。",
                summary=f"「{predicate}」的规则指定计算尚未完成，本次仅保留研究记录。",
                alignment="INSUFFICIENT",
                risk_flags=["CRYPTO_QUESTION_ANALYSIS_INCOMPLETE"],
                unknowns=["规则指定价格源、精确窗口或问题专属状态仍有未核实项。"],
                wallet_trade_interpretation=_optional_model(WalletTradeInterpretation, trade),
            )
        satisfied = question.get("condition_satisfied")
        result_text = "已满足" if satisfied is True else "尚未满足" if satisfied is False else "尚不能判定"
        if case_type == "SHORT_WINDOW_UP_DOWN":
            state = (
                f"该市场比较 {fields.get('exact_window_start')} 至 {fields.get('exact_window_end')} 的"
                f"{fields.get('resolution_price_source')} {fields.get('resolution_pair')} 价格："
                f"起点 {fields.get('start_price')}，终点 {fields.get('end_price')}，"
                f"窗口收益 {float(fields.get('realized_window_return') or 0):+.4f}%，"
                f"因此规则方向为 {'Up' if satisfied else 'Down'}。"
            )
        elif case_type == "PRICE_TOUCH_OR_RANGE":
            state = (
                f"该市场检查 {fields.get('window_start')} 至 {fields.get('window_end')} 是否触及 "
                f"{fields.get('threshold')}：规则窗口高低点为 {fields.get('window_high')} / "
                f"{fields.get('window_low')}，当前状态为{result_text}，首次触及时点为 "
                f"{fields.get('threshold_hit_at')}。"
            )
        elif case_type == "PRICE_ENDPOINT":
            state = (
                f"该市场只比较 {fields.get('endpoint_time')} 的终点价格，不采用盘中触价规则；"
                f"终点价格为 {fields.get('endpoint_price')}，当前结论为{result_text}。"
            )
        elif case_type == "FDV_AFTER_LAUNCH":
            state = (
                f"按总供应量 {fields.get('total_supply')} × 正式测量价 "
                f"{fields.get('spot_price_at_measurement')} 计算，FDV 为 {fields.get('calculated_fdv')}，"
                f"相对阈值状态为 {fields.get('threshold_status')}；盘前价未被用作正式测量价。"
            )
        elif case_type == "TOKEN_LAUNCH":
            state = (
                f"代币当前状态为 {fields.get('official_token_status')}；合约、领取、可转让与交易状态分别为 "
                f"{fields.get('contract_address_status')}、{fields.get('claim_status')}、"
                f"{fields.get('transferability_requirement')}、{fields.get('exchange_listing_status')}。"
            )
        elif case_type == "CORPORATE_BTC_ACTION":
            state = (
                f"公司官方持仓为 {fields.get('official_holdings')}（截至 "
                f"{fields.get('official_holdings_as_of')}）；链上转账目的地为 "
                f"{fields.get('custody_destination')}，可验证出售状态为 "
                f"{'已确认' if fields.get('confirmed_sale') else '未确认'}。"
            )
        else:
            state = f"该市场命题为「{predicate}」，问题专属证据计算结果为{result_text}。"
        interpretation = str(trade.get("interpretation") or "当前无法解释地址成交与该市场计算的关系。")
        alignment = {
            "ALIGNED": "SUPPORTS",
            "CONTRADICTED": "CONTRADICTS",
            "MIXED": "MIXED",
        }.get(str(trade.get("alignment")), "INSUFFICIENT")
        return DomainExpertReport(
            agent="CryptoAgent",
            domain="CRYPTO",
            market_state=state,
            signal_interpretation=interpretation,
            summary=f"{state}{interpretation}",
            alignment=_alignment(alignment),
            claims=[
                ClaimDraft(statement=state, modality="FACT", confidence=0.9, evidence_keys=["rules", "osint"]),
                ClaimDraft(
                    statement=interpretation,
                    modality="INFERENCE",
                    confidence=0.68,
                    evidence_keys=["signal", "osint"],
                ),
            ],
            key_factors=[
                "规则指定的市场问题计算",
                "地址 outcome 与计算结果的关系",
                "失效条件与后续官方状态",
            ],
            risk_flags=[] if (crypto_packet or {}).get("point_in_time_complete") else ["CRYPTO_PIT_INCOMPLETE"],
            unknowns=list(question.get("invalidation_conditions") or []),
            wallet_trade_interpretation=_optional_model(WalletTradeInterpretation, trade),
        )
    return DomainExpertReport(
        agent="CryptoAgent",
        domain="CRYPTO",
        market_state="缺少与市场结算问题对应的分析结果。",
        signal_interpretation="通用行情和旧版趋势预测不能替代规则指定的计算。",
        summary="问题专属证据不完整，本次仅保留研究记录。",
        alignment="INSUFFICIENT",
        risk_flags=["CRYPTO_QUESTION_ANALYSIS_INCOMPLETE"],
        unknowns=["规则指定来源、精确窗口和市场命题尚未核实。"],
    )


def deterministic_report(
    domain: str,
    evidence: list[dict[str, Any]],
    *,
    crypto_packet: dict[str, Any] | None = None,
) -> DomainExpertReport:
    definition = definition_for(domain)
    if definition.domain == "CRYPTO":
        return _crypto_fallback(crypto_packet)
    state = "领域语义调查尚未完成。"
    interpretation = "原始材料保留在证据中，未完成分析时不生成事件结论或交易优势判断。"
    return DomainExpertReport(
        agent=definition.agent,
        domain=definition.domain,
        market_state=state,
        signal_interpretation=interpretation,
        summary=f"{state}{interpretation}",
        alignment="INSUFFICIENT",
        claims=[],
        risk_flags=["DOMAIN_ANALYSIS_NOT_COMPLETED"],
        unknowns=["需要对照原始材料完成领域调查。"],
    )
