"""Deterministic, budgeted Evidence Contract gap resolution."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from pydantic import Field

from smart_money.contracts import StrictModel
from smart_money.research.activity_state import MemoryActivityStateStore
from smart_money.research.contract_activity_recipes import CONTRACT_ACTIVITY_RECIPES, FIELD_ACTIVITY_BINDINGS
from smart_money.research.contracts import CaseClassification, EvidenceContractResult


@dataclass(frozen=True)
class ContractActivitySpec:
    activity_id: str
    budget_cost: int
    cache_ttl_seconds: int = 300
    min_interval_seconds: float = 1.0
    max_attempts: int = 2
    circuit_failure_threshold: int = 3
    circuit_open_seconds: int = 300


ACTIVITY_SPECS: dict[str, ContractActivitySpec] = {
    "RULES_RESOLUTION_SOURCE": ContractActivitySpec("RULES_RESOLUTION_SOURCE", 0, min_interval_seconds=0),
    "RELATED_MARKET_CONTEXT": ContractActivitySpec("RELATED_MARKET_CONTEXT", 0, min_interval_seconds=0),
    "MACRO_BLS_RELEASES": ContractActivitySpec("MACRO_BLS_RELEASES", 2, 1800, 2),
    "WEATHER_REGISTERED_DATA": ContractActivitySpec("WEATHER_REGISTERED_DATA", 1, 300, 2),
    "MENTIONS_REGISTERED_DATA": ContractActivitySpec("MENTIONS_REGISTERED_DATA", 1, 300, 2),
}
ACTIVITY_SPECS.update(
    {
        activity_id: ContractActivitySpec(
            activity_id=activity_id,
            budget_cost=recipe.budget_cost,
            cache_ttl_seconds=recipe.cache_ttl_seconds,
            min_interval_seconds=recipe.min_interval_seconds,
            max_attempts=recipe.max_attempts,
        )
        for activity_id, recipe in CONTRACT_ACTIVITY_RECIPES.items()
    }
)


class ActivityExecutor(Protocol):
    def __call__(
        self,
        activity_id: str,
        requested_fields: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]: ...


class ContractEvaluator(Protocol):
    def __call__(self, additional_rows: list[dict[str, Any]]) -> tuple[EvidenceContractResult, list[str]]: ...


class ActivityRun(StrictModel):
    activity_run_id: str | None = None
    activity_id: str
    requested_fields: list[str]
    budget_cost: int
    status: str
    attempts: int
    cache_hit: bool
    evidence_ids: list[str]
    request_count: int = 0
    field_audit: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class GapResolution(StrictModel):
    initial_missing_fields: list[str]
    final_contract: EvidenceContractResult
    additional_rows: list[dict[str, Any]]
    activity_runs: list[ActivityRun]
    search_budget_used: int
    search_budget_limit: int
    stopped_reason: str


class ActivityGuardError(RuntimeError):
    budget_charged = False
    attempts = 0


class ActivityExecutionError(RuntimeError):
    budget_charged = True

    def __init__(self, message: str, *, attempts: int, request_count: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.request_count = request_count


class ActivityReportedError(RuntimeError):
    """A registered executor completed transport handling but reported source failure."""


class GuardedActivityRunner:
    """Adds cache, rate limiting, Retry-After retries, and a circuit breaker."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        state_store: MemoryActivityStateStore | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now or (
            (lambda: datetime.now(timezone.utc))
            if monotonic is time.monotonic
            else (lambda: datetime.fromtimestamp(self.monotonic(), tz=timezone.utc))
        )
        self.state_store = state_store or MemoryActivityStateStore()

    def run(
        self,
        spec: ContractActivitySpec,
        cache_key: str,
        requested_fields: list[str],
        executor: ActivityExecutor,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], int, bool]:
        field_key = ",".join(sorted(set(requested_fields)))
        activity_cache_key = f"{cache_key}:{field_key}"
        cached = self.state_store.load_cache(spec.activity_id, activity_cache_key, now=self.now())
        if cached:
            return cached.rows, cached.runtime, 0, True
        guard = self.state_store.guard(
            spec.activity_id,
            now=self.now(),
            min_interval_seconds=spec.min_interval_seconds,
        )
        if guard.circuit_open_until:
            raise ActivityGuardError(f"ACTIVITY_CIRCUIT_OPEN:{spec.activity_id}:{guard.circuit_open_until.isoformat()}")
        if guard.wait_seconds > 0:
            self.sleep(guard.wait_seconds)

        last_error: Exception | None = None
        request_count = 0
        for attempt in range(1, spec.max_attempts + 1):
            try:
                rows, runtime = executor(spec.activity_id, requested_fields)
                request_count += int(runtime.get("requestCount") or 0)
                if _runtime_failed(runtime):
                    error_detail = runtime.get("error") or runtime.get("errors") or ""
                    raise ActivityReportedError(f"SOURCE_REPORTED_FAILURE:{runtime.get('status')}:{error_detail}")
                runtime = {**runtime, "_activityRequestCount": request_count}
                self.state_store.save_success(
                    spec.activity_id,
                    activity_cache_key,
                    rows,
                    runtime,
                    now=self.now(),
                    cache_ttl_seconds=spec.cache_ttl_seconds,
                )
                return rows, runtime, attempt, False
            except Exception as exc:
                last_error = exc
                request_count += int(getattr(exc, "request_count", 0))
                self.state_store.save_failure(
                    spec.activity_id,
                    now=self.now(),
                    failure_threshold=spec.circuit_failure_threshold,
                    circuit_open_seconds=spec.circuit_open_seconds,
                )
                if attempt < spec.max_attempts:
                    self.sleep(_retry_after_seconds(exc, attempt))
        raise ActivityExecutionError(
            f"ACTIVITY_FAILED:{spec.activity_id}:{last_error}",
            attempts=spec.max_attempts,
            request_count=request_count,
        ) from last_error


