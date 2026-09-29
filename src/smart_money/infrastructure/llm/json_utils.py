from __future__ import annotations

import json
import re
from typing import Any


def extract_json_object(text: str) -> dict[str, Any]:
    if not text:
        raise ValueError("empty model response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model response was not a JSON object")
    return parsed


def extract_and_repair_json_object(text: str) -> tuple[dict[str, Any], list[str]]:
    """Apply bounded syntax-only repairs before schema validation.

    No key or semantic value is invented here.  Repairs are limited to common
    transport/rendering damage; the caller must still validate the result with
    its Pydantic schema.
    """

    try:
        return extract_json_object(text), []
    except (ValueError, json.JSONDecodeError):
        pass
    cleaned = (text or "").strip()
    repairs: list[str] = []
    fenced = re.sub(r"^```(?:json)?\s*", "", cleaned)
    fenced = re.sub(r"\s*```$", "", fenced)
    if fenced != cleaned:
        repairs.append("MARKDOWN_FENCE_REMOVED")
    cleaned = fenced
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0:
        candidate = cleaned[start : end + 1] if end > start else cleaned[start:]
        if candidate != cleaned:
            repairs.append("SURROUNDING_TEXT_REMOVED")
        cleaned = candidate
    trailing = re.sub(r",\s*([}\]])", r"\1", cleaned)
    if trailing != cleaned:
        repairs.append("TRAILING_COMMA_REMOVED")
        cleaned = trailing
    invalid_escape = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", cleaned)
    if invalid_escape != cleaned:
        repairs.append("INVALID_ESCAPE_ESCAPED")
        cleaned = invalid_escape
    quote_count = 0
    escaped = False
    for character in cleaned:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character == '"':
            quote_count += 1
    if quote_count % 2:
        closing = cleaned.rfind("}")
        cleaned = cleaned[:closing] + '"' + cleaned[closing:] if closing >= 0 else cleaned + '"'
        repairs.append("UNCLOSED_STRING_CLOSED")
    if cleaned.count("{") > cleaned.count("}"):
        cleaned += "}" * (cleaned.count("{") - cleaned.count("}"))
        repairs.append("UNCLOSED_OBJECT_CLOSED")
    parsed = json.loads(cleaned, strict=False)
    if not isinstance(parsed, dict):
        raise ValueError("model response was not a JSON object")
    return parsed, repairs
