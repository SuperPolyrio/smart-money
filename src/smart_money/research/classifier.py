"""Predicate-first market classification and capability gating."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from smart_money.research.contracts import (
    CapabilityDecision,
    CapabilityState,
    CaseClassification,
    Domain,
    JurisdictionScope,
    MarketArchetype,
    PoliticalCaseType,
    PoliticsCase,
)
from smart_money.research.models import SignalCandidate


@dataclass(frozen=True)
class _Rule:
    archetype: MarketArchetype
    domain: Domain
    patterns: tuple[str, ...]
    modalities: tuple[str, ...]


RULES = (
    _Rule(
        MarketArchetype.MENTION_OR_POST_COUNT,
        Domain.MENTIONS_SOCIAL,
        (r"\bmentions?\b", r"\bposts?\b", r"\btweets?\b", "提到", "发帖"),
        ("text", "count"),
    ),
    _Rule(
        MarketArchetype.EXACT_WEATHER_BUCKET,
        Domain.WEATHER_CLIMATE,
        ("temperature", "highest temp", "lowest temp", "气温", "温度"),
        ("weather_forecast", "observation"),
    ),
    _Rule(
        MarketArchetype.WEATHER_THRESHOLD,
        Domain.WEATHER_CLIMATE,
        ("hurricane", "rainfall", "snowfall", "降雨", "飓风"),
        ("weather_forecast", "observation"),
    ),
    _Rule(
        MarketArchetype.CENTRAL_BANK_DECISION,
        Domain.MACRO_ECONOMY,
        ("interest rate", "fed decision", "fomc", "加息", "降息", "利率"),
        ("official_release", "macro_data"),
    ),
    _Rule(
        MarketArchetype.MACRO_DATA_RELEASE,
        Domain.MACRO_ECONOMY,
        ("cpi", "inflation", "unemployment", "nonfarm", "gdp", "通胀", "失业率"),
        ("official_release", "macro_data"),
    ),
    _Rule(
        MarketArchetype.PRICE_DIRECTION_SHORT_WINDOW,
        Domain.CRYPTO,
        ("up or down", "higher or lower", "涨还是跌"),
        ("spot_price", "orderbook"),
    ),
    _Rule(
        MarketArchetype.CRYPTO_FDV_AFTER_LAUNCH,
        Domain.CRYPTO,
        (r"\bfdv\b", "fully diluted valuation"),
        ("official_supply", "measurement_price"),
    ),
    _Rule(
        MarketArchetype.CRYPTO_TOKEN_LAUNCH,
        Domain.CRYPTO,
        ("launch a token", "token launch", "issue a token", "airdrop token"),
        ("official_announcement", "contract_state", "trading_state"),
    ),
    _Rule(
        MarketArchetype.CRYPTO_CORPORATE_BTC_ACTION,
        Domain.CRYPTO,
        (
            "sell bitcoin",
            "sell any bitcoin",
            "sell btc",
            "bitcoin holdings",
            r"\b(?:sell|selling|sold)\b.{0,40}\b(?:bitcoin|btc)\b",
        ),
        ("official_filing", "company_treasury", "onchain_transfer"),
    ),
    _Rule(
        MarketArchetype.CRYPTO_EXCHANGE_LISTING,
        Domain.CRYPTO,
        ("listed on", "exchange listing", "list spot", "listing on"),
        ("official_exchange_announcement", "official_project_announcement"),
    ),
    _Rule(
        MarketArchetype.CRYPTO_REGULATION,
        Domain.CRYPTO,
        ("sec approve", "crypto regulation", "crypto bill", r"\bcftc\b"),
        ("official_record", "legal_process"),
    ),
    _Rule(
        MarketArchetype.CRYPTO_PUBLIC_SALE,
        Domain.CRYPTO,
        ("public sale", "token sale", r"\bico\b"),
        ("official_announcement", "sale_state"),
    ),
    _Rule(
        MarketArchetype.CRYPTO_PROTOCOL_EVENT,
        Domain.CRYPTO,
        ("mainnet", "hard fork", "protocol upgrade", "staking launch"),
        ("official_status", "protocol_state"),
    ),
    _Rule(
        MarketArchetype.PRICE_THRESHOLD_TOUCH,
        Domain.CRYPTO,
        (
            r"\b(?:bitcoin|btc|ethereum|eth|solana|sol|bnb|doge|xrp|hype)\b.{0,60}"
            r"\b(?:reach|hit|touch|dip\s+to|exceed)\s+(?:\$|USD\s*)?[\d,]+(?:\.\d+)?",
            "达到",
            "触及",
        ),
        ("spot_price", "high_low"),
    ),
    _Rule(
        MarketArchetype.PRICE_THRESHOLD_ENDPOINT,
        Domain.CRYPTO,
        (
            r"\b(?:bitcoin|btc|ethereum|eth|solana|sol|bnb|doge|xrp|hype)\b.{0,60}"
            r"\b(?:above|below|over|under)\s+(?:\$|USD\s*)?[\d,]+(?:\.\d+)?\s+on\b",
            "price on",
            "高于",
            "低于",
        ),
        ("spot_price", "close_price"),
    ),
    _Rule(
        MarketArchetype.PRICE_THRESHOLD_ENDPOINT,
        Domain.FINANCE,
        (r"\bwti\b", r"\bbrent\b", "oil close", "gold close", "原油收盘", "黄金收盘"),
        ("commodity_price", "settlement_price"),
    ),
    _Rule(
        MarketArchetype.POLITICAL_PARTY_CONTROL,
        Domain.POLITICS_ELECTIONS,
        (
            r"\b(?:win|gain|retain|lose|control)\b.{0,45}\b(?:majority|parliament|congress|senate|house)\b",
            r"\b(?:party|coalition)\b.{0,45}\b(?:majority|control)\b",
            "政党控制",
            "赢得多数席位",
        ),
        ("official_result", "seat_projection"),
    ),
    _Rule(
        MarketArchetype.ELECTION_WINNER,
        Domain.POLITICS_ELECTIONS,
        (
            r"\bwin(?:s)?\b.{0,50}\b(?:election|race|primary)\b",
            r"\b(?:election|race|primary)\b.{0,50}\bwinner\b",
            "elected",
            "选举获胜",
            "当选",
        ),
        ("official_result", "polling"),
    ),
    _Rule(
        MarketArchetype.APPOINTMENT_OR_REMOVAL,
        Domain.POLITICS_ELECTIONS,
        ("appointed", "reappointed", "resign", "removed", "step down", "任命", "辞职", "下台"),
        ("official_record", "news"),
    ),
    _Rule(
        MarketArchetype.POLITICAL_POLLING,
        Domain.POLITICS_ELECTIONS,
        ("approval rating", "favorability", "poll at least", "polling average", "支持率", "民调达到"),
        ("polling", "survey_methodology"),
    ),
    _Rule(
        MarketArchetype.POLITICAL_COURT_DECISION,
        Domain.POLITICS_ELECTIONS,
        ("supreme court", "court rule", "court ruling", "appeal court", "法院裁决", "最高法院"),
        ("official_record", "legal"),
    ),
    _Rule(
        MarketArchetype.POLITICAL_LEGISLATION,
        Domain.POLITICS_ELECTIONS,
        (
            r"\bbill\b.{0,50}\bpass(?:es|ed)?\b",
            r"\bpass(?:es|ed)?\b.{0,50}\bbill\b",
            "legislation",
            "become law",
            "signed into law",
            "法案",
            "立法",
        ),
        ("official_record", "legislative"),
    ),
    _Rule(
        MarketArchetype.POLITICAL_INSTITUTIONAL_VOTE,
        Domain.POLITICS_ELECTIONS,
        (
            r"\b(?:security council|general assembly|parliament|congress|senate|legislature|council)\b"
            r".{0,70}\b(?:vote|approve|adopt|reject|resolution)\b",
            r"\b(?:vote|approve|adopt|reject)\b.{0,70}"
            r"\b(?:security council|general assembly|parliament|congress|senate|legislature|council)\b",
            "安理会表决",
            "联合国大会表决",
        ),
        ("official_record", "institutional_vote"),
    ),
    _Rule(
        MarketArchetype.POLITICAL_EXECUTIVE_ACTION,
        Domain.POLITICS_ELECTIONS,
        ("executive order", "presidential decree", "government decree", "行政命令", "总统令", "政府禁令"),
        ("official_record", "executive_action"),
    ),
    _Rule(
        MarketArchetype.MILITARY_ACTION,
        Domain.GEOPOLITICS,
        ("military", "strike", "invade", "airstrike", "军事", "袭击", "入侵"),
        ("official_statement", "geospatial"),
    ),
    _Rule(
        MarketArchetype.TERRITORY_CONTROL,
        Domain.GEOPOLITICS,
        ("control of", "capture", "territory", "占领", "控制"),
        ("geospatial", "official_statement"),
    ),
    _Rule(
        MarketArchetype.ESPORTS_MATCH_WINNER,
        Domain.ESPORTS,
        ("league of legends", "counter-strike", "valorant", "dota", "esports"),
        ("match_result", "odds"),
    ),
    _Rule(
        MarketArchetype.SPORTS_SERIES_WINNER,
        Domain.SPORTS,
        ("series", "championship", "world cup", "stanley cup", "总冠军"),
        ("match_result", "odds"),
    ),
    _Rule(
        MarketArchetype.SPORTS_MATCH_WINNER,
        Domain.SPORTS,
        (r"\bvs\.?\s", "beat ", "match winner", "game winner", "比赛获胜"),
        ("match_result", "odds"),
    ),
    _Rule(
        MarketArchetype.IPO_VALUATION,
        Domain.FINANCE,
        ("ipo valuation", "market cap at ipo", "上市估值"),
        ("filing", "market_data"),
    ),
    _Rule(
        MarketArchetype.IPO_COMPLETION, Domain.FINANCE, ("ipo by", "go public", "上市"), ("filing", "official_release")
    ),
    _Rule(
        MarketArchetype.EARNINGS_OR_CORPORATE_EVENT,
        Domain.FINANCE,
        ("earnings", "revenue", "acquire", "merger", "财报", "营收", "收购"),
        ("filing", "market_data"),
    ),
    _Rule(
        MarketArchetype.PRODUCT_RELEASE_BY_DATE,
        Domain.TECH_SCIENCE,
        ("release", "launch", "ship", "发布", "推出", "上线"),
        ("official_release", "product_status"),
    ),
    _Rule(
        MarketArchetype.SCIENTIFIC_MILESTONE,
        Domain.TECH_SCIENCE,
        ("scientific", "trial", "fda approval", "space launch", "科学", "试验"),
        ("paper", "official_release"),
    ),
    _Rule(
        MarketArchetype.AWARD_WINNER,
        Domain.CULTURE_ENTERTAINMENT,
        ("win best", "award", "oscar", "grammy", "获奖"),
        ("official_result", "media"),
    ),
    _Rule(
        MarketArchetype.BOX_OFFICE_THRESHOLD,
        Domain.CULTURE_ENTERTAINMENT,
        ("box office", "gross over", "票房"),
        ("box_office", "media"),
    ),
    _Rule(
        MarketArchetype.OFFICIAL_RANKING_SNAPSHOT,
        Domain.CULTURE_ENTERTAINMENT,
        ("ranking", "chart", "top 10", "排名", "榜单"),
        ("official_snapshot",),
    ),
)


class CaseClassifier:
    version = "case-classifier-v2"

    def classify(
        self,
        candidate: SignalCandidate,
        context: dict[str, Any],
    ) -> CaseClassification:
        market = context.get("market") or {}
        title = str(market.get("title") or market.get("question") or "")
        rules = str((context.get("rules") or {}).get("rules_text") or market.get("rules_current") or "")
        tags = " ".join(str(item) for item in (market.get("tags") or []))
        category = " ".join(
            str(market.get(key) or "")
            for key in ("official_category", "internal_category_l1", "internal_category_l2", "series")
        )
        predicate_text = f"{title}\n{rules}".lower()
        metadata_text = f"{tags}\n{category}\n{candidate.sector_id}".lower()
        reasons: list[str] = []
        conflicts: list[str] = []
        selected: _Rule | None = None
        for rule in RULES:
            match = next((pattern for pattern in rule.patterns if re.search(pattern, predicate_text, re.I)), None)
            if match:
                selected = rule
                reasons.append(f"PREDICATE_MATCH:{match}")
                break
        if selected is None:
            selected = self._metadata_fallback(metadata_text)
            reasons.append("OFFICIAL_METADATA_FALLBACK" if selected else "GENERAL_EVENT_FALLBACK")
        if selected is None:
            selected = _Rule(MarketArchetype.GENERAL_EVENT, Domain.GENERAL, (), ("web",))

        metadata_domain = self._metadata_domain(metadata_text)
        secondaries: list[Domain] = []
        if selected.archetype == MarketArchetype.MENTION_OR_POST_COUNT and any(
            token in f"{predicate_text} {metadata_text}"
            for token in ("gta", "pop-culture", "entertainment", "movie", "music", "game")
        ):
            secondaries.append(Domain.CULTURE_ENTERTAINMENT)
        if metadata_domain and metadata_domain != selected.domain:
            if metadata_domain not in secondaries:
                secondaries.append(metadata_domain)
            conflicts.append(f"PREDICATE_DOMAIN_OVERRIDES_METADATA:{metadata_domain.value}")
        if selected.archetype == MarketArchetype.CENTRAL_BANK_DECISION and Domain.FINANCE not in secondaries:
            secondaries.append(Domain.FINANCE)
        entity = self._entity(title)
        source = self._authoritative_source(rules)
        political_case_type = self._political_case_type(selected.domain, selected.archetype, predicate_text)
        politics_case = (
            self._politics_case(
                political_case_type,
                entity=entity,
                text=predicate_text,
                market=market,
                resolution_source=source,
            )
            if political_case_type is not None
            else None
        )
        resolution = f"AUTHORITATIVE_SOURCE:{source}" if source else "RULE_PREDICATE"
        confidence = (
            0.96
            if reasons[0].startswith("PREDICATE_MATCH")
            else 0.72
            if reasons[0] == "OFFICIAL_METADATA_FALLBACK"
            else 0.20
        )
        if conflicts:
            confidence = min(confidence, 0.79)
        return CaseClassification(
            primary_domain=selected.domain,
            secondary_domains=secondaries,
            market_archetype=selected.archetype,
            political_case_type=political_case_type,
            politics_case=politics_case,
            resolution_mechanism=resolution,
            underlying_entity=entity,
            action_or_metric=self._action_metric(selected.archetype),
            time_horizon=self._time_horizon(title, market),
            data_modality=list(selected.modalities),
            required_capabilities=[f"{selected.domain.value}.{selected.archetype.value}"],
            confidence=confidence,
            reasons=reasons,
            conflicts=conflicts,
        )

    @staticmethod
    def _political_case_type(
        domain: Domain,
        archetype: MarketArchetype,
        predicate_text: str = "",
    ) -> PoliticalCaseType | None:
        if domain != Domain.POLITICS_ELECTIONS:
            return None
        case_type = {
            MarketArchetype.ELECTION_WINNER: PoliticalCaseType.ELECTION,
            MarketArchetype.APPOINTMENT_OR_REMOVAL: PoliticalCaseType.APPOINTMENT_OR_REMOVAL,
            MarketArchetype.POLITICAL_EXECUTIVE_ACTION: PoliticalCaseType.EXECUTIVE_ACTION,
            MarketArchetype.POLITICAL_PARTY_CONTROL: PoliticalCaseType.PARTY_CONTROL,
            MarketArchetype.POLITICAL_POLLING: PoliticalCaseType.POLLING_OR_APPROVAL,
            MarketArchetype.POLITICAL_INSTITUTIONAL_VOTE: PoliticalCaseType.INSTITUTIONAL_VOTE,
            MarketArchetype.POLITICAL_COURT_DECISION: PoliticalCaseType.COURT_DECISION,
            MarketArchetype.POLITICAL_LEGISLATION: PoliticalCaseType.LEGISLATION,
        }.get(archetype)
        if case_type is not None:
            return case_type
        if re.search(r"\b(court|tribunal|judge|ruling|appeal)\b|法院|法庭|裁决", predicate_text, re.I):
            return PoliticalCaseType.COURT_DECISION
        if re.search(r"\b(bill|legislation|law|statute|veto)\b|法案|立法|法律", predicate_text, re.I):
            return PoliticalCaseType.LEGISLATION
        return PoliticalCaseType.GENERAL

    @staticmethod
    def _politics_case(
        case_type: PoliticalCaseType,
        *,
        entity: str | None,
        text: str,
        market: dict[str, Any],
        resolution_source: str | None,
    ) -> PoliticsCase:
        jurisdiction_code, scope = CaseClassifier._jurisdiction(text, market)
        institution = CaseClassifier._institution(text, market)
        actors = market.get("actors")
        actor_list = [str(item) for item in actors if str(item).strip()] if isinstance(actors, list) else []
        if entity and entity not in actor_list:
            actor_list.append(entity)
        if institution and institution not in actor_list:
            actor_list.append(institution)
        return PoliticsCase(
            case_type=case_type,
            jurisdiction_scope=scope,
            jurisdiction_code=jurisdiction_code,
            institution=institution,
            actors=actor_list[:12],
            decision_stage=CaseClassifier._decision_stage(text, market),
            deadline=str(market.get("end_at") or market.get("deadline") or "") or None,
            resolution_source=resolution_source,
        )

    @staticmethod
    def _jurisdiction(text: str, market: dict[str, Any]) -> tuple[str | None, JurisdictionScope]:
        explicit = next(
            (
                str(market.get(key)).strip()
                for key in ("jurisdiction_code", "jurisdiction", "country_code", "country", "region")
                if market.get(key) not in (None, "")
            ),
            None,
        )
        value = explicit.upper().replace("_", "-") if explicit else None
        corpus = text.lower()
        if value in {"UN", "UNITED NATIONS"} or re.search(
            r"\b(united nations|un security council|un general assembly)\b", corpus
        ):
            return "UN", JurisdictionScope.INTERNATIONAL_ORGANIZATION
        if value in {"EU", "EUROPEAN UNION"} or re.search(
            r"\b(european union|european parliament|european commission|european council)\b", corpus
        ):
            return "EU", JurisdictionScope.SUPRANATIONAL
        if value:
            return value, JurisdictionScope.SUBNATIONAL if "-" in value else JurisdictionScope.NATIONAL
        if re.search(r"\b(united kingdom|uk|uk parliament|house of commons)\b", corpus):
            return "GB", JurisdictionScope.NATIONAL
        if re.search(r"\b(united states|u\.s\.|us congress|white house)\b", corpus):
            return "US", JurisdictionScope.NATIONAL
        return None, JurisdictionScope.UNKNOWN

    @staticmethod
    def _institution(text: str, market: dict[str, Any]) -> str | None:
        explicit = str(market.get("institution") or "").strip()
        if explicit:
            return explicit
        patterns = (
            (r"\b(?:un security council|united nations security council)\b", "UN_SECURITY_COUNCIL"),
            (r"\b(?:un general assembly|united nations general assembly)\b", "UN_GENERAL_ASSEMBLY"),
            (r"\b(?:united nations|\bUN\b)\b", "UNITED_NATIONS"),
            (r"\beuropean parliament\b", "EUROPEAN_PARLIAMENT"),
            (r"\beuropean commission\b", "EUROPEAN_COMMISSION"),
            (r"\beuropean council\b", "EUROPEAN_COUNCIL"),
            (r"\b(?:uk parliament|british parliament)\b", "UK_PARLIAMENT"),
            (r"\bhouse of commons\b", "UK_HOUSE_OF_COMMONS"),
            (r"\bsupreme court\b", "SUPREME_COURT"),
            (r"\b(?:us congress|united states congress)\b", "US_CONGRESS"),
            (r"\bsenate\b", "SENATE"),
            (r"\bhouse of representatives\b", "HOUSE_OF_REPRESENTATIVES"),
        )
        return next((name for pattern, name in patterns if re.search(pattern, text, re.I)), None)

    @staticmethod
    def _decision_stage(text: str, market: dict[str, Any]) -> str:
        explicit = str(market.get("decision_stage") or market.get("stage") or "").strip()
        if explicit:
            return explicit.upper().replace(" ", "_")
        stages = (
            (r"\b(certified|certification|final result)\b|正式认证", "CERTIFICATION"),
            (r"\b(primary|first round)\b|初选", "PRIMARY"),
            (r"\b(appeal|appellate)\b|上诉", "APPEAL"),
            (r"\b(confirm|confirmation)\b|确认表决", "CONFIRMATION"),
            (r"\b(vote|ballot|approve|adopt|reject)\b|表决|投票|通过|否决", "VOTE"),
            (r"\b(nominee|nominated|nomination)\b|提名", "NOMINATION"),
            (r"\b(signed|promulgated|enacted)\b|签署|颁布", "ENACTED"),
        )
        return next((stage for pattern, stage in stages if re.search(pattern, text, re.I)), "UNKNOWN")

    @staticmethod
    def _metadata_fallback(text: str) -> _Rule | None:
        domain = CaseClassifier._metadata_domain(text)
        if domain == Domain.CRYPTO:
            return _Rule(MarketArchetype.GENERAL_CRYPTO, domain, (), ("registered_crypto_data",))
        return _Rule(MarketArchetype.GENERAL_EVENT, domain, (), ("web",)) if domain else None

    @staticmethod
    def _metadata_domain(text: str) -> Domain | None:
        for words, domain in (
            (("crypto", "bitcoin", "ethereum"), Domain.CRYPTO),
            (("weather", "climate"), Domain.WEATHER_CLIMATE),
            (("politics", "election"), Domain.POLITICS_ELECTIONS),
            (("geopolitics", "war"), Domain.GEOPOLITICS),
            (("esports",), Domain.ESPORTS),
            (("sports", "nba", "nfl", "soccer"), Domain.SPORTS),
            (("finance", "stocks"), Domain.FINANCE),
            (("economy", "macro"), Domain.MACRO_ECONOMY),
            (("science", "tech"), Domain.TECH_SCIENCE),
            (("culture", "entertainment"), Domain.CULTURE_ENTERTAINMENT),
        ):
            if any(word in text for word in words):
                return domain
        return None

    @staticmethod
    def _entity(title: str) -> str | None:
        cleaned = re.sub(r"^(will|does|is|are|can)\s+", "", title.strip(), flags=re.I)
        cleaned = re.split(
            r"\b(?:win|launch|release|reach|be above|be below|mention|be (?:re)?appointed|resign|step down|"
            r"pass|approve|adopt|reject|rule|issue|sign)\b",
            cleaned,
            maxsplit=1,
            flags=re.I,
        )[0]
        return cleaned.strip(" ?:-")[:160] or None

    @staticmethod
    def _authoritative_source(rules: str) -> str | None:
        match = re.search(
            r"(?:according to|resolved (?:using|by)|source(?:d)? from)\s+([^.;\n]{3,100})",
            rules,
            re.I,
        )
        return match.group(1).strip() if match else None

    @staticmethod
    def _time_horizon(title: str, market: dict[str, Any]) -> str:
        match = re.search(r"\b(\d+)\s*(minute|hour|day|week|month|year)s?\b", title, re.I)
        if match:
            return f"{match.group(1)}_{match.group(2).upper()}"
        return str(market.get("end_at") or "UNKNOWN")

    @staticmethod
    def _action_metric(archetype: MarketArchetype) -> str:
        return {
            MarketArchetype.MENTION_OR_POST_COUNT: "MENTION_COUNT",
            MarketArchetype.EXACT_WEATHER_BUCKET: "OBSERVED_TEMPERATURE",
            MarketArchetype.WEATHER_THRESHOLD: "WEATHER_THRESHOLD",
            MarketArchetype.CENTRAL_BANK_DECISION: "POLICY_RATE_DECISION",
            MarketArchetype.PRICE_DIRECTION_SHORT_WINDOW: "PRICE_DIRECTION",
            MarketArchetype.PRICE_THRESHOLD_TOUCH: "PRICE_TOUCH",
            MarketArchetype.PRICE_THRESHOLD_ENDPOINT: "ENDPOINT_PRICE",
            MarketArchetype.CRYPTO_TOKEN_LAUNCH: "TOKEN_LAUNCH_STATE",
            MarketArchetype.CRYPTO_FDV_AFTER_LAUNCH: "FULLY_DILUTED_VALUATION",
            MarketArchetype.CRYPTO_PUBLIC_SALE: "PUBLIC_SALE_STATE",
            MarketArchetype.CRYPTO_CORPORATE_BTC_ACTION: "CORPORATE_BTC_ACTION",
            MarketArchetype.CRYPTO_REGULATION: "REGULATORY_PROCESS",
            MarketArchetype.CRYPTO_EXCHANGE_LISTING: "EXCHANGE_LISTING_STATE",
            MarketArchetype.CRYPTO_PROTOCOL_EVENT: "PROTOCOL_EVENT_STATE",
        }.get(archetype, archetype.value)


class CapabilityRegistry:
    """Registered domain calculations can produce reviewable analysis, never auto-publish."""

    def resolve(self, classification: CaseClassification) -> CapabilityDecision:
        from smart_money.research.contract_activity_recipes import EVIDENCE_CONTRACTS

        supported = classification.market_archetype in EVIDENCE_CONTRACTS
        return CapabilityDecision(
            capability_id=classification.required_capabilities[0],
            state=CapabilityState.REVIEW_ONLY if supported else CapabilityState.SHADOW,
            evidence_builder=classification.market_archetype.value if supported else None,
            point_in_time_supported=supported,
            probability_mode="QUALITATIVE_STATE",
            reason="registered_analysis_requires_review" if supported else "unsupported_evidence_contract",
            domain_status="PARTIAL" if supported else "INCOMPLETE",
            public_draft_generated=supported,
        )
