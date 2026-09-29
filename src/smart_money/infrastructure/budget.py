"""Shared per-research request/deadline guards, including retries and repairs.

Local Qwen has no billing gate. Missing operator limits remain explicit and do
not silently become a production-approved budget.
"""

from __future__ import annotations

import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from smart_money.contracts import StrictModel, parse_utc


class ResearchLimits(StrictModel):
    max_requests: int | None = Field(default=None, ge=1)
    total_seconds: float | None = Field(default=None, gt=0)
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    max_input_chars: int | None = Field(default=None, ge=1)
    max_response_bytes: int | None = Field(default=None, ge=1)


class ResearchBudgetExceeded(RuntimeError):
    pass


class ResearchBudget:
    def __init__(self, limits: dict[str, Any] | None = None, *, previous: dict[str, Any] | None = None) -> None:
        self.limits = ResearchLimits.model_validate(limits or {})
        now = datetime.now(timezone.utc)
        previous = previous or {}
        if previous and previous.get("limits") != self.limits.model_dump():
            raise ValueError("Research retry must preserve its original limits")
        started_at = parse_utc(previous.get("started_at")) if previous else now
        if started_at is None or started_at > now:
            raise ValueError("Research budget start time is missing or in the future")
        self.started_at = started_at
        self.started = time.monotonic() - (now - self.started_at).total_seconds()
        self.requests: list[dict[str, Any]] = deepcopy(previous.get("requests", []))
        self._lock = threading.Lock()

    def reserve(self, purpose: str, timeout: float, *, input_chars: int = 0) -> float:
        with self._lock:
            elapsed = time.monotonic() - self.started
            if self.limits.max_requests is not None and len(self.requests) >= self.limits.max_requests:
                raise ResearchBudgetExceeded("SHARED_REQUEST_LIMIT")
            if self.limits.max_input_chars is not None and input_chars > self.limits.max_input_chars:
                raise ResearchBudgetExceeded("SHARED_CONTEXT_LIMIT")
            if self.limits.total_seconds is not None:
                remaining = self.limits.total_seconds - elapsed
                if remaining <= 0:
                    raise ResearchBudgetExceeded("SHARED_DEADLINE")
                timeout = min(timeout, remaining)
            if self.limits.request_timeout_seconds is not None:
                timeout = min(timeout, self.limits.request_timeout_seconds)
            self.requests.append({"purpose": purpose, "elapsed_seconds": elapsed})
            return timeout

    def snapshot(self) -> dict[str, Any]:
        return {
            "limits": self.limits.model_dump(),
            "started_at": self.started_at.isoformat(),
            "requests_used": len(self.requests),
            "requests": list(self.requests),
            "local_model_billing": "NOT_APPLICABLE",
            "confirmed": all(value is not None for value in self.limits.model_dump().values()),
        }

    def check_response(self, size: int) -> None:
        if self.limits.max_response_bytes is not None and size > self.limits.max_response_bytes:
            raise ResearchBudgetExceeded("SHARED_RESPONSE_LIMIT")
        if self.limits.total_seconds is not None and time.monotonic() - self.started > self.limits.total_seconds:
            raise ResearchBudgetExceeded("SHARED_DEADLINE")
