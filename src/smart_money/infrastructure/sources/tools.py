"""Deterministic, allow-listed source tools for MAS evidence collection.

Direct retrieval reads registered documents and precise JSON adapters.
Domain parsers turn artifacts into contract fields.  A discovery result or raw
artifact is never evidence on its own.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from pydantic import Field

from smart_money.contracts import StrictModel
from smart_money.infrastructure.budget import ResearchBudget
from smart_money.infrastructure.sources import documents
from smart_money.infrastructure.sources.config import ExternalSource, load_external_sources, source_fingerprint
from smart_money.infrastructure.sources.direct_http import (
    MAX_BYTES,
    MAX_CANDIDATES,
    MAX_DOCUMENTS,
    MAX_REDIRECTS,
    MAX_SOURCES,
    RetrievalBudget,
    SourcePolicyError,
    fetch_public,
    public_host,
)

TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


class RegisteredSource(StrictModel):
    source_id: str
    source_tier: Literal["T0", "T1", "T2", "T3", "T4", "T5"]
    allowed_domains: list[str]
    url_prefixes: list[str] = Field(default_factory=list)
    query_param_allowlist: list[str] = Field(default_factory=list)
    definition: ExternalSource | None = None


class SourceToolSpec(StrictModel):
    tool_id: str
    tool_kind: Literal["DISCOVER", "FETCH", "BROWSER", "DOMAIN_API", "LOCAL"]
    allowed_source_ids: list[str]
    output_types: list[Literal["DISCOVERY", "ARTIFACT", "DOMAIN_FACT"]]
    deterministic: bool = True
    requires_llm: bool = False


class SourceToolRequest(StrictModel):
    source_id: str
    url: str | None = None
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    discovered_urls: list[str] = Field(default_factory=list)
    discovery_kind: Literal["feed", "listing", "sitemap"] | None = None
    request_limit: int = Field(default=4, ge=1, le=24)
    as_of: datetime
    max_bytes: int = Field(default=2_000_000, ge=1, le=10_000_000)


class DiscoveryRecord(StrictModel):
    discovery_id: str
    source_id: str
    title: str
    url: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class FetchedArtifact(StrictModel):
    artifact_id: str
    source_id: str
    source_tier: str
    canonical_url: str
    media_type: str
    content_hash: str
    retrieved_at: datetime
    first_seen_at: datetime | None = None
    text: str | None = None
    structured_payload: dict[str, Any] | list[Any] | None = None
    extraction_method: str
    byte_count: int
    document_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DomainFact(StrictModel):
    fact_id: str
    field_name: str
    value: Any
    artifact_id: str
    parser_id: str
    observed_at: datetime | None = None


class SourceToolResult(StrictModel):
    tool_id: str
    source_id: str
    status: Literal["ok", "empty", "error"]
    request_count: int = 0
    discoveries: list[DiscoveryRecord] = Field(default_factory=list)
    artifacts: list[FetchedArtifact] = Field(default_factory=list)
    facts: list[DomainFact] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class SourceTool(Protocol):
    def execute(self, request: SourceToolRequest) -> SourceToolResult: ...


class SourceToolRequestError(RuntimeError):
    """A registered network request failed after consuming request budget."""

    def __init__(self, message: str, *, request_count: int, cause: Exception) -> None:
        super().__init__(message)
        self.request_count = request_count
        self.response = getattr(cause, "response", None)


class SourcePolicy:
    def __init__(self, sources: dict[str, RegisteredSource]) -> None:
        self.sources = sources

    def source(self, source_id: str) -> RegisteredSource:
        try:
            return self.sources[source_id]
        except KeyError as exc:
            raise SourcePolicyError(f"UNREGISTERED_SOURCE:{source_id}") from exc

    def authorize(self, request: SourceToolRequest) -> tuple[RegisteredSource, str]:
        source = self.source(request.source_id)
        if request.url:
            public_host(request.url)
            url = canonicalize_url(request.url)
        elif source.url_prefixes:
            url = canonicalize_url(source.url_prefixes[0])
        else:
            raise SourcePolicyError(f"REGISTERED_SOURCE_URL_REQUIRED:{source.source_id}")
        host = public_host(url)
        parts = urlsplit(url)
        allowed = [domain.lower().lstrip(".").rstrip(".") for domain in source.allowed_domains]
        if host not in allowed:
            raise SourcePolicyError(f"SOURCE_DOMAIN_NOT_ALLOWED:{source.source_id}:{host}")
        if source.url_prefixes and not any(_matches_prefix(url, prefix) for prefix in source.url_prefixes):
            discovered = {canonicalize_url(item) for item in request.discovered_urls}
            if url not in discovered:
                raise SourcePolicyError(f"SOURCE_URL_NOT_REGISTERED_OR_DISCOVERED:{source.source_id}")
        embedded_params = {key for key, _value in parse_qsl(parts.query, keep_blank_values=True)}
        disallowed_params = (set(request.params) | embedded_params) - set(source.query_param_allowlist)
        if disallowed_params:
            raise SourcePolicyError(
                f"SOURCE_QUERY_PARAM_NOT_ALLOWED:{source.source_id}:{','.join(sorted(disallowed_params))}"
            )
        return source, url


class SourceToolRegistry:
    def __init__(self) -> None:
        self.sources: dict[str, RegisteredSource] = {}
        self.source_status: dict[str, str] = {}
        self.specs: dict[str, SourceToolSpec] = {}
        self.tools: dict[str, SourceTool] = {}
        self.catalog: list[ExternalSource] = []
        self.budget: ResearchBudget | None = None
        self.retrieval = RetrievalBudget()

    @property
    def policy(self) -> SourcePolicy:
        return SourcePolicy(self.sources)

    def register_source(self, source: RegisteredSource) -> None:
        if source.source_id in self.sources:
            raise ValueError(f"DUPLICATE_SOURCE:{source.source_id}")
        self.sources[source.source_id] = source

    def register_tool(self, spec: SourceToolSpec, tool: SourceTool) -> None:
        if spec.tool_id in self.tools:
            raise ValueError(f"DUPLICATE_SOURCE_TOOL:{spec.tool_id}")
        unknown = set(spec.allowed_source_ids) - set(self.sources)
        if unknown:
            raise ValueError(f"SOURCE_TOOL_REFERENCES_UNKNOWN_SOURCE:{spec.tool_id}:{','.join(sorted(unknown))}")
        self.specs[spec.tool_id] = spec
        self.tools[spec.tool_id] = tool

    def execute(self, tool_id: str, request: SourceToolRequest) -> SourceToolResult:
        if tool_id not in self.tools:
            raise ValueError(f"UNREGISTERED_SOURCE_TOOL:{tool_id}")
        spec = self.specs[tool_id]
        if request.source_id not in spec.allowed_source_ids:
            raise SourcePolicyError(f"SOURCE_NOT_ALLOWED_FOR_TOOL:{tool_id}:{request.source_id}")
        tool = self.tools[tool_id]
        if isinstance(tool, RegisteredHttpFetchTool):
            tool.budget = self.budget
            tool.retrieval = self.retrieval
        if self.budget and self.budget.limits.max_response_bytes:
            request = request.model_copy(
                update={"max_bytes": min(request.max_bytes, self.budget.limits.max_response_bytes)}
            )
        result = tool.execute(request)
        if self.budget:
            self.budget.check_response(0)
        return result


class RegisteredHttpFetchTool:
    tool_id = "fetch.registered"
    budget: ResearchBudget | None = None

    def __init__(self, policy: SourcePolicy, *, now: Any = None) -> None:
        self.policy = policy
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.retrieval = RetrievalBudget()

    def execute(self, request: SourceToolRequest) -> SourceToolResult:
        source, url = self.policy.authorize(request)
        definition = source.definition
        if definition is None:
            raise SourcePolicyError("SOURCE_DEFINITION_REQUIRED")
        if definition.access.render == "browser":
            raise SourcePolicyError("BROWSER_RENDER_UNAVAILABLE")
        if request.discovery_kind and not any(
            d.kind == request.discovery_kind and canonicalize_url(d.url) == url for d in definition.discovery
        ):
            raise SourcePolicyError("DISCOVERY_ENTRY_NOT_REGISTERED")
        if request.discovery_kind and self.retrieval.candidates >= MAX_CANDIDATES:
            raise SourcePolicyError("RETRIEVAL_CANDIDATE_LIMIT")
        if request.params:
            parts = urlsplit(url)
            url = urlunsplit(parts._replace(query=urlencode([*parse_qsl(parts.query), *request.params.items()])))
        is_document = definition.access.mode == "web" and not request.discovery_kind
        if source.source_id not in self.retrieval.sources and len(self.retrieval.sources) >= MAX_SOURCES:
            raise SourcePolicyError("RETRIEVAL_SOURCE_LIMIT")
        self.retrieval.sources.add(source.source_id)
        if is_document and url not in self.retrieval.documents and len(self.retrieval.documents) >= MAX_DOCUMENTS:
            raise SourcePolicyError("RETRIEVAL_DOCUMENT_LIMIT")
        if is_document:
            self.retrieval.documents.add(url)
        cache_key = f"{source_fingerprint(definition)}|{url}|{request.discovery_kind or 'body'}"
        if value := documents.cached(cache_key):
            # Hashes are verified before reused material is handed to a parser or role.
            for artifact in value.get("artifacts", []):
                if artifact["byte_count"] > min(request.max_bytes, MAX_BYTES):
                    raise SourcePolicyError("SOURCE_RESPONSE_TOO_LARGE")
                if artifact.get("document_ref"):
                    documents.archive().get(artifact["document_ref"])
            result = SourceToolResult.model_validate({**value, "request_count": 0})
            result.discoveries = result.discoveries[: max(0, MAX_CANDIDATES - self.retrieval.candidates)]
            self.retrieval.candidates += len(result.discoveries)
            return result
        request_count = 0
        started = time.monotonic()
        try:
            for redirect_index in range(MAX_REDIRECTS + 1):
                if request_count >= request.request_limit:
                    raise SourcePolicyError("ACTIVITY_REQUEST_LIMIT")
                timeout = min(
                    self.retrieval.reserve(url, self.budget), self.retrieval.remaining() - (time.monotonic() - started)
                )
                if timeout <= 0:
                    raise SourcePolicyError("RETRIEVAL_DEADLINE")
                request_count += 1
                attempt: dict[str, Any] = {"source_id": source.source_id, "url": url}
                self.retrieval.attempts.append(attempt)
                tick = time.monotonic()
                try:
                    response = fetch_public(url, timeout=timeout, max_bytes=min(request.max_bytes, MAX_BYTES))
                    attempt.update(status=response.status, bytes=len(response.body), wire_bytes=response.wire_bytes)
                except Exception as exc:
                    attempt["error"] = str(exc)
                    raise
                finally:
                    attempt["seconds"] = round(time.monotonic() - tick, 3)
                if response.status not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                if not location or redirect_index == MAX_REDIRECTS:
                    raise SourcePolicyError("SOURCE_REDIRECT_LIMIT_EXCEEDED")
                redirected = urljoin(url, location)
                source, url = self.policy.authorize(
                    request.model_copy(
                        update={
                            "url": redirected,
                            "params": {},
                            "discovered_urls": [redirected],
                        }
                    )
                )
            if response.status != 200:
                raise SourcePolicyError(f"SOURCE_HTTP_{response.status}")
            media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            digest = hashlib.sha256(response.body).hexdigest()
            if request.discovery_kind:
                result = self._discover(source, url, response.body, request)
            else:
                document = documents.extract_document(response.body, url, media_type) if is_document else None
                retrieved = self.now().astimezone(timezone.utc)
                artifact = FetchedArtifact(
                    artifact_id=f"artifact_{digest[:24]}",
                    source_id=source.source_id,
                    source_tier=source.source_tier,
                    canonical_url=url,
                    media_type=media_type,
                    content_hash=digest,
                    retrieved_at=retrieved,
                    first_seen_at=documents.first_seen(source.source_id, url, digest, retrieved) if document else None,
                    structured_payload=None if is_document else json.loads(response.body),
                    extraction_method="DIRECT_HTML_V1" if is_document else "REGISTERED_JSON_V1",
                    byte_count=len(response.body),
                    document_ref=documents.archive().put(document) if document else None,
                    metadata=document["metadata"] if document else {},
                )
                result = SourceToolResult(
                    tool_id=self.tool_id, source_id=source.source_id, status="ok", artifacts=[artifact]
                )
            result.request_count = request_count
            if time.monotonic() - started >= self.retrieval.remaining():
                raise SourcePolicyError("RETRIEVAL_DEADLINE")
            if self.budget:
                self.budget.check_response(len(response.body))
            documents.cached(cache_key, value=result.model_dump(mode="json"))
            return result
        except Exception as exc:
            raise SourceToolRequestError(
                f"REGISTERED_SOURCE_REQUEST_FAILED:{source.source_id}:{exc}",
                request_count=request_count,
                cause=exc,
            ) from exc
        finally:
            self.retrieval.seconds_used += time.monotonic() - started

    def _discover(
        self, source: RegisteredSource, url: str, content: bytes, request: SourceToolRequest
    ) -> SourceToolResult:
        discoveries = []
        for row in documents.discover_documents(content, url, request.discovery_kind or "listing"):
            if self.retrieval.candidates >= MAX_CANDIDATES:
                break
            self.retrieval.candidates += 1
            try:
                _, candidate_url = self.policy.authorize(
                    request.model_copy(
                        update={
                            "url": row["url"],
                            "discovered_urls": [row["url"]],
                        }
                    )
                )
            except SourcePolicyError:
                continue
            definition = source.definition
            if urlsplit(candidate_url).path in {"", "/"} or (
                definition and candidate_url in {canonicalize_url(d.url) for d in definition.discovery}
            ):
                continue
            discoveries.append(
                DiscoveryRecord(
                    discovery_id="discovery_" + hashlib.sha256(candidate_url.encode()).hexdigest()[:24],
                    source_id=source.source_id,
                    title=row["title"],
                    url=candidate_url,
                    metadata=row,
                )
            )
        return SourceToolResult(
            tool_id=self.tool_id,
            source_id=source.source_id,
            status="ok" if discoveries else "empty",
            discoveries=discoveries,
            errors=["DISCOVERY_LIMIT_REACHED"] if self.retrieval.candidates >= MAX_CANDIDATES else [],
        )


def build_default_source_tool_registry(
    *,
    now: Any = None,
    source_config_path: str | Path | None = None,
) -> SourceToolRegistry:
    registry = SourceToolRegistry()
    adapter_params = {
        "coinbase_market_data": ["start", "end", "granularity"],
        "binance_market_data": ["symbol", "interval", "startTime", "endTime", "limit"],
    }
    registry.catalog = load_external_sources(source_config_path)
    for source in registry.catalog:
        if not source.enabled:
            registry.source_status[source.id] = "DISABLED"
            continue
        if source.access.mode == "manual":
            registry.source_status[source.id] = "MANUAL_INPUT_REQUIRED"
            continue
        if source.access.mode == "adapter" and source.access.adapter not in adapter_params:
            registry.source_status[source.id] = "ADAPTER_UNAVAILABLE"
            continue
        registry.register_source(
            RegisteredSource(
                source_id=source.id,
                source_tier="T1" if source.role != "newsroom" else "T3",
                allowed_domains=source.domains,
                url_prefixes=[*source.entry_urls, *(d.url for d in source.discovery)],
                query_param_allowlist=adapter_params.get(source.access.adapter or "", []),
                definition=source,
            )
        )
        registry.source_status[source.id] = (
            "BROWSER_RENDER_UNAVAILABLE" if source.access.render == "browser" else "REGISTERED"
        )
    fetch = RegisteredHttpFetchTool(registry.policy, now=now)
    registry.register_tool(
        SourceToolSpec(
            tool_id=fetch.tool_id,
            tool_kind="FETCH",
            allowed_source_ids=list(registry.sources),
            output_types=["ARTIFACT", "DISCOVERY"],
        ),
        fetch,
    )
    return registry


def canonicalize_url(value: str) -> str:
    parts = urlsplit(str(value or "").strip())
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def _matches_prefix(url: str, raw_prefix: str) -> bool:
    target = urlsplit(canonicalize_url(url))
    prefix = urlsplit(canonicalize_url(raw_prefix))
    return (
        target.scheme == prefix.scheme
        and target.netloc == prefix.netloc
        and (target.path == prefix.path or target.path.startswith(prefix.path.rstrip("/") + "/"))
    )
