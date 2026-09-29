"""Request-local cache and source guards for deterministic Evidence Activities."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class ActivityCacheEntry:
    rows: list[dict[str, Any]]
    runtime: dict[str, Any]
    expires_at: datetime


@dataclass(frozen=True)
class ActivityGuardDecision:
    wait_seconds: float = 0
    circuit_open_until: datetime | None = None


@dataclass
class _MemoryGuard:
    last_request_at: datetime | None = None
    consecutive_failures: int = 0
    circuit_open_until: datetime | None = None


class MemoryActivityStateStore:
    """Cache and rate-limit state for one sequential evidence collection run."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], ActivityCacheEntry] = {}
        self._guards: dict[str, _MemoryGuard] = {}
        self._lock = threading.Lock()

    def load_cache(self, activity_id: str, cache_key: str, *, now: datetime) -> ActivityCacheEntry | None:
        with self._lock:
            entry = self._cache.get((activity_id, cache_key))
            if entry is None or entry.expires_at <= now:
                if entry is not None:
                    self._cache.pop((activity_id, cache_key), None)
                return None
            return ActivityCacheEntry(copy.deepcopy(entry.rows), copy.deepcopy(entry.runtime), entry.expires_at)

    def guard(self, activity_id: str, *, now: datetime, min_interval_seconds: float) -> ActivityGuardDecision:
        with self._lock:
            state = self._guards.setdefault(activity_id, _MemoryGuard())
            if state.circuit_open_until and state.circuit_open_until > now:
                return ActivityGuardDecision(circuit_open_until=state.circuit_open_until)
            wait = 0.0
            if state.last_request_at:
                wait = max(0.0, min_interval_seconds - (now - state.last_request_at).total_seconds())
            return ActivityGuardDecision(wait_seconds=wait)

    def save_success(
        self,
        activity_id: str,
        cache_key: str,
        rows: list[dict[str, Any]],
        runtime: dict[str, Any],
        *,
        now: datetime,
        cache_ttl_seconds: int,
    ) -> None:
        with self._lock:
            state = self._guards.setdefault(activity_id, _MemoryGuard())
            state.last_request_at = now
            state.consecutive_failures = 0
            state.circuit_open_until = None
            self._cache[(activity_id, cache_key)] = ActivityCacheEntry(
                copy.deepcopy(rows),
                copy.deepcopy(runtime),
                now + timedelta(seconds=cache_ttl_seconds),
            )

    def save_failure(
        self,
        activity_id: str,
        *,
        now: datetime,
        failure_threshold: int,
        circuit_open_seconds: int,
    ) -> None:
        with self._lock:
            state = self._guards.setdefault(activity_id, _MemoryGuard())
            state.last_request_at = now
            state.consecutive_failures += 1
            if state.consecutive_failures >= failure_threshold:
                state.circuit_open_until = now + timedelta(seconds=circuit_open_seconds)
