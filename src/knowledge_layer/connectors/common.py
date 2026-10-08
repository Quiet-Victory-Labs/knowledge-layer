"""Shared safe primitives for source discovery connectors."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


class ConnectorError(Exception):
    """An expected connector failure with a safe, stable public description."""

    def __init__(self, code: str, message: str, source_pointer: str | None = None):
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.source_pointer = source_pointer


def stable_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def safe_identifier(value: Any, *, max_length: int = 128) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", value)) and len(value) <= max_length


def normalize_name(value: str) -> str:
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return " ".join(re.findall(r"[a-z0-9]+", words.casefold()))


def type_name(value: Any) -> str:
    """Normalize Python/Arrow/SQL type descriptions without leaking values."""
    raw = str(value).strip().casefold()
    for _ in range(4):
        wrapped = re.fullmatch(r"(?:nullable|lowcardinality)\((.*)\)", raw)
        if wrapped is None:
            break
        raw = wrapped[1].strip()
    # Container descriptions include their member types. A struct containing
    # an integer is still a struct, rather than an integer column.
    if re.match(r"^(?:jsonb?|struct|list|large_list|fixed_size_list|array|map)(?:\b|[<(])", raw) or raw.endswith("[]"):
        return "structured"
    if re.match(r"^(?:u?int\d*|integer|bigint|smallint|tinyint|mediumint|serial|bigserial|smallserial|long)(?:\b|\()", raw):
        return "integer"
    if re.match(r"^(?:float\d*|double|decimal\d*|bignumeric|numeric|real|number)(?:\b|\()", raw):
        return "number"
    if re.match(r"^bool(?:ean)?\b", raw):
        return "boolean"
    if re.match(r"^(?:timestamp|datetime)(?:\d*\b|[\[(])", raw):
        return "timestamp"
    if re.match(r"^date(?:32|64)?(?:\b|\[)", raw):
        return "date"
    if re.match(r"^(?:n?var?char|n?char|character|text|string|large_string|fixedstring|enum\d*)(?:\b|\()", raw):
        return "text"
    return "unknown"