class ContractGapResolver:
    def __init__(self, runner: GuardedActivityRunner | None = None) -> None:
        self.runner = runner or GuardedActivityRunner()

    def resolve(
        self,
        classification: CaseClassification,
        initial_contract: EvidenceContractResult,
        *,
        cache_key: str,
        search_budget_limit: int,
        executor: ActivityExecutor,
        evaluator: ContractEvaluator,
    ) -> GapResolution:
        additional_rows: list[dict[str, Any]] = []
        runs: list[ActivityRun] = []
        budget_used = 0
        contract = initial_contract
        bindings = FIELD_ACTIVITY_BINDINGS.get(classification.market_archetype, {})
        planned: list[tuple[str, list[str]]] = []
        unresolved_fields = list(dict.fromkeys([*contract.missing_fields, *contract.pit_missing_fields]))
        for field in unresolved_fields:
            for activity_id in bindings.get(field, ()):
                existing = next((row for row in planned if row[0] == activity_id), None)
                if existing:
                    existing[1].append(field)
                else:
                    planned.append((activity_id, [field]))

        stopped_reason = "CONTRACT_ALREADY_COMPLETE" if contract.passed else "NO_REGISTERED_ACTIVITY"
        known_evidence_ids: set[str] = set()
        for activity_id, fields in planned:
            spec = ACTIVITY_SPECS[activity_id]
            requested_fields = list(dict.fromkeys(fields))
            if budget_used + spec.budget_cost > search_budget_limit:
                stopped_reason = "SEARCH_BUDGET_EXHAUSTED"
                continue
            activity_run_id = f"ear_{uuid4().hex}"
            budget_before = budget_used
            try:
                rows, activity_runtime, attempts, cache_hit = self.runner.run(
                    spec,
                    cache_key,
                    requested_fields,
                    executor,
                )
                candidate_rows = [*additional_rows, *rows]
                candidate_contract, evidence_ids = evaluator(candidate_rows)
                introduced = sorted(set(evidence_ids) - known_evidence_ids)
                charged = 0 if cache_hit else spec.budget_cost
                run = ActivityRun(
                    activity_run_id=activity_run_id,
                    activity_id=activity_id,
                    requested_fields=requested_fields,
                    budget_cost=charged,
                    status="EVIDENCE_ADDED" if introduced else "NO_MATCHING_EVIDENCE",
                    attempts=attempts,
                    cache_hit=cache_hit,
                    evidence_ids=introduced,
                    request_count=(
                        int(activity_runtime.get("_activityRequestCount") or activity_runtime.get("requestCount") or 0)
                        if not cache_hit
                        else 0
                    ),
                    field_audit=dict(activity_runtime.get("fieldAudit") or {}),
                )
                budget_after = budget_before + charged
                additional_rows = candidate_rows
                contract = candidate_contract
                known_evidence_ids.update(evidence_ids)
                budget_used = budget_after
                runs.append(run)
            except Exception as exc:
                charged = spec.budget_cost if getattr(exc, "budget_charged", True) else 0
                run = ActivityRun(
                    activity_run_id=activity_run_id,
                    activity_id=activity_id,
                    requested_fields=requested_fields,
                    budget_cost=charged,
                    status="FAILED",
                    attempts=int(getattr(exc, "attempts", spec.max_attempts)),
                    cache_hit=False,
                    evidence_ids=[],
                    request_count=int(getattr(exc, "request_count", 0)),
                    field_audit={},
                    error=str(exc)[:300],
                )
                budget_after = budget_before + charged
                budget_used = budget_after
                runs.append(run)
            if contract.passed:
                stopped_reason = "CONTRACT_COMPLETE"
                break
            stopped_reason = "SEARCH_BUDGET_EXHAUSTED" if budget_used >= search_budget_limit else "CONTRACT_INCOMPLETE"
        return GapResolution(
            initial_missing_fields=list(
                dict.fromkeys([*initial_contract.missing_fields, *initial_contract.pit_missing_fields])
            ),
            final_contract=contract,
            additional_rows=additional_rows,
            activity_runs=runs,
            search_budget_used=budget_used,
            search_budget_limit=search_budget_limit,
            stopped_reason=stopped_reason,
        )


def _retry_after_seconds(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) if response is not None else {}
    raw = headers.get("Retry-After") or headers.get("retry-after") if headers else None
    if raw:
        try:
            return min(60.0, max(0.0, float(raw)))
        except (TypeError, ValueError):
            pass
    return min(8.0, float(2 ** (attempt - 1)))


def _runtime_failed(runtime: dict[str, Any]) -> bool:
    return str(runtime.get("status") or "").strip().lower() in {
        "error",
        "failed",
        "unavailable",
    }
