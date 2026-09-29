"""Wallet, market-rule, and cross-market report builders."""

from __future__ import annotations

import json
from typing import Any

from smart_money.contracts import parse_utc
from smart_money.research.contract_field_extractors import quoted_rule_fields
from smart_money.research.engine_contract import EngineHost
from smart_money.research.engine_support import (
    MARKET_PROMPT_FIELDS,
    _compact,
    _money,
    _pct,
    _quality_packet,
    _select_fields,
)
from smart_money.research.models import (
    ClaimDraft,
    RulesReport,
    SignalCandidate,
    SpecialistReport,
)


class ReportBuilderMixin(EngineHost):
    def _wallet_forensics(self, candidate: SignalCandidate, context: dict[str, Any]) -> SpecialistReport:
        wallet_profiles = candidate.wallet_profiles
        claims: list[ClaimDraft] = []
        risks: list[str] = []
        if candidate.wallet_profiles:
            for profile in wallet_profiles:
                address = profile.wallet[:10]
                if profile.display_name:
                    claims.append(
                        ClaimDraft(
                            statement=(
                                f"账户 {address} 在观察名单中的显示名称为 {profile.display_name}；"
                                "该名称不代表真实身份认证。"
                            ),
                            modality="FACT",
                            confidence=1,
                            evidence_keys=["signal"],
                        )
                    )
                for source in profile.discovery_sources:
                    parts = str(source.get("source", "")).split(":")
                    if len(parts) != 3 or parts[0] != "official" or source.get("pnl") is None:
                        continue
                    period = {"WEEK": "周榜", "MONTH": "月榜", "ALL": "总榜"}.get(parts[2].upper())
                    if period and source.get("last_seen_at"):
                        claims.append(
                            ClaimDraft(
                                statement=(
                                    f"截至 {source['last_seen_at']} 的官方 {parts[1]} 盈利{period}记录中，"
                                    f"账户 {address} 的榜单展示 PnL 为 {_money(source['pnl'])}。"
                                    "这是榜单口径，不等于已核实的独立事件胜率或专家资格。"
                                ),
                                modality="FACT",
                                confidence=1,
                                evidence_keys=["signal"],
                            )
                        )
                if profile.sector_pnl is not None:
                    claims.append(
                        ClaimDraft(
                            statement=f"账户 {address} 在相关板块的信号时点净收益为 {_money(profile.sector_pnl)}。",
                            modality="FACT",
                            confidence=1,
                            evidence_keys=["signal"],
                        )
                    )
                if profile.sector_resolved_count is not None and profile.sector_win_rate is not None:
                    claims.append(
                        ClaimDraft(
                            statement=(
                                f"账户 {address} 在相关板块有 {profile.sector_resolved_count} 个已结算独立事件，"
                                f"记录胜率为 {_pct(profile.sector_win_rate)}。"
                            ),
                            modality="FACT",
                            confidence=1,
                            evidence_keys=["signal"],
                        )
                    )
                if profile.same_price_band_median_size is not None and profile.current_trade_size_multiple is not None:
                    claims.append(
                        ClaimDraft(
                            statement=(
                                f"账户 {address} 在相近成本区间的历史投入中位数为 "
                                f"{_money(profile.same_price_band_median_size)}，本次投入为其 "
                                f"{profile.current_trade_size_multiple:.1f} 倍。"
                            ),
                            modality="FACT",
                            confidence=1,
                            evidence_keys=["signal"],
                        )
                    )
                if profile.position_change != "UNKNOWN":
                    action = {"OPEN": "开仓", "ADD": "加仓", "REDUCE": "减仓", "EXIT": "清仓"}.get(
                        profile.position_change, profile.position_change
                    )
                    positions = (
                        f"，相关份额从 {profile.position_before} 份变为 {profile.position_after} 份"
                        if profile.position_before is not None and profile.position_after is not None
                        else ""
                    )
                    claims.append(
                        ClaimDraft(
                            statement=f"可重建持仓显示，账户 {address} 本次为{action}{positions}。",
                            modality="FACT",
                            confidence=1,
                            evidence_keys=["signal"],
                        )
                    )
            if len(wallet_profiles) >= 2:
                risks.append("MULTI_WALLET_COMMON_CONTROL_UNKNOWN")
        trade = candidate.evidence.get("trade") or {}
        if candidate.side in {"BUY", "SELL"} and all(
            value is not None
            for value in (candidate.outcome, candidate.entry_price, candidate.notional, trade.get("size"))
        ):
            side = "买入" if candidate.side == "BUY" else "卖出"
            claims.append(
                ClaimDraft(
                    statement=(
                        f"账户 {candidate.wallet[:10]} {side} {candidate.outcome} {trade['size']} 份，"
                        f"本次成交均价每份 {candidate.entry_price} USDC，成交金额 {candidate.notional} USDC。"
                    ),
                    modality="FACT",
                    confidence=1,
                    evidence_keys=["signal"],
                )
            )
        limitations = ["无法观察该地址在其他平台的对冲仓位。"]
        if not wallet_profiles or all(p.sector_pnl is None for p in wallet_profiles):
            limitations.append("未取得可核实的同板块历史业绩，不能据本次成交评价该地址的预测能力。")
        if not wallet_profiles or all(p.current_trade_size_multiple is None for p in wallet_profiles):
            limitations.append("缺少交易前的可比投入基准，不能判断本次投入是否异常。")
        if any(profile.sector_match is False for profile in wallet_profiles):
            risks.append("CURRENT_SECTOR_DIFFERS_FROM_BEST_PROFILE")
        if not claims:
            risks.append("LIMITED_WALLET_HISTORY")
        fallback = SpecialistReport(
            agent="WalletForensicsAgent",
            summary="；".join(claim.statement for claim in claims)
            or "该地址已进入候选名单，但当前证据缺少可发布的板块历史统计。",
            claims=claims,
            risk_flags=risks,
            unknowns=limitations,
        )
        self.runtime["agents"]["wallet-forensics"] = {
            "source": "deterministic-summary",
            "status": "SUCCESS",
            "reason": "FROZEN_FACTS_ONLY",
        }
        return fallback

    def _rules_analysis(
        self, candidate: SignalCandidate, context: dict[str, Any], required_fields: list[str]
    ) -> RulesReport:
        market = context.get("market") or {}
        rules = context.get("rules") or {}
        claims: list[ClaimDraft] = []
        rules_text = rules.get("rules_text") or market.get("rules_current")
        snapshot_at = parse_utc(rules.get("snapshot_at") or context.get("market_obtained_at"))
        later = snapshot_at is None or snapshot_at > candidate.as_of
        if rules_text:
            claims.append(
                ClaimDraft(
                    statement=f"市场结算规则摘要：{_compact(rules_text, 360)}",
                    modality="FACT",
                    confidence=1,
                    evidence_keys=["rules" if rules else "market"],
                    time_scope="RESEARCH_UPDATE" if later else "TRADE_TIME",
                )
            )
        risk_flags = []
        if not rules_text:
            risk_flags.append("RULE_SNAPSHOT_MISSING")
        if float(market.get("rules_risk_score") or 0) > 0:
            risk_flags.append("RULE_RISK_RECORDED")
        fallback = RulesReport(
            agent="RulesAndOsintAgent",
            summary=(_compact(rules_text, 500) if rules_text else "未取得规则正文，不能依据市场标题补全结算条件。"),
            claims=claims,
            risk_flags=risk_flags,
            unknowns=["规则语义尚未核验。"],
        )
        report = self._call(
            "rules-osint",
            "You are RulesAnalyst (output agent RulesAndOsintAgent). Read only the supplied rule text. Identify "
            "conditions, operators, objects, dates, time zones, required sources, exceptions and ambiguities. "
            "For relevant required_fields return questions with an exact nonempty rule_quote and the field name. "
            "Do not invent fields, fetch sources, decide settlement or use the title instead of missing rules. "
            "Return at most three distinct core rule claims; questions must use only supplied required_fields. "
            "Select exact original passages from the quote choices. Omit fields not stated in these rules; "
            "do not ask whether settlement rules mention trading signals or wallets. "
            "Separate announced, planned, approved, available and completed. Preserve unresolved ambiguities. "
            "Bind source_ids, URLs, entities, regions and fact_time only when stated in the exact rule_quote. "
            "For literal rule parameters also return value and value_quote: copy the exact scalar from the rule, "
            "including explicit year and timezone in timestamps. Do not translate prose into operator symbols, "
            "infer missing precision or calculate a value. Leave value null when it cannot be copied literally.",
            {
                "market_id": candidate.market_id,
                "as_of": candidate.as_of.isoformat(),
                "market": _select_fields(market, MARKET_PROMPT_FIELDS),
                "rules": rules,
                "rules_evidence_id": f"ev_{self.runtime['runId'].removeprefix('mas_')}_rules",
                "required_fields": required_fields,
                "data_quality": _quality_packet(candidate, context),
                "rules_time_scope": "RESEARCH_UPDATE" if later else "TRADE_TIME",
            },
            RulesReport,
            fallback,
        )
        invalid = [
            q
            for q in report.questions
            if q.field not in required_fields or not q.rule_quote.strip() or q.rule_quote not in str(rules_text or "")
        ]
        for q in report.questions:
            if any(url not in q.rule_quote for url in q.urls):
                invalid.append(q)
            for source_id in q.source_ids:
                source = next((s for s in self.evidence_router.source_tool_registry.catalog if s.id == source_id), None)
                if source is None or not any(
                    t.casefold() in q.rule_quote.casefold() for t in (source.id, source.name, *source.domains)
                ):
                    invalid.append(q)
            if any(value.casefold() not in q.rule_quote.casefold() for value in [*q.entities, *q.regions]):
                invalid.append(q)
        if report.agent != "RulesAndOsintAgent":
            self.runtime["agents"]["rules-osint"].update(status="FAILED", reason="WRONG_RULES_AGENT")
            return fallback
        if invalid:
            self.runtime["agents"]["rules-osint"].update(status="FAILED", reason="INVALID_RULE_QUESTIONS")
            report.questions = [q for q in report.questions if q not in invalid]
            report.unknowns.append("部分规则问题未通过原文校验，已剔除；其余主张仍须独立核验。")
        if later:
            report.claims = [c.model_copy(update={"time_scope": "RESEARCH_UPDATE"}) for c in report.claims]
        self.runtime["ruleQuestions"] = [q.model_dump(mode="json") for q in report.questions]
        if rules.get("rules_text"):
            projection = quoted_rule_fields(self.runtime["ruleQuestions"], rules)
            fields = projection.get("contract_fields", {})
            report.claims.extend(
                ClaimDraft(
                    statement=f"规则参数 {field} = {json.dumps(value, ensure_ascii=False)}。",
                    modality="FACT",
                    confidence=1,
                    evidence_keys=["rules"],
                    contract_field=field,
                    field_value=value,
                    time_scope="RESEARCH_UPDATE" if later else "TRADE_TIME",
                )
                for field, value in fields.items()
            )
        return report
