"""Extract replay-safe Polymarket classification metadata from persisted market data."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pydantic import Field

from smart_money.contracts import StrictModel

__all__ = [
    "OfficialCategoryRef",
    "OfficialClassification",
    "OfficialClassificationExtractor",
    "OfficialTagRef",
    "SportsMetadataRef",
]


class OfficialCategoryRef(StrictModel):
    id: str | None = None
    slug: str
    label: str | None = None
    parent_category: str | None = None


class OfficialTagRef(StrictModel):
    id: str | None = None
    slug: str
    label: str | None = None


class SportsMetadataRef(StrictModel):
    sport: str | None = None
    tag_ids: list[str] = Field(default_factory=list)
    series: str | None = None
    resolution: str | None = None
    ordering: str | None = None


class OfficialClassification(StrictModel):
    categories: list[OfficialCategoryRef] = Field(default_factory=list)
    tags: list[OfficialTagRef] = Field(default_factory=list)
    sports: list[SportsMetadataRef] = Field(default_factory=list)


def _slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text)
    return text.strip("-")


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _objects(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _nested_payloads(market: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield market
    gamma = market.get("gamma_payload")
    if isinstance(gamma, dict):
        yield gamma
        for event in _objects(gamma.get("events")):
            if isinstance(event, dict):
                yield event
    for event in _objects(market.get("events")):
        if isinstance(event, dict):
            yield event


class OfficialClassificationExtractor:
    """Normalize categories, tags and optional /sports metadata without live I/O."""

    def extract(
        self,
        market: dict[str, Any],
        *,
        embedded_market: dict[str, Any] | None = None,
    ) -> OfficialClassification:
        sources = [market]
        if embedded_market:
            sources.append(embedded_market)

        categories: list[OfficialCategoryRef] = []
        tags: list[OfficialTagRef] = []
        sports: list[SportsMetadataRef] = []
        for source in sources:
            for payload in _nested_payloads(source):
                categories.extend(self._categories(payload.get("categories")))
                tags.extend(self._tags(payload.get("tags")))
                sports.extend(
                    self._sports(
                        payload.get("sports_metadata") or payload.get("sportsMetadata") or payload.get("sports")
                    )
                )
        return OfficialClassification(
            categories=self._dedupe(categories),
            tags=self._dedupe(tags),
            sports=self._dedupe(sports),
        )

    @staticmethod
    def _categories(value: Any) -> list[OfficialCategoryRef]:
        rows = []
        for item in _objects(value):
            if isinstance(item, str):
                slug = _slug(item)
                if slug:
                    rows.append(OfficialCategoryRef(slug=slug, label=_text(item)))
                continue
            if not isinstance(item, dict):
                continue
            parent = item.get("parentCategory") or item.get("parent_category")
            if isinstance(parent, dict):
                parent = parent.get("slug") or parent.get("label") or parent.get("id")
            slug = _slug(item.get("slug") or item.get("label") or item.get("name"))
            if slug:
                rows.append(
                    OfficialCategoryRef(
                        id=_text(item.get("id")),
                        slug=slug,
                        label=_text(item.get("label") or item.get("name")),
                        parent_category=_slug(parent) or None,
                    )
                )
        return rows

    @staticmethod
    def _tags(value: Any) -> list[OfficialTagRef]:
        rows = []
        for item in _objects(value):
            if isinstance(item, str):
                slug = _slug(item)
                if slug:
                    rows.append(OfficialTagRef(slug=slug, label=_text(item)))
                continue
            if not isinstance(item, dict):
                continue
            slug = _slug(item.get("slug") or item.get("label") or item.get("name"))
            if slug:
                rows.append(
                    OfficialTagRef(
                        id=_text(item.get("id")),
                        slug=slug,
                        label=_text(item.get("label") or item.get("name")),
                    )
                )
        return rows

    @staticmethod
    def _sports(value: Any) -> list[SportsMetadataRef]:
        items = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_ids = item.get("tags") or item.get("tag_ids") or item.get("tagIds") or []
            if isinstance(raw_ids, str):
                raw_ids = [part.strip() for part in raw_ids.split(",")]
            tag_ids = [str(part).strip() for part in raw_ids if str(part).strip()]
            if any(
                item.get(key) is not None
                for key in ("sport", "tags", "tag_ids", "tagIds", "series", "resolution", "ordering")
            ):
                rows.append(
                    SportsMetadataRef(
                        sport=_slug(item.get("sport")) or None,
                        tag_ids=tag_ids,
                        series=_text(item.get("series")),
                        resolution=_text(item.get("resolution")),
                        ordering=_text(item.get("ordering")),
                    )
                )
        return rows

    @staticmethod
    def _dedupe(rows: list[Any]) -> list[Any]:
        output = []
        seen = set()
        for row in rows:
            key = row.model_dump_json(exclude_none=True)
            if key not in seen:
                seen.add(key)
                output.append(row)
        return output
