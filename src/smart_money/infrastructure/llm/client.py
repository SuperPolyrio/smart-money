"""The sole model transport: the locally served Qwen3.8-27B JSON endpoint."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

from smart_money.infrastructure.budget import ResearchBudget, ResearchBudgetExceeded


@dataclass(frozen=True)
class CompletionUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    runtime: str = "local-qwen"


class LocalQwenClient:
    model = "Qwen3.8-27B"

    def __init__(self, *, api_base: str | None = None, timeout: float | None = None) -> None:
        self.api_base = (api_base or os.environ.get("SMART_MONEY_QWEN_BASE_URL", "http://127.0.0.1:30000/v1")).rstrip(
            "/"
        )
        parts = urlsplit(self.api_base)
        try:
            local = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname or "").is_loopback
        except ValueError:
            local = False
        if (
            not local
            or parts.scheme not in {"http", "https"}
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Qwen endpoint must be a local loopback service or SSH tunnel")
        self.timeout = timeout or float(os.environ.get("SMART_MONEY_QWEN_TIMEOUT_SECONDS", "45"))
        self.autostart = os.environ.get("SMART_MONEY_QWEN_AUTOSTART", "0") == "1"
        if self.autostart and self.api_base not in {"http://127.0.0.1:30000/v1", "http://localhost:30000/v1"}:
            raise ValueError("Qwen autostart requires the managed local endpoint on port 30000")
        self.last_usage = CompletionUsage(self.model)
        self.budget: ResearchBudget | None = None

    @property
    def configured(self) -> bool:
        return True

    def _ensure_ready(self, session: requests.Session) -> None:
        if not self.autostart:
            return
        deadline = time.monotonic() + float(os.environ.get("SMART_MONEY_QWEN_STARTUP_SECONDS", "600"))
        if self.budget and self.budget.limits.total_seconds is not None:
            deadline = min(deadline, self.budget.started + self.budget.limits.total_seconds)
        started = False
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                response = session.get(self.api_base + "/models", timeout=min(2, remaining), allow_redirects=False)
                if response.status_code == 200:
                    if self.model not in {item.get("id") for item in response.json().get("data", [])}:
                        raise RuntimeError("Local Qwen service returned a different model")
                    return
                if response.status_code != 503:
                    raise RuntimeError(f"Local Qwen readiness failed: HTTP {response.status_code}")
            except (requests.ConnectionError, requests.Timeout):
                pass
            if not started:
                try:
                    subprocess.run(
                        ["systemctl", "--user", "start", "smart-money-qwen.service"],
                        check=True,
                        capture_output=True,
                        timeout=min(10, remaining),
                    )
                except subprocess.SubprocessError as exc:
                    raise requests.ConnectionError(
                        "Local Qwen startup failed; inspect smart-money-qwen.service logs"
                    ) from exc
                started = True
            state = subprocess.run(
                ["systemctl", "--user", "show", "smart-money-qwen.service", "-p", "ActiveState", "--value"],
                check=True,
                capture_output=True,
                text=True,
                timeout=min(5, remaining),
            ).stdout.strip()
            if state not in {"active", "activating"}:
                raise requests.ConnectionError(
                    f"Local Qwen startup failed ({state}); inspect smart-money-qwen.service logs"
                )
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        if self.budget and self.budget.limits.total_seconds is not None:
            if time.monotonic() >= self.budget.started + self.budget.limits.total_seconds:
                raise ResearchBudgetExceeded("SHARED_DEADLINE")
        raise requests.Timeout("Local Qwen readiness deadline exceeded")

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 1200,
        workflow_name: str = "smart-money-analysis",
        response_schema: dict[str, Any] | None = None,
        structured_mode: str = "json_schema",
    ) -> str:
        limit = int(os.environ.get("SMART_MONEY_QWEN_INPUT_MAX_CHARS", "32000"))
        if sum(len(item.get("content", "")) for item in messages) > limit:
            raise ResearchBudgetExceeded("Model input exceeds context limit")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if structured_mode == "json_schema" and response_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": re.sub(r"[^A-Za-z0-9_-]", "_", workflow_name)[:64],
                    "strict": True,
                    "schema": response_schema,
                },
            }
        else:
            body["response_format"] = {"type": "json_object"}
        with requests.Session() as session:
            session.trust_env = False
            self._ensure_ready(session)
            timeout = (
                self.budget.reserve(workflow_name, self.timeout, input_chars=sum(len(m["content"]) for m in messages))
                if self.budget
                else self.timeout
            )
            response = session.post(
                self.api_base + "/chat/completions", json=body, timeout=timeout, allow_redirects=False
            )
        if 300 <= response.status_code < 400:
            raise RuntimeError("Local Qwen redirects are forbidden")
        response.raise_for_status()
        if self.budget:
            self.budget.check_response(len(response.content))
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("Local Qwen returned no choices")
        if choices[0].get("finish_reason") == "length":
            raise ValueError("Local Qwen output was truncated")
        content = (choices[0].get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Local Qwen returned empty content")
        usage = payload.get("usage") or {}
        self.last_usage = CompletionUsage(
            self.model, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        )
        return content
