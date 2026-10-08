"""Shared bounded extraction for operator-enabled remote document contents."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.local_files import _document_profile

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 60
DOCUMENT_SUFFIXES = {".txt", ".md", ".pdf", ".docx", ".html", ".htm"}


class DocumentLimitExceeded(Exception):
    """A single file is too large; keep its metadata and record the gap."""


def operator_content_policy(profile: dict[str, Any]) -> tuple[bool, int, int]:
    enabled = profile.get("retain_document_content", False)
    maximum = profile.get("max_file_bytes", MAX_FILE_BYTES)
    total = profile.get("max_total_bytes", MAX_TOTAL_BYTES)
    if (type(enabled) is not bool or type(maximum) is not int or not 1 <= maximum <= MAX_FILE_BYTES
            or type(total) is not int or not maximum <= total <= MAX_TOTAL_BYTES):
        raise ConnectorError("document_policy_invalid", "The server profile has an invalid document-retention policy")
    return enabled, maximum, total


class RemoteBudget:
    def __init__(self, total_bytes: int = MAX_TOTAL_BYTES):
        self.deadline = time.monotonic() + MAX_SECONDS
        self.total_bytes = total_bytes
        self.content_bytes = 0
        self.metadata_bytes = 0

    def timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectorError("remote_discovery_timeout", "Remote discovery exceeded its time budget; narrow the selected scope")
        return min(10, remaining)

    def consume(self, amount: int, *, content: bool = False) -> None:
        self.timeout()
        if content:
            self.content_bytes += amount
            if self.content_bytes > self.total_bytes:
                raise ConnectorError("document_total_byte_limit", "Remote document reads exceed the operator's total byte budget")
        else:
            self.metadata_bytes += amount
            if self.metadata_bytes > MAX_METADATA_BYTES:
                raise ConnectorError("remote_metadata_too_large", "Remote metadata exceeds its response budget")

    def consume_json(self, value: Any) -> None:
        try:
            for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
                self.consume(len(chunk.encode("utf-8")))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ConnectorError("remote_metadata_invalid", "The source returned invalid metadata") from exc


def metadata_profile(size: int | None, status: str = "not_requested", *, enabled: bool = False) -> dict[str, Any]:
    return {"metadata_only": True, "size_bytes": size, "row_count": None,
            "row_count_is_complete": False, "profile_rows": 0, "profile_is_sampled": False,
            "null_counts": {}, "distinct_counts": {}, "content_retained": False,
            "extraction_status": status,
            "document_extraction": {"status": status, "complete": False, "truncated": False,
                                    "truncation_reasons": []},
            "document_retention": {"enabled": enabled, "complete": False if enabled else None,
                                   "retained_chunks": 0, "source_text_complete": False}}


def extract_document(name: str, suffix: str, data: bytes, budget: RemoteBudget) -> tuple[dict[str, Any], dict[str, str] | None]:
    """Reuse local extraction limits and native locators; never execute contents."""
    budget.timeout()
    try:
        filename = Path(name).name
        if Path(filename).suffix.casefold() != suffix:
            filename += suffix
        profile = _document_profile(Path(filename), data, retain_semantic_samples=True)
        profile.update(metadata_only=False, source_content_sha256=hashlib.sha256(data).hexdigest(),
                       profile_version=1, profile_policy={"retain_document_content": True})
        budget.timeout()
        status = profile["extraction_status"]
        error = None if status == "extracted" and profile["document_extraction"]["complete"] else {
            "code": "document_extraction_incomplete", "message": "Remote document text extraction has a recorded coverage gap"}
        return profile, error
    except ConnectorError as exc:
        return metadata_profile(len(data), exc.code, enabled=True), {"code": exc.code, "message": exc.public_message}
