"""Versioned market taxonomy and deterministic independent-event clusters."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

CLASSIFIER_VERSION = "deterministic-v3"


@dataclass(frozen=True)
class TaxonomyNode:
    sector_id: str
    parent_sector_id: str | None
    display_name: str
    depth: int


@dataclass(frozen=True)
class TaxonomyAssignment:
    sector_id: str
    confidence: float
    method: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class NormalizedMarket:
    condition_id: str
    market_id: str | None
    event_id: str | None
    market_slug: str | None
    event_slug: str | None
    event_title: str | None
    title: str
    description: str | None
    rules: str | None
    end_at: str | None
    closed: bool | None
    resolved: bool | None
    negative_risk: bool | None
    enable_order_book: bool | None
    token_ids: list[str]
    outcomes: list[Any]
    tags: list[str]
    official_category: str | None
    payload: dict[str, Any]


TAXONOMY_NODES = (
    TaxonomyNode("CRYPTO", None, "Crypto", 1),
    TaxonomyNode("CRYPTO.PRICE", "CRYPTO", "Crypto price", 2),
    TaxonomyNode("CRYPTO.PROTOCOL", "CRYPTO", "Crypto protocol", 2),
    TaxonomyNode("CRYPTO.FDV", "CRYPTO", "Token valuation", 2),
    TaxonomyNode("CRYPTO.PUBLIC_SALE", "CRYPTO", "Public token sale", 2),
    TaxonomyNode("POLITICS", None, "Politics", 1),
    TaxonomyNode("POLITICS.US", "POLITICS", "US politics", 2),
    TaxonomyNode("POLITICS.GLOBAL", "POLITICS", "Global politics", 2),
    TaxonomyNode("POLITICS.GEOPOLITICS", "POLITICS", "Geopolitics", 2),
    TaxonomyNode("SPORTS", None, "Sports", 1),
    TaxonomyNode("SPORTS.SOCCER", "SPORTS", "Soccer", 2),
    TaxonomyNode("SPORTS.ESPORTS", "SPORTS", "Esports", 2),
    TaxonomyNode("SPORTS.TENNIS", "SPORTS", "Tennis", 2),
    TaxonomyNode("SPORTS.BASKETBALL", "SPORTS", "Basketball", 2),
    TaxonomyNode("SPORTS.BASEBALL", "SPORTS", "Baseball", 2),
    TaxonomyNode("SPORTS.AMERICAN_FOOTBALL", "SPORTS", "American football", 2),
    TaxonomyNode("SPORTS.OTHER", "SPORTS", "Other sports", 2),
    TaxonomyNode("WEATHER", None, "Weather", 1),
    TaxonomyNode("ECONOMICS", None, "Economics", 1),
    TaxonomyNode("ECONOMICS.FED", "ECONOMICS", "Federal Reserve", 2),
    TaxonomyNode("ECONOMICS.MACRO", "ECONOMICS", "Macroeconomics", 2),
    TaxonomyNode("FINANCE", None, "Finance", 1),
    TaxonomyNode("FINANCE.RATES", "FINANCE", "Rates and bonds", 2),
    TaxonomyNode("FINANCE.EQUITIES", "FINANCE", "Equities", 2),
    TaxonomyNode("FINANCE.COMMODITIES", "FINANCE", "Commodities", 2),
    TaxonomyNode("TECH", None, "Technology", 1),
    TaxonomyNode("TECH.AI", "TECH", "Artificial intelligence", 2),
    TaxonomyNode("TECH.SCIENCE", "TECH", "Science and engineering", 2),
    TaxonomyNode("MENTIONS", None, "Mentions", 1),
    TaxonomyNode("ENTERTAINMENT", None, "Entertainment", 1),
    TaxonomyNode("ENTERTAINMENT.FILM", "ENTERTAINMENT", "Film and television", 2),
    TaxonomyNode("ENTERTAINMENT.MUSIC", "ENTERTAINMENT", "Music", 2),
    TaxonomyNode("ENTERTAINMENT.POP_CULTURE", "ENTERTAINMENT", "Pop culture", 2),
    TaxonomyNode("OTHER", None, "Other", 1),
)

# Ordered profile sectors that can provide evidence for a market sector.
# Exact specialists always rank first; the remaining links are deliberately
# narrow so a generic profitable wallet cannot qualify everywhere.
SECTOR_PROFILE_RELATIONS: dict[str, tuple[str, ...]] = {
    "ECONOMICS.FED": (
        "ECONOMICS.FED",
        "ECONOMICS",
        "FINANCE.RATES",
        "ECONOMICS.MACRO",
        "FINANCE",
    ),
    "ECONOMICS.MACRO": (
        "ECONOMICS.MACRO",
        "ECONOMICS",
        "ECONOMICS.FED",
        "FINANCE.RATES",
        "FINANCE",
    ),
    "FINANCE.RATES": (
        "FINANCE.RATES",
        "FINANCE",
        "ECONOMICS.FED",
        "ECONOMICS",
        "ECONOMICS.MACRO",
    ),
}


def related_profile_sectors(market_sector_id: str) -> tuple[str, ...]:
    configured = SECTOR_PROFILE_RELATIONS.get(market_sector_id)
    if configured is not None:
        return configured
    parent = market_sector_id.split(".", 1)[0]
    return (market_sector_id,) if parent == market_sector_id else (market_sector_id, parent)


def sector_relation_rows() -> list[tuple[str, str, int, str]]:
    rows: list[tuple[str, str, int, str]] = []
    for market_sector in node_ids():
        for rank, profile_sector in enumerate(related_profile_sectors(market_sector)):
            kind = "EXACT" if rank == 0 else "RELATED"
            rows.append((market_sector, profile_sector, rank, kind))
    return rows


def as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    return [value]


def text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def lower_text(*values: Any) -> str:
    return " ".join(text(value) for value in values if value not in (None, "")).lower()


def slug_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text(value).lower()).strip("-")


def phrase_hits(corpus: str, terms: Iterable[str]) -> list[str]:
    """Match short symbols as tokens so ETH does not accidentally match 'both'."""
    hits: list[str] = []
    for term in terms:
        normalized = term.lower()
        if len(normalized) <= 3 or normalized.isalnum() and len(normalized) <= 4:
            pattern = rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])"
            matched = re.search(pattern, corpus) is not None
        else:
            matched = normalized in corpus
        if matched:
            hits.append(term)
    return hits


def bool_or_none(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def first(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_market(payload: Mapping[str, Any]) -> NormalizedMarket | None:
    condition_id = text(first(payload, "conditionId", "condition_id"))
    if not condition_id:
        return None
    events = as_list(payload.get("events"))
    event = next((item for item in events if isinstance(item, dict)), payload.get("event") or {})
    event = event if isinstance(event, dict) else {}
    raw_tags = as_list(first(payload, "tags") or event.get("tags"))
    tags = [text(item.get("slug") if isinstance(item, dict) else item).lower() for item in raw_tags]
    token_ids = [str(item) for item in as_list(first(payload, "clobTokenIds", "clob_token_ids", "tokenIds")) if item]
    title = text(first(payload, "question", "title", "name"))
    if not title:
        return None
    description = text(first(payload, "description")) or None
    # Gamma exposes the settlement text as `description`; older payloads and
    # internal projections may call the same field rules/resolutionCriteria.
    rules = text(first(payload, "rules", "resolutionCriteria", "resolution_criteria")) or description
    return NormalizedMarket(
        condition_id=condition_id.lower(),
        market_id=text(first(payload, "id", "marketId", "market_id")) or None,
        event_id=text(first(payload, "eventId", "event_id") or event.get("id")) or None,
        market_slug=text(first(payload, "slug", "marketSlug", "market_slug")) or None,
        event_slug=text(first(payload, "eventSlug", "event_slug") or event.get("slug")) or None,
        event_title=text(first(payload, "eventTitle", "event_title") or event.get("title")) or None,
        title=title,
        description=description,
        rules=rules,
        end_at=text(first(payload, "endDate", "end_date", "endDateIso")) or None,
        closed=bool_or_none(first(payload, "closed")),
        resolved=bool_or_none(first(payload, "resolved")),
        negative_risk=bool_or_none(first(payload, "negRisk", "negativeRisk", "negative_risk")),
        enable_order_book=bool_or_none(first(payload, "enableOrderBook", "enable_order_book")),
        token_ids=token_ids,
        outcomes=as_list(first(payload, "outcomes", "outcomeNames", "outcome_names")),
        tags=[tag for tag in tags if tag],
        official_category=text(first(payload, "category") or event.get("category")) or None,
        payload=dict(payload),
    )


def classify_market(market: NormalizedMarket) -> TaxonomyAssignment:
    tags = {slug_text(tag) for tag in market.tags if slug_text(tag)}
    category = slug_text(market.official_category)
    event_corpus = lower_text(market.event_title, market.event_slug)
    market_corpus = lower_text(market.title)
    full_corpus = lower_text(event_corpus, market_corpus, market.description, market.rules)

    root_tags: dict[str, set[str]] = {
        "WEATHER": {"weather", "temperature", "daily-temperature", "hurricane", "wildfire"},
        "SPORTS": {
            "sports",
            "games",
            "soccer",
            "football",
            "basketball",
            "baseball",
            "tennis",
            "esports",
            "nba",
            "wnba",
            "nfl",
            "mlb",
            "nhl",
            "ufc",
            "golf",
            "cricket",
            "formula1",
            "fifa",
            "counter-strike-2",
            "league-of-legends",
            "dota-2",
            "valorant",
            "tour-de-france",
        },
        "CRYPTO": {
            "crypto",
            "crypto-prices",
            "bitcoin",
            "ethereum",
            "solana",
            "xrp",
            "ripple",
            "bnb",
            "dogecoin",
            "up-or-down",
            "multi-strikes",
            "tge",
            "airdrops",
        },
        "POLITICS": {
            "politics",
            "elections",
            "global-elections",
            "us-election",
            "geopolitics",
            "trump-presidency",
            "middle-east",
            "government",
            "courts",
            "uk-labour-leadership",
            "congress",
            "primary-elections",
            "midterms",
            "house-primary",
            "republican-primary",
        },
        "ECONOMICS": {
            "economy",
            "economics",
            "macro",
            "macro-indicators",
            "economic-policy",
            "fed",
            "fed-rates",
            "inflation",
            "labor",
            "unemployment",
        },
        "FINANCE": {
            "finance",
            "equities",
            "stocks",
            "earnings",
            "commodities",
            "global-rates",
            "finance-updown",
            "stock-prices",
            "ipo",
            "spy",
            "qqq",
        },
        "TECH": {"tech", "ai", "big-tech", "science", "robot", "technology", "ai-releases"},
        "MENTIONS": {"mention-markets", "tweets-markets", "mentions", "views"},
        "ENTERTAINMENT": {
            "pop-culture",
            "movies",
            "music",
            "awards",
            "tv",
            "reality-tv",
            "celebrities",
            "youtube",
        },
    }
    root_terms: dict[str, tuple[str, ...]] = {
        "WEATHER": ("temperature", "rainfall", "snowfall", "hurricane", "weather", "wildfire"),
        "SPORTS": (
            " vs ",
            " vs. ",
            "nba",
            "wnba",
            "nfl",
            "mlb",
            "nhl",
            "tennis",
            "soccer",
            "basketball",
            "baseball",
            "counter-strike",
            "valorant",
            "dota 2",
            "league of legends",
            "(bo1)",
            "(bo3)",
            "(bo5)",
            "lol:",
            "lol ",
            "both teams to score",
            "premier league",
            "world cup",
            "tour de france",
            "itf ",
            "atp ",
            "wta ",
            "set 1 winner",
            "set handicap",
            "championships, qualification",
            "t20 blast",
            "djokovic",
            "exact score:",
            "map handicap:",
            "game handicap:",
            "games total:",
            "total kills over/under",
            "atp-",
            "wta-",
        ),
        "CRYPTO": (
            "bitcoin",
            "ethereum",
            "solana",
            "crypto",
            "btc",
            "eth",
            "xrp",
            "dogecoin",
            "bnb",
            "hyperliquid",
            "launch a token",
            "token launch",
            "public sale",
        ),
        "POLITICS": (
            "election",
            "prime minister",
            "parliament",
            "senate",
            "congress",
            "white house",
            "president",
            "government",
            "supreme court",
            "secretary of",
            "political party",
            "ceasefire",
            "peace deal",
            "invade",
            "invasion",
            "war",
            "nato",
            "ukraine",
            "russia",
            "israel",
            "iran",
            "taiwan",
            "geopolitic",
            "sanctions",
            "middle east",
            "kharg island",
            "hormuz",
            "enriched uranium",
        ),
        "ECONOMICS": (
            "inflation",
            "gdp",
            "nonfarm",
            "unemployment",
            "recession",
            "cpi",
            "pce",
            "federal reserve",
            "fomc",
            "rate hike",
            "rate cut",
            "interest rate",
        ),
        "FINANCE": (
            "treasury yield",
            "bond yield",
            "stock price",
            "shares",
            "earnings",
            "ipo",
            "nasdaq",
            "s&p",
            "spy",
            "qqq",
            "crude oil",
            "brent oil",
            "wti",
            "natural gas",
            "gold",
            "silver",
            "xauusd",
            "xagusd",
        ),
        "TECH": (
            "openai",
            "chatgpt",
            "anthropic",
            "claude",
            "gemini",
            "grok",
            "xai",
            "ai model",
            "artificial intelligence",
            "microsoft",
            "spacex",
            "data center",
            "robot",
            "technology",
        ),
        "MENTIONS": (
            "mention",
            "mentions",
            "say during",
            "will anyone say",
            "will trump say",
            "tweet",
            "post on x",
            "views on day",
        ),
        "ENTERTAINMENT": (
            "grammy",
            "oscar",
            "album",
            "movie",
            "box office",
            "youtube",
            "subscribers",
            "mrbeast",
        ),
    }

    scores = {root: 0 for root in root_tags}
    matched: dict[str, list[str]] = {root: [] for root in root_tags}
    for root, candidates in root_tags.items():
        tag_hits = sorted(tags & candidates)
        if tag_hits:
            scores[root] += min(8, 4 * len(tag_hits))
            matched[root].extend(tag_hits)
        if category in candidates:
            scores[root] += 6
            matched[root].append(f"category:{category}")
        event_hits = phrase_hits(event_corpus, root_terms[root])
        market_hits = phrase_hits(market_corpus, root_terms[root])
        if event_hits:
            scores[root] += min(8, 4 * len(event_hits))
            matched[root].extend(f"event:{term}" for term in event_hits)
        if market_hits:
            scores[root] += min(6, 3 * len(market_hits))
            matched[root].extend(f"market:{term}" for term in market_hits)

    # A specific pop-culture tag is stronger than the generic `tech` tag used
    # on Google search and social-platform markets.
    if "pop-culture" in tags:
        scores["ENTERTAINMENT"] += 3
        matched["ENTERTAINMENT"].append("specific-tag:pop-culture")

    # Combo score markets often omit all sport tags but retain recognizable leg text.
    if "combo" in tags and (
        re.search(r"\b\d+\s*-\s*\d+\b", market_corpus)
        or " AND " in market.title
        and phrase_hits(market_corpus, root_terms["SPORTS"])
    ):
        scores["SPORTS"] += 5
        matched["SPORTS"].append("combo:sports-structure")

    priority = ("WEATHER", "SPORTS", "CRYPTO", "ECONOMICS", "FINANCE", "TECH", "POLITICS", "MENTIONS", "ENTERTAINMENT")
    root = max(priority, key=lambda item: (scores[item], -priority.index(item)))
    if scores[root] < 3:
        return TaxonomyAssignment(
            "OTHER",
            0.25,
            "RULE",
            {"classifier": CLASSIFIER_VERSION, "matched_terms": [], "root_scores": scores},
        )

    sector = root
    subtype_terms: list[str] = []
    if root == "SPORTS":
        subtypes = {
            "SPORTS.ESPORTS": (
                {"esports", "counter-strike-2", "league-of-legends", "dota-2", "valorant"},
                (
                    "counter-strike",
                    "valorant",
                    "dota 2",
                    "league of legends",
                    "lol:",
                    "lol ",
                    "(bo1)",
                    "(bo3)",
                    "(bo5)",
                ),
            ),
            "SPORTS.SOCCER": (
                {"soccer", "fifa", "fifa-world-cup", "football", "premier-league", "la-liga", "bundesliga"},
                (
                    "soccer",
                    "premier league",
                    "la liga",
                    "bundesliga",
                    "serie a",
                    "champions league",
                    "world cup",
                    "both teams to score",
                    "exact score:",
                ),
            ),
            "SPORTS.TENNIS": (
                {"tennis", "atp", "wta"},
                (
                    "tennis",
                    "open, qualification",
                    "masters:",
                    "itf ",
                    "atp ",
                    "wta ",
                    "set 1 winner",
                    "set handicap",
                    "championships, qualification",
                    "djokovic",
                    " open:",
                    "atp-",
                    "wta-",
                ),
            ),
            "SPORTS.BASKETBALL": ({"basketball", "nba", "wnba", "ncaa-basketball"}, ("nba", "wnba", "basketball")),
            "SPORTS.BASEBALL": ({"baseball", "mlb"}, ("mlb", "baseball")),
            "SPORTS.AMERICAN_FOOTBALL": ({"nfl", "cfb"}, ("nfl", "american football")),
        }
        esports_structure = (
            "map handicap:",
            "game handicap:",
            "games total:",
            "total kills over/under",
        )
        if phrase_hits(full_corpus, esports_structure):
            tags.add("esports")
        subtype_hits = [
            (name, sorted(tags & tag_set) + phrase_hits(full_corpus, terms))
            for name, (tag_set, terms) in subtypes.items()
        ]
        subtype_hits = [(name, hits) for name, hits in subtype_hits if hits]
        if len(subtype_hits) == 1:
            sector, subtype_terms = subtype_hits[0]
        else:
            sector = "SPORTS.OTHER"
            subtype_terms = [name for name, _hits in subtype_hits]
    elif root == "POLITICS":
        geo_terms = (
            "ceasefire",
            "peace deal",
            "invade",
            "invasion",
            "war",
            "nato",
            "ukraine",
            "russia",
            "israel",
            "iran",
            "iranian",
            "kharg island",
            "hormuz",
            "enriched uranium",
            "taiwan",
            "geopolitic",
            "sanctions",
            "middle east",
        )
        us_terms = ("united states", "u.s.", "white house", "congress", "senate", "trump", "biden", "scotus")
        election_tags = {
            "elections",
            "global-elections",
            "us-election",
            "primary-elections",
            "midterms",
            "house-primary",
            "republican-primary",
        }
        if tags & election_tags:
            if tags & {
                "us-election",
                "primary-elections",
                "midterms",
                "house-primary",
                "republican-primary",
            } or phrase_hits(full_corpus, us_terms):
                sector, subtype_terms = "POLITICS.US", phrase_hits(full_corpus, us_terms)
            else:
                sector, subtype_terms = "POLITICS.GLOBAL", sorted(tags & election_tags)
        elif tags & {"geopolitics", "middle-east", "iran", "israel", "ukraine", "russia"} or phrase_hits(
            full_corpus, geo_terms
        ):
            sector, subtype_terms = "POLITICS.GEOPOLITICS", phrase_hits(full_corpus, geo_terms)
        elif tags & {
            "us-election",
            "trump",
            "trump-presidency",
            "congress",
            "primary-elections",
            "midterms",
            "house-primary",
            "republican-primary",
        } or phrase_hits(full_corpus, us_terms):
            sector, subtype_terms = "POLITICS.US", phrase_hits(full_corpus, us_terms)
        else:
            sector = "POLITICS.GLOBAL"
    elif root == "ECONOMICS":
        fed_terms = (
            "federal reserve",
            "the fed",
            "fed rate",
            "fed raise",
            "fed hike",
            "fed cut",
            "fed funds",
            "fomc",
            "rate hike",
            "rate cut",
            "interest rate hike",
            "interest rate cut",
            "powell",
        )
        if tags & {"fed", "fed-rates", "jerome-powell"} or phrase_hits(full_corpus, fed_terms):
            sector, subtype_terms = "ECONOMICS.FED", phrase_hits(full_corpus, fed_terms)
        else:
            sector = "ECONOMICS.MACRO"
    elif root == "FINANCE":
        rate_terms = ("treasury yield", "bond yield", "10-year yield", "2-year yield", "sofr", "global rates")
        commodity_terms = (
            "crude oil",
            "brent oil",
            "wti",
            "natural gas",
            "gold",
            "silver",
            "xauusd",
            "xagusd",
            "commodity",
        )
        equity_terms = ("stock price", "shares", "earnings", "ipo", "nasdaq", "s&p", "spy", "qqq", "equities")
        if tags & {"global-rates", "rates"} or phrase_hits(full_corpus, rate_terms):
            sector, subtype_terms = "FINANCE.RATES", phrase_hits(full_corpus, rate_terms)
        elif tags & {"commodities", "oil", "gold", "silver"} or phrase_hits(full_corpus, commodity_terms):
            sector, subtype_terms = "FINANCE.COMMODITIES", phrase_hits(full_corpus, commodity_terms)
        elif tags & {"equities", "stocks", "stock-prices", "earnings"} or phrase_hits(full_corpus, equity_terms):
            sector, subtype_terms = "FINANCE.EQUITIES", phrase_hits(full_corpus, equity_terms)
    elif root == "CRYPTO":
        if phrase_hits(full_corpus, ("fdv", "fully diluted", "market cap at launch")):
            sector = "CRYPTO.FDV"
        elif phrase_hits(
            full_corpus, ("public sale", "token sale", "launch a token", "token launch", "ico", "launchpad")
        ):
            sector = "CRYPTO.PUBLIC_SALE"
        elif tags & {"crypto-prices", "up-or-down", "multi-strikes"} or phrase_hits(
            market_corpus, ("up or down", "above", "below", "price", "5m", "15m", "1h")
        ):
            sector = "CRYPTO.PRICE"
        else:
            sector = "CRYPTO.PROTOCOL"
    elif root == "TECH":
        if tags & {"ai", "ai-releases"} or phrase_hits(
            full_corpus,
            (
                "openai",
                "chatgpt",
                "anthropic",
                "claude",
                "gemini",
                "grok",
                "xai",
                "ai model",
                "artificial intelligence",
            ),
        ):
            sector = "TECH.AI"
        elif tags & {"science", "robot", "space", "spacex"} or phrase_hits(
            full_corpus, ("robot", "spaceflight", "spacex", "starship", "scientific")
        ):
            sector = "TECH.SCIENCE"
    elif root == "ENTERTAINMENT":
        if tags & {"movies", "tv", "reality-tv"} or phrase_hits(full_corpus, ("movie", "box office", "oscar")):
            sector = "ENTERTAINMENT.FILM"
        elif tags & {"music", "awards"} or phrase_hits(full_corpus, ("album", "song", "grammy")):
            sector = "ENTERTAINMENT.MUSIC"
        else:
            sector = "ENTERTAINMENT.POP_CULTURE"

    confidence = min(0.98, 0.58 + 0.035 * scores[root])
    return TaxonomyAssignment(
        sector,
        confidence,
        "RULE",
        {
            "classifier": CLASSIFIER_VERSION,
            "matched_terms": sorted(set(matched[root] + subtype_terms)),
            "root_scores": scores,
            "event_context_used": bool(market.event_title or market.event_slug),
        },
    )


def cluster_identity(market: NormalizedMarket, taxonomy_version: str) -> tuple[str, str]:
    canonical = market.event_id or market.event_slug or market.market_slug or market.condition_id
    canonical = re.sub(r"[^a-z0-9]+", "-", canonical.lower()).strip("-")
    digest = hashlib.sha256(f"{taxonomy_version}|{canonical}".encode()).hexdigest()[:24]
    return f"event-{digest}", canonical


def node_ids(nodes: Iterable[TaxonomyNode] = TAXONOMY_NODES) -> set[str]:
    return {node.sector_id for node in nodes}
