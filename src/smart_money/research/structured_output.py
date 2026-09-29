"""Schema-constrained LLM output with auditable, bounded repair attempts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import requests
from pydantic import BaseModel, ValidationError

from smart_money.infrastructure.budget import ResearchBudgetExceeded
from smart_money.infrastructure.llm.gateway import GatewayCompletion, LLMGateway, payload_hash
from smart_money.infrastructure.llm.json_utils import extract_and_repair_json_object

T = TypeVar("T", bound=BaseModel)


def _compact(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class StructuredRun(Generic[T]):  # noqa: UP046 - production runtime remains Python 3.10 compatible.
    value: T
    audit: dict[str, Any]
    completion: GatewayCompletion | None


class StructuredOutputRunner:
    """Run one schema request plus at most one evidence-free repair."""

    def __init__(self, gateway: LLMGateway, *, max_repairs: int = 1) -> None:
        self.gateway = gateway
        self.max_repairs = max(0, min(1, max_repairs))

    def run(
        self,
        messages: list[dict[str, str]],
        *,
        model: type[T],
        fallback: T,
        workflow_name: str,
        max_tokens: int = 1200,
        response_schema: dict[str, Any] | None = None,
    ) -> StructuredRun[T]:
        schema = response_schema if response_schema is not None else model.model_json_schema()
        modes = ["json_schema", "json_object"]
        attempts: list[dict[str, Any]] = []
        validation_errors: list[str] = []
        raw_output: str | None = None
        last_completion: GatewayCompletion | None = None
        total_input_tokens = 0
        total_output_tokens = 0
        local_repairs: list[str] = []
        for index in range(1, self.max_repairs + 2):
            mode = modes[min(index - 1, len(modes) - 1)]
            request_messages = (
                messages if raw_output is None else self._repair_messages(raw_output, schema, validation_errors[-1])
            )
            attempt: dict[str, Any] = {
                "attempt": index,
                "structured_mode": mode,
                "request_hash": payload_hash(request_messages),
                "repair_request": raw_output is not None,
            }
            try:
                completion = self.gateway.generate_sync(
                    request_messages,
                    max_tokens=max_tokens,
                    workflow_name=(workflow_name if index == 1 else f"{workflow_name}-repair-{index - 1}"),
                    response_schema=schema,
                    structured_mode=mode,
                )
                last_completion = completion
                raw_output = completion.content
                total_input_tokens += completion.input_tokens
                total_output_tokens += completion.output_tokens
                attempt.update(
                    {
                        "provider": completion.provider,
                        "model": completion.model,
                        "runtime": completion.runtime,
                        "response_hash": completion.response_hash or payload_hash(completion.content),
                        "input_tokens": completion.input_tokens,
                        "output_tokens": completion.output_tokens,
                    }
                )
                parsed, repairs = extract_and_repair_json_object(completion.content)
                local_repairs.extend(repairs)
                value = model.model_validate(parsed)
                attempt["status"] = "VALID"
                attempt["local_repairs"] = repairs
                attempts.append(attempt)
                return StructuredRun(
                    value=value,
                    completion=completion,
                    audit={
                        "structured_mode": mode,
                        "attempts": attempts,
                        "validation_errors": validation_errors,
                        "repaired": index > 1 or bool(local_repairs),
                        "local_repairs": list(dict.fromkeys(local_repairs)),
                        "fallback_used": False,
                        "model": completion.model,
                        "token_usage": {
                            "input_tokens": total_input_tokens,
                            "output_tokens": total_output_tokens,
                        },
                    },
                )
            except ResearchBudgetExceeded as exc:
                validation_errors.append(str(exc))
                attempt.update({"status": "BUDGET_EXHAUSTED", "error": str(exc), "retryable": False})
                attempts.append(attempt)
                break
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                error = _compact(exc)
                validation_errors.append(error)
                attempt.update({"status": "VALIDATION_FAILED", "error": error})
                if raw_output is None:
                    attempts.append(attempt)
                    break  # No returned text to repair; do not repeat the investigation.
            except Exception as exc:
                error = _compact(exc)
                validation_errors.append(error)
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = isinstance(exc, requests.RequestException) and (
                    status is None or status == 429 or status >= 500
                )
                attempt.update({"status": "REQUEST_FAILED", "error": error, "retryable": retryable})
                attempts.append(attempt)
                break  # A transport failure is not a format repair.
            attempts.append(attempt)
        return StructuredRun(
            value=fallback,
            completion=last_completion,
            audit={
                "structured_mode": attempts[-1]["structured_mode"] if attempts else "none",
                "attempts": attempts,
                "validation_errors": validation_errors,
                "repaired": False,
                "local_repairs": list(dict.fromkeys(local_repairs)),
                "fallback_used": True,
                "model": last_completion.model if last_completion else None,
                "token_usage": {
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                },
            },
        )

    @staticmethod
    def _repair_messages(raw_output: str, schema: dict[str, Any], validation_error: str) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "Repair only JSON syntax and schema conformance. Do not fetch facts, add facts, change numbers, "
                    "or reinterpret evidence. Return one JSON object and nothing else."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Schema:\n{json.dumps(schema, ensure_ascii=False, sort_keys=True)}\n\n"
                    f"Validation error:\n{validation_error}\n\n"
                    f"Raw output:\n{raw_output}"
                ),
            },
        ]
