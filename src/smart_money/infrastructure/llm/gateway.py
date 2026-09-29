"""One local Qwen invocation path; no remote providers or model failover."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import requests

from smart_money.infrastructure.llm.client import LocalQwenClient


class ExecutionMode(StrEnum):
    FULL_LLM = "FULL_LLM"
    PARTIAL_LLM = "PARTIAL_LLM"
    DETERMINISTIC_ONLY = "DETERMINISTIC_ONLY"


@dataclass(frozen=True)
class GatewayCompletion:
    content: str
    provider: str
    model: str
    runtime: str
    execution_mode: ExecutionMode
    input_tokens: int = 0
    output_tokens: int = 0
    request_hash: str = ""
    response_hash: str = ""
    attempts: int = 1
    structured_mode: str = "json_schema"


class LLMGateway:
    def __init__(self, client: LocalQwenClient | None = None) -> None:
        self.client = client or LocalQwenClient()
        self._lock = threading.Lock()
        self.max_attempts = max(1, int(os.environ.get("SMART_MONEY_QWEN_MAX_ATTEMPTS", "3")))

    @property
    def configured(self) -> bool:
        return self.client.configured

    def generate_sync(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 1200,
        workflow_name: str,
        response_schema: dict[str, Any] | None = None,
        structured_mode: str = "json_schema",
    ) -> GatewayCompletion:
        # The local service is shared; serialize this engine's calls and retain transport retries only.
        with self._lock:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    content = self.client.complete_json(
                        messages,
                        max_tokens=max_tokens,
                        workflow_name=workflow_name,
                        response_schema=response_schema,
                        structured_mode=structured_mode,
                    )
                    usage = self.client.last_usage
                    return GatewayCompletion(
                        content,
                        "local-qwen",
                        self.client.model,
                        usage.runtime,
                        ExecutionMode.FULL_LLM,
                        usage.input_tokens,
                        usage.output_tokens,
                        payload_hash(messages),
                        payload_hash(content),
                        attempt,
                        structured_mode,
                    )
                except requests.RequestException as exc:
                    status = getattr(exc.response, "status_code", None)
                    if attempt == self.max_attempts or (status is not None and status < 500 and status != 429):
                        raise
                    time.sleep(min(2.0, 0.5 * 2 ** (attempt - 1)))
        raise RuntimeError("Local Qwen request not executed")


def aggregate_execution_mode(agent_runtime: dict[str, Any]) -> ExecutionMode:
    stages = list((agent_runtime.get("agents") or {}).values())
    executed = [s for s in stages if s.get("source") == "llm" and s.get("status") == "SUCCESS"]
    if not executed:
        return ExecutionMode.DETERMINISTIC_ONLY
    incomplete = any(s.get("source") != "deterministic-summary" and s.get("status") != "SUCCESS" for s in stages)
    return ExecutionMode.PARTIAL_LLM if incomplete else ExecutionMode.FULL_LLM


def payload_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()
