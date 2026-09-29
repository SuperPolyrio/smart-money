"""One catalogue-driven path: known documents, then bounded local discovery."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from smart_money.infrastructure.sources import documents
from smart_money.infrastructure.sources.config import match_sources, source_fingerprint
from smart_money.infrastructure.sources.direct_http import MAX_SOURCES
from smart_money.infrastructure.sources.tools import (
    DiscoveryRecord,
    SourcePolicyError,
    SourceToolRegistry,
    SourceToolRequest,
    SourceToolRequestError,
    canonicalize_url,
)
from smart_money.research.domain_source_parsers import DomainSourceParserRegistry


class UniversalEvidenceResearchGateway:
    def __init__(
        self, source_tools: SourceToolRegistry, parsers: DomainSourceParserRegistry, *, now: Any = None
    ) -> None:
        self.source_tools, self.parsers = source_tools, parsers
        self.now = now or (lambda: datetime.now(timezone.utc))

    def collect(
        self,
        *,
        activity_id: str,
        parser_id: str,
        requested_fields: list[str],
        market: dict[str, Any],
        as_of: datetime,
        max_requests: int = 3,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], set[str]]:
        selected, selection = match_sources(self.source_tools.catalog, requested_fields, market)
        selected &= set(self.source_tools.sources)
        selected = {s for s in selected if (d := self.source_tools.sources[s].definition) and d.access.mode == "web"}
        rows: list[dict[str, Any]] = []
        audit: dict[str, Any] = {
            "status": "empty",
            "requestCount": 0,
            "selection": selection,
            "errors": [],
            "documents": [],
            "discovery": [],
            "activityId": activity_id,
        }
        known = self._known_urls(market, selected, as_of)
        pending: list[tuple[str, str, dict[str, Any]]] = [(s, u, {}) for s, u in known]
        seen: set[str] = set()
        for source_id in sorted(selected)[:MAX_SOURCES]:
            if known:
                break
            if any(s == source_id for s, _, _ in pending):
                continue
            source = self.source_tools.sources[source_id].definition
            if not source or not source.discovery:
                audit["errors"].append(f"DISCOVERY_NOT_CONFIGURED:{source_id}")
                continue
            candidates: list[DiscoveryRecord] = []
            for discovery in source.discovery:
                if audit["requestCount"] >= max_requests:
                    break
                try:
                    result = self.source_tools.execute(
                        "fetch.registered",
                        SourceToolRequest(
                            source_id=source_id,
                            url=discovery.url,
                            discovery_kind=discovery.kind,
                            as_of=as_of,
                            request_limit=max_requests - audit["requestCount"],
                        ),
                    )
                    audit["requestCount"] += result.request_count
                    audit["errors"].extend(result.errors)
                    audit["discovery"].append(
                        {
                            "source_id": source_id,
                            "url": discovery.url,
                            "kind": discovery.kind,
                            "candidates": len(result.discoveries),
                        }
                    )
                    candidates.extend(result.discoveries)
                except (SourceToolRequestError, SourcePolicyError, OSError, ValueError) as exc:
                    self._failure(audit, exc)
                if candidates:
                    break
            # Ranking is only a discovery hint, never proof of identity, timing or a claim.
            terms = set(re.findall(r"[\w]{3,}", str(market.get("title") or "").casefold()))
            ranked = sorted(candidates, key=lambda d: -len(terms & set(re.findall(r"[\w]{3,}", d.title.casefold()))))
            pending.extend((d.source_id, d.url, d.metadata) for d in ranked)
        for source_id, url, discovery_meta in pending:
            if url in seen or audit["requestCount"] >= max_requests:
                continue
            seen.add(url)
            detail: dict[str, Any] = {"source_id": source_id, "url": url, "status": "failed"}
            audit["documents"].append(detail)
            try:
                result = self.source_tools.execute(
                    "fetch.registered",
                    SourceToolRequest(
                        source_id=source_id,
                        url=url,
                        discovered_urls=[url],
                        as_of=as_of,
                        request_limit=max_requests - audit["requestCount"],
                    ),
                )
                audit["requestCount"] += result.request_count
                audit["errors"].extend(result.errors)
            except (SourceToolRequestError, SourcePolicyError, OSError, ValueError) as exc:
                self._failure(audit, exc)
                detail["error"] = str(exc)
                continue
            for artifact in result.artifacts:
                body = documents.archive().get(artifact.document_ref) if artifact.document_ref else {}
                # Text remains separate from formal structured fields; the verifier must read it.
                readable = artifact.model_copy(update={"text": body.get("text", artifact.text)})
                parsed = self.parsers.parse(parser_id, readable, market, requested_fields)
                source = self.source_tools.sources[source_id].definition
                times = artifact.metadata.get("times", {})
                published = self._time(times.get("published", []))
                modified = self._time(times.get("modified", []))
                first_seen = artifact.first_seen_at or artifact.retrieved_at
                # Feed dates are preserved as discovery metadata, not relabelled article dates.
                row: dict[str, Any] = {
                    "external_evidence_id": "research_" + artifact.artifact_id,
                    "retrieval_method": artifact.extraction_method,
                    "source_id": source_id,
                    "source_name": source.name if source else source_id,
                    "source_tier": artifact.source_tier,
                    "evidence_kind": "original_material",
                    "title": artifact.metadata.get("title") or parsed.title or url,
                    "url": artifact.canonical_url,
                    "published_at": published,
                    "modified_at": modified,
                    "retrieved_at": artifact.retrieved_at.isoformat(),
                    "first_seen_at": first_seen.isoformat(),
                    "valid_as_of": first_seen.isoformat(),
                    "temporal_relation": "BEFORE_SIGNAL" if first_seen <= as_of else "CURRENT_ONLY",
                    "summary": (readable.text or "")[:1200],
                    "contract_fields": {f.field_name: f.value for f in parsed.facts},
                    "source_metadata": {
                        "source_id": source_id,
                        "source_definition": source.model_dump(mode="json") if source else None,
                        "source_config_hash": source_fingerprint(source) if source else None,
                        "source_content_hash": artifact.content_hash,
                        "document_ref": artifact.document_ref,
                        "document_times": times,
                        "discovery": discovery_meta,
                        "contract_parser_id": parser_id,
                        "contract_parser_status": parsed.status,
                        "domain_facts": [f.model_dump(mode="json") for f in parsed.facts],
                        **parsed.source_metadata,
                    },
                }
                if not artifact.document_ref:
                    row.update(raw_text=artifact.text, raw_data=artifact.structured_payload)
                rows.append(row)
                detail.update(
                    status="extracted",
                    bytes=artifact.byte_count,
                    artifact_id=artifact.artifact_id,
                    fields=list(row["contract_fields"]),
                    missing_fields=parsed.rejected_fields,
                    document_ref=artifact.document_ref,
                )
        audit["status"] = "partial" if audit["errors"] and rows else "ok" if rows else "empty"
        audit["artifactCount"] = len(rows)
        return rows, audit, selected

    @staticmethod
    def _failure(audit: dict[str, Any], exc: Exception) -> None:
        audit["requestCount"] += getattr(exc, "request_count", 0)
        audit["errors"].append(type(exc).__name__ + ":" + str(exc))

    @staticmethod
    def _time(values: list[dict[str, Any]]) -> str | None:
        instants = {v["at"] for v in values if v.get("at")}
        return next(iter(instants)) if len(instants) == 1 else None

    def _known_urls(self, market: dict[str, Any], selected: set[str], as_of: datetime) -> list[tuple[str, str]]:
        urls = re.findall(r'https?://[^\s<>()\[\]{}"\'\\]+', str(market.get("rules_current") or ""))
        urls += [u for q in market.get("rule_questions", []) for u in q.get("urls", [])]
        result = []
        for url in dict.fromkeys(u.rstrip(".,;:!?") for u in urls):
            if urlsplit(url).path in {"", "/"}:
                continue
            for source_id in sorted(selected):
                source = self.source_tools.sources[source_id].definition
                if source and canonicalize_url(url) in {canonicalize_url(d.url) for d in source.discovery}:
                    continue
                try:
                    self.source_tools.policy.authorize(
                        SourceToolRequest(
                            source_id=source_id,
                            url=url,
                            discovered_urls=[url],
                            as_of=as_of,
                        )
                    )
                except SourcePolicyError:
                    continue
                result.append((source_id, url))
                break
        return result
