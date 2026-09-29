"""Registered crypto fact and price-source adapters for domain evidence."""

from __future__ import annotations

import math
import os
import re
from datetime import datetime, timedelta, timezone
from statistics import pstdev
from typing import TYPE_CHECKING, Any

from smart_money.infrastructure.budget import ResearchBudgetExceeded
from smart_money.infrastructure.sources.config import source_fingerprint
from smart_money.infrastructure.sources.tools import SourcePolicyError, SourceToolRequest, SourceToolRequestError
from smart_money.research.contracts import CaseClassification
from smart_money.research.crypto import candle_coverage, compare_values, crypto_case
from smart_money.research.domain_evidence_common import BINANCE_MARKET_DATA_BASE_URL, _compact, _utc
from smart_money.research.evidence import contract_field_values

if TYPE_CHECKING:
    from smart_money.research.domain_evidence import DomainEvidenceRouter


def _registered_crypto_price_rows(
    self: DomainEvidenceRouter,
    activity_id: str,
    candidate: Any,
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch bounded, rule-selected Coinbase/Binance price data.

    Unsupported resolution providers intentionally return no rows.  The
    Evidence Contract then remains incomplete instead of substituting a
    convenient but non-authoritative exchange.
    """

    if activity_id not in {"CRYPTO_PRICE_WINDOW", "CRYPTO_THRESHOLD_PATH", "CRYPTO_SPOT_SNAPSHOT"}:
        return [], {"status": "not-applicable", "requestCount": 0}
    classification = CaseClassification.model_validate(context["_case_classification"])
    parameters, parameter_refs, pit = contract_field_values(context.get("_evidence_items", []))
    case = crypto_case(classification, {key: value for key, value in parameters.items() if pit.get(key)})
    provider = str(case.resolution_source or "").upper()
    if provider not in {"COINBASE", "BINANCE"}:
        return [], {
            "status": "unsupported-resolution-source" if provider else "resolution-source-missing",
            "requestCount": 0,
            "resolutionSource": case.resolution_source,
        }
    pair = str(case.resolution_pair or "").upper()
    if not re.fullmatch(r"[A-Z0-9]{2,12}/(?:USD|USDT)", pair):
        return [], {"status": "resolution-pair-missing", "requestCount": 0, "resolutionPair": pair}
    source_id = "coinbase_exchange" if provider == "COINBASE" else "binance_spot"
    if source_id not in self.source_tool_registry.sources:
        return [], {
            "status": "unavailable",
            "reason": self.source_tool_registry.source_status.get(source_id, "SOURCE_NOT_REGISTERED"),
            "requestCount": 0,
            "sourceId": source_id,
        }
    request_budget = max(1, min(24, int(os.environ.get("POLYDATA_MAS_CRYPTO_ACTIVITY_REQUEST_BUDGET", "12"))))
    now = self.now().astimezone(timezone.utc)
    cutoff = min(candidate.as_of.astimezone(timezone.utc), now)
    try:
        if activity_id == "CRYPTO_SPOT_SNAPSHOT":
            artifacts, request_count = self._crypto_spot_artifacts(
                source_id,
                pair,
                as_of=cutoff,
            )
            if not artifacts:
                return [], {"status": "empty", "requestCount": request_count}
            artifact, price = artifacts[0]
            fields: dict[str, Any] = {
                "current_price": price,
                "price_source": source_id,
            }
            return [self._crypto_artifact_row([artifact], fields, candidate, source_id, parameters={"pair": pair})], {
                "status": "ok",
                "requestCount": request_count,
                "resolutionSource": provider,
                "resolutionPair": pair,
            }
        start = case.window_start
        end = case.window_end or case.endpoint_time
        if start is None or end is None:
            return [], {"status": "window-missing", "requestCount": 0}
        effective_end = min(end, cutoff)
        if effective_end <= start:
            return [], {"status": "window-not-started", "requestCount": 0}
        threshold, operator = case.threshold, case.comparison_operator
        if activity_id == "CRYPTO_THRESHOLD_PATH" and (threshold is None or operator not in {">", ">=", "<", "<="}):
            return [], {"status": "threshold-or-operator-missing", "requestCount": 0}
        path, artifacts, request_count, errors = self._crypto_candle_path(
            source_id,
            pair,
            start,
            effective_end,
            granularity_seconds=60,
            request_budget=request_budget,
        )
        coverage = candle_coverage(path, start, effective_end)
        complete = coverage == 1.0
        final = complete and effective_end == end
        prices = [point["price"] for point in path]
        returns = [math.log(current / previous) for previous, current in zip(prices, prices[1:])]
        volatility = pstdev(returns) * 100 if len(returns) >= 2 else None
        fields = {}
        if path and activity_id == "CRYPTO_PRICE_WINDOW":
            start_price = path[0]["open"] if _utc(path[0]["at"]) == start else None
            end_price = prices[-1] if final else None
            fields = {
                "start_price": start_price,
                "end_price": end_price,
                "minute_or_finer_path": path,
                "window_high": max(point["high"] for point in path) if complete else None,
                "window_low": min(point["low"] for point in path) if complete else None,
                "realized_window_return": (end_price / start_price - 1) * 100
                if end_price is not None and start_price is not None
                else None,
                "realized_window_volatility": volatility if complete else None,
                "calculation_status": "FINAL_WINDOW" if final else "PARTIAL_WINDOW",
            }
            if case.endpoint_time:
                fields.update(
                    endpoint_price=end_price,
                    volatility_to_endpoint=volatility if complete else None,
                    data_status="FINAL_ENDPOINT" if final else "PARTIAL",
                )
        elif path:
            hits = [
                point
                for point in path
                if threshold is not None
                and compare_values(point["high"] if operator in {">", ">="} else point["low"], threshold, operator)
                is True
            ]
            fields = {
                "current_price": prices[-1],
                "distance_to_threshold": threshold - prices[-1] if threshold is not None else None,
                "window_high": max(point["high"] for point in path) if complete else None,
                "window_low": min(point["low"] for point in path) if complete else None,
                "threshold_hit": True if hits else False if final else None,
                # OHLC establishes an interval, not an exact trade timestamp.
                "threshold_hit_at": {"start": hits[0]["at"], "end": hits[0]["end_at"]}
                if hits
                else "NOT_HIT"
                if final
                else None,
                "minute_path_coverage": candle_coverage(path, start, end),
                "realized_volatility": volatility if complete else None,
                "remaining_time": max(0.0, (end - cutoff).total_seconds()),
            }
        params = {
            "pair": pair,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "cutoff": cutoff.isoformat(),
            "granularity_seconds": 60,
            "threshold": threshold,
            "comparison_operator": operator,
            "rule_evidence_ids": parameter_refs,
        }
        rows = (
            [self._crypto_artifact_row(artifacts, fields, candidate, source_id, parameters=params)] if artifacts else []
        )
        return rows, {
            "status": "ok" if final else "partial" if rows else "empty",
            "requestCount": request_count,
            "requestBudget": request_budget,
            "pathPoints": len(path),
            "coverage": coverage,
            "resolutionSource": provider,
            "resolutionPair": pair,
            "errors": errors,
        }
    except Exception as exc:
        return [], {
            "status": "error",
            "error": _compact(exc, 300),
            "requestCount": int(getattr(exc, "request_count", 0)),
            "resolutionSource": provider,
            "resolutionPair": pair,
        }


def _crypto_spot_artifacts(
    self: DomainEvidenceRouter,
    source_id: str,
    pair: str,
    *,
    as_of: datetime,
) -> tuple[list[tuple[Any, float]], int]:
    if source_id == "coinbase_exchange":
        product = pair.replace("/", "-")
        url = f"https://api.exchange.coinbase.com/products/{product}/ticker"
        params: dict[str, Any] = {}
    else:
        symbol = pair.replace("/", "")
        url = f"{BINANCE_MARKET_DATA_BASE_URL}/api/v3/ticker/price"
        params = {"symbol": symbol}
    result = self.source_tool_registry.execute(
        "fetch.registered",
        SourceToolRequest(
            source_id=source_id,
            url=url,
            params=params,
            as_of=as_of,
        ),
    )
    rows: list[tuple[Any, float]] = []
    for artifact in result.artifacts:
        payload = artifact.structured_payload
        raw_price = payload.get("price") if isinstance(payload, dict) else None
        price = float(raw_price) if raw_price is not None else None
        if price is not None:
            rows.append((artifact, price))
    return rows, result.request_count


def _crypto_candle_path(
    self: DomainEvidenceRouter,
    source_id: str,
    pair: str,
    start: datetime,
    end: datetime,
    *,
    granularity_seconds: int,
    request_budget: int,
) -> tuple[list[dict[str, Any]], list[Any], int, list[str]]:
    cursor = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    points: dict[str, dict[str, Any]] = {}
    artifacts: list[Any] = []
    errors: list[str] = []
    request_count = 0
    while cursor < end and request_count < request_budget:
        if source_id == "coinbase_exchange":
            product = pair.replace("/", "-")
            chunk_end = min(end, cursor + timedelta(seconds=granularity_seconds * 299))
            url = f"https://api.exchange.coinbase.com/products/{product}/candles"
            params: dict[str, Any] = {
                "start": cursor.isoformat(),
                "end": chunk_end.isoformat(),
                "granularity": granularity_seconds,
            }
        else:
            chunk_end = min(end, cursor + timedelta(seconds=granularity_seconds * 999))
            url = f"{BINANCE_MARKET_DATA_BASE_URL}/api/v3/klines"
            params = {
                "symbol": pair.replace("/", ""),
                "interval": "1m" if granularity_seconds == 60 else "1h",
                "startTime": int(cursor.timestamp() * 1000),
                "endTime": int(chunk_end.timestamp() * 1000),
                "limit": 1000,
            }
        try:
            result = self.source_tool_registry.execute(
                "fetch.registered",
                SourceToolRequest(source_id=source_id, url=url, params=params, as_of=end),
            )
        except (SourceToolRequestError, SourcePolicyError, ResearchBudgetExceeded, OSError, ValueError) as exc:
            request_count += int(getattr(exc, "request_count", 0))
            errors.append(type(exc).__name__ + ":" + _compact(exc, 300))
            break
        request_count += result.request_count
        errors.extend(result.errors)
        if not result.artifacts:
            break
        artifact = result.artifacts[0]
        artifacts.append(artifact)
        payload = artifact.structured_payload if isinstance(artifact.structured_payload, list) else []
        for candle in payload:
            if not isinstance(candle, list) or len(candle) < 6:
                continue
            try:
                if source_id == "coinbase_exchange":
                    timestamp, low, high, open_price, close = candle[:5]
                    at = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
                else:
                    timestamp, open_price, high, low, close = candle[:5]
                    at = datetime.fromtimestamp(float(timestamp) / 1000, tz=timezone.utc)
                low, high, open_price, close = map(float, (low, high, open_price, close))
            except (ValueError, TypeError, OverflowError, OSError):
                errors.append("INVALID_CANDLE")
                continue
            closed_at = at + timedelta(seconds=granularity_seconds)
            if start <= at < closed_at <= end:
                point: dict[str, Any] = {
                    "at": at.isoformat(),
                    "end_at": closed_at.isoformat(),
                    "price": float(close),
                    "open": float(open_price),
                    "high": float(high),
                    "low": float(low),
                }
                values = [point[key] for key in ("price", "open", "high", "low")]
                if not all(math.isfinite(value) and value > 0 for value in values):
                    continue
                if (
                    not point["low"]
                    <= min(point["open"], point["price"])
                    <= max(point["open"], point["price"])
                    <= point["high"]
                ):
                    continue
                prior = points.get(at.isoformat())
                if prior is not None and prior != point:
                    # Conflicting pages invalidate this observation, not just the older value.
                    points[at.isoformat()] = {"at": at.isoformat(), "conflict": True}
                elif prior is None:
                    points[at.isoformat()] = point
        cursor = chunk_end
    return (
        sorted((p for p in points.values() if not p.get("conflict")), key=lambda point: point["at"]),
        artifacts,
        request_count,
        errors,
    )


def _crypto_artifact_row(
    self: DomainEvidenceRouter,
    artifacts: list[Any],
    fields: dict[str, Any],
    candidate: Any,
    source_id: str,
    *,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    source = self.source_tool_registry.sources[source_id].definition
    artifact = max(artifacts, key=lambda item: item.retrieved_at)
    return {
        "external_evidence_id": f"crypto_{artifact.artifact_id}",
        "retrieval_method": "REGISTERED_CRYPTO_PRICE_ADAPTER",
        "source_id": source_id,
        "entity_ids": [candidate.market_id],
        "entity_match_score": 1.0,
        "source_name": source_id,
        "source_tier": artifact.source_tier,
        "evidence_kind": "market_data",
        "title": f"Registered crypto price evidence for {candidate.market_id}",
        "url": artifact.canonical_url,
        "published_at": None,
        "retrieved_at": artifact.retrieved_at.isoformat(),
        "temporal_relation": "UNKNOWN",
        "summary": "Rule-selected deterministic crypto price calculation.",
        "raw_data": {
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
            "calculation_parameters": parameters,
        },
        "contract_fields": {key: value for key, value in fields.items() if value is not None},
        "source_metadata": {
            "source_id": source_id,
            "source_artifact_ids": [item.artifact_id for item in artifacts],
            "source_definition": source.model_dump(mode="json") if source else None,
            "source_config_hash": source_fingerprint(source) if source else None,
            "source_content_hash": artifact.content_hash,
            "observed_at": artifact.retrieved_at.isoformat(),
        },
    }
