"""Bounded discovery for selected local CSV, JSON, JSONL, and document files."""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import os
import re
import stat
import zipfile
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from itertools import islice
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from knowledge_layer.connectors.common import ConnectorError, stable_id, type_name

SUPPORTED_TABLE_TYPES = {".csv", ".json", ".jsonl", ".parquet"}
SUPPORTED_DOCUMENT_TYPES = {".md", ".txt", ".pdf", ".docx", ".html", ".htm"}
SENSITIVE_NAME = re.compile(r"(^|_)(password|secret|token|api_key|ssn|tax_id|dob|birth_date)(_|$)|email|phone|address", re.IGNORECASE)
DEFAULT_MAX_FILES = 100
DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
DEFAULT_PROFILE_ROWS = 5_000
DEFAULT_MAX_COLUMNS = 256
PROFILE_VERSION = 7
DOCUMENT_PROFILE_VERSION = 7
PDF_PROFILE_VERSION = 7
MAX_SEMANTIC_SAMPLES = 50
MAX_JOIN_PROBES = 100
MAX_DOCUMENT_TEXT_CHARS = 50_000
MAX_DOCUMENT_LINES = 4_000
MAX_DOCUMENT_CHUNK_CHARS = 280
MAX_DOCUMENT_QUANTIFIED_CLAIMS = 100
MAX_PDF_OPERATIONS = 50_000
# pypdf parses a whole decoded content stream into operation objects before
# visitor callbacks run. This also bounds that pre-visitor allocation.
MAX_PDF_DECODED_STREAM_BYTES = 256_000
MAX_PDF_TOTAL_CONTENT_BYTES = 256_000
MAX_PDF_XOBJECT_ENTRIES = 256
MAX_PDF_TEXT_XOBJECTS = 32
# A ToUnicode mapping can turn one encoded glyph into 256 Unicode characters
# before pypdf calls visitor_text. Bound the encoded text operands first.
MAX_PDF_TEXT_OPERAND_BYTES = 16_000


class _PdfTextBudgetExceeded(Exception):
    """Stop parsing before repeated PDF forms expand beyond the text budget."""


class _ColumnProfile:
    def __init__(self, name: str):
        self.name = name
        self.null_count = 0
        self.missing_count = 0
        self.empty_count = 0
        self.sample_count = 0
        self.type_counts: dict[str, int] = {}
        self.distinct_values: set[str] = set()
        self.distinct_capped = False
        self.sensitive = bool(SENSITIVE_NAME.search(name))

    def observe(self, value: Any, *, present: bool = True) -> None:
        self.sample_count += 1
        if not present:
            self.missing_count += 1
            return
        if value is None:
            self.null_count += 1
            return
        if isinstance(value, str) and not value.strip():
            self.empty_count += 1
        inferred_type = _value_type(value)
        self.type_counts[inferred_type] = self.type_counts.get(inferred_type, 0) + 1
        if self.sensitive:
            return
        if len(self.distinct_values) < 256:
            self.distinct_values.add(_distinct_value(value))
        else:
            self.distinct_capped = True

    def to_schema_field(self, total_rows: int) -> dict[str, Any]:
        if self.type_counts:
            data_type = max(self.type_counts, key=lambda name: (self.type_counts[name], name))
        else:
            data_type = "unknown"
        return {"name": self.name, "data_type": data_type,
                "nullable": self.null_count > 0 or self.missing_count > 0 if total_rows else None}

    def profile(self) -> dict[str, Any]:
        profile = {
            "null_count": self.null_count,
            "missing_count": self.missing_count,
            "empty_count": self.empty_count,
            "sample_count": self.sample_count,
            "distinct_count": None if self.sensitive else len(self.distinct_values),
            "distinct_count_is_lower_bound": self.distinct_capped,
            "sensitive_name": self.sensitive,
        }
        return profile


def _canonical_value(value: Any) -> str:
    """Represent exact JSON value equality without storing it in the profile.

    Decimal.normalize uses the current precision context and can round. Trim
    only trailing coefficient zeros instead; enormous exponents remain compact.
    JSON numbers share equality across integer/decimal notation, while booleans
    and strings keep their distinct JSON types.
    """
    def typed(item: Any) -> Any:
        if isinstance(item, bool):
            return ["boolean", item]
        if isinstance(item, (int, Decimal, float)):
            number = Decimal.from_float(item) if isinstance(item, float) else Decimal(item)
            if not number.is_finite():
                raise ConnectorError("invalid_json_number", "JSON numbers must be finite")
            sign, digits, exponent = number.as_tuple()
            if not any(digits):
                return ["number", 0, "0", 0]
            end = len(digits)
            while digits[end - 1] == 0:
                end -= 1
                exponent += 1
            return ["number", sign, "".join(str(digit) for digit in digits[:end]), exponent]
        if isinstance(item, dict):
            return ["object", [[key, typed(member)] for key, member in sorted(item.items())]]
        if isinstance(item, list):
            return ["array", [typed(member) for member in item]]
        if item is None:
            return ["null"]
        if isinstance(item, str):
            return ["string", item]
        if isinstance(item, (datetime, date)):
            return ["string", item.isoformat()]
        raise ConnectorError("invalid_json_record", "JSON values must use supported scalar or structured types")

    return json.dumps(typed(value), separators=(",", ":"))


def _distinct_value(value: Any) -> str:
    """Use the same exact value equality for cardinality and join probes."""
    return _canonical_value(value)


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, (float, Decimal)):
        return "number"
    if isinstance(value, (dict, list)):
        return "structured"
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.casefold() in {"true", "false"}:
            return "boolean"
        if re.fullmatch(r"[+-]?\d+", candidate):
            return "integer"
        if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][+-]?\d+)?", candidate):
            return "number"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
            return "date"
        if "T" in candidate and re.match(r"\d{4}-\d{2}-\d{2}T", candidate):
            return "timestamp"
        return "text"
    return type_name(type(value).__name__)


def _limit(config: dict[str, Any], key: str, default: int, maximum: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConnectorError("invalid_scan_limit", f"{key} must be a positive integer")
    return min(value, maximum)


def _selected_root(config: dict[str, Any]) -> tuple[Path, Path, str]:
    raw_root = config.get("root")
    if not isinstance(raw_root, str) or not raw_root:
        raise ConnectorError("source_root_required", "A server-configured source root is required")
    try:
        root = Path(raw_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ConnectorError("source_root_unavailable", "The configured source root is unavailable") from exc
    if not root.is_dir():
        raise ConnectorError("source_root_invalid", "The configured source root must be a directory")

    raw_path = config.get("path", ".")
    if not isinstance(raw_path, str) or not raw_path:
        raise ConnectorError("invalid_source_path", "path must be a relative directory selector")
    relative = Path(raw_path)
    if relative.is_absolute() or any(part in {"..", ""} for part in relative.parts):
        raise ConnectorError("path_outside_root", "The selected path must stay inside its configured root")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ConnectorError("path_outside_root", "Symbolic links are not allowed in a source path")
    try:
        selected = current.resolve(strict=True)
    except OSError as exc:
        raise ConnectorError("source_path_unavailable", "The selected source path is unavailable") from exc
    if not selected.is_relative_to(root) or not selected.is_dir():
        raise ConnectorError("path_outside_root", "The selected path must be a directory inside its configured root")
    return root, selected, relative.as_posix() if raw_path != "." else "."


def _safe_file_bytes(root: Path, pointer: str, max_file_bytes: int) -> tuple[int, int, bytes | None]:
    """Open every path component without following links, then read from one FD."""
    relative = Path(pointer)
    parts = relative.parts
    if relative.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ConnectorError("path_outside_root", "A discovered path left the configured root")
    directory_fd = -1
    file_fd = -1
    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConnectorError("invalid_source_file", "The discovered source is not a regular file")
        if info.st_size > max_file_bytes:
            return info.st_size, info.st_mtime_ns, None
        with os.fdopen(file_fd, "rb") as handle:
            file_fd = -1
            data = handle.read(max_file_bytes + 1)
        if len(data) > max_file_bytes:
            return len(data), info.st_mtime_ns, None
        return len(data), info.st_mtime_ns, data
    except OSError as exc:
        raise ConnectorError("source_path_unavailable", "A source file became unavailable") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def _column_states(names: list[str]) -> dict[str, _ColumnProfile]:
    if (len(names) != len(set(names)) or any(
        not isinstance(name, str) or not name.strip() or len(name) > 120
        or re.search(r"[\x00-\x1f\x7f]", name) for name in names
    )):
        raise ConnectorError("invalid_column_names", "Source columns must have distinct bounded printable names")
    return {name: _ColumnProfile(name) for name in names}


def _observe_row(states: dict[str, _ColumnProfile], row: dict[str, Any]) -> None:
    for name, state in states.items():
        state.observe(row.get(name), present=name in row)


def _table_profile(
    states: dict[str, _ColumnProfile], row_count: int, sampled_rows: int, *, truncated: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    schema = [state.to_schema_field(sampled_rows) for state in states.values()]
    null_counts = {name: state.null_count for name, state in states.items()}
    distinct_counts = {
        name: state.profile()["distinct_count"] for name, state in states.items()
    }
    profile = {
        "row_count": row_count,
        "row_count_is_complete": True,
        "profile_rows": sampled_rows,
        "profile_is_sampled": truncated or sampled_rows < row_count,
        "null_counts": null_counts,
        "missing_counts": {name: state.missing_count for name, state in states.items()},
        "empty_counts": {name: state.empty_count for name, state in states.items()},
        "schema_is_complete": True,
        "schema_type_basis": "bounded_profile",
        "distinct_count_basis": "typed_observed_representation",
        "distinct_counts": distinct_counts,
        "distinct_counts_are_lower_bounds": {
            name: state.distinct_capped for name, state in states.items()
        },
        "sensitive_fields_omitted_from_distinct_counts": [
            name for name, state in states.items() if state.sensitive
        ],
    }
    return schema, profile


def _profile_csv(data: bytes, profile_rows: int, max_columns: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with io.StringIO(data.decode("utf-8-sig"), newline="") as handle:
        reader = csv.reader(handle, strict=True)
        header = next(reader, [])
        if not header:
            return [], {"row_count": 0, "row_count_is_complete": True, "profile_rows": 0}
        if len(header) > max_columns:
            raise ConnectorError("column_limit_exceeded", "The source file exceeds max_columns")
        names = header
        states = _column_states(names)
        row_count = 0
        sampled = 0
        malformed = 0
        for values in reader:
            if not values:
                continue
            row_count += 1
            if len(values) > len(names):
                raise ConnectorError("malformed_csv_record", "A CSV record exceeds its declared header width")
            if sampled < profile_rows:
                row = {name: values[index] for index, name in enumerate(names) if index < len(values)}
                _observe_row(states, row)
                sampled += 1
        schema, profile = _table_profile(states, row_count, sampled, truncated=sampled < row_count)
        profile["malformed_rows"] = malformed
        return schema, profile


def _flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    # Keep nested structures as a field-level type; do not persist their values.
    return {str(key): value for key, value in row.items()}


def _profile_json_rows(
    rows: Iterable[dict[str, Any]],
    *,
    known_count: int,
    profile_rows: int,
    max_columns: int,
    retain_semantic_samples: bool = False,
    known_fields: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    names: list[str] = list(known_fields or [])
    for row in rows:
        if not isinstance(row, dict):
            raise ConnectorError("invalid_json_record", "JSON table records must be objects")
        for name in row:
            key = str(name)
            if key not in names:
                names.append(key)
                if len(names) > max_columns:
                    raise ConnectorError("column_limit_exceeded", "The source file exceeds max_columns")
        if len(selected) < profile_rows:
            selected.append(_flatten_row(row))
    states = _column_states(names)
    for row in selected:
        _observe_row(states, row)
    truncated = known_count > len(selected)
    schema, profile = _table_profile(states, known_count, len(selected), truncated=truncated)
    if retain_semantic_samples:
        entity_samples = []
        edge_samples = []
        considered_rows = min(len(selected), MAX_SEMANTIC_SAMPLES)
        for row in selected[:considered_rows]:
            if "entity_id" in row and "label" in row:
                entity_id, label = row.get("entity_id"), row.get("label")
                if isinstance(entity_id, str) and isinstance(label, str) and entity_id.strip() and label.strip():
                    if len(entity_id) > 120 or len(label) > 120:
                        raise ConnectorError("semantic_sample_limit_exceeded", "A retained source entity exceeds its string limit")
                    entity_samples.append({"entity_id": entity_id, "label": label})
            if {"subject_id", "predicate", "parent_id"} <= row.keys():
                subject, predicate, parent = row.get("subject_id"), row.get("predicate"), row.get("parent_id")
                if all(isinstance(value, str) and value.strip() for value in (subject, predicate, parent)):
                    if len(subject) > 120 or len(parent) > 120 or len(predicate) > 80:
                        raise ConnectorError("semantic_sample_limit_exceeded", "A retained source edge exceeds its string limit")
                    edge_samples.append({"subject_id": subject, "predicate": predicate, "parent_id": parent})
        if entity_samples or edge_samples:
            profile["semantic_samples"] = {
                "entities": entity_samples, "edges": edge_samples,
                "sampled_rows": considered_rows, "row_limit": MAX_SEMANTIC_SAMPLES,
                "is_sampled": considered_rows < known_count,
                "truncated": considered_rows < len(selected),
            }
    profile["content_retained"] = bool(profile.get("semantic_samples"))
    return schema, profile


def _csv_key_samples(data: bytes, key_fields: set[str], row_limit: int) -> dict[str, list[str]]:
    """Read bounded key values only in memory; callers persist aggregate counts."""
    result = {field: [] for field in key_fields}
    with io.StringIO(data.decode("utf-8-sig"), newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        for index, row in enumerate(reader):
            if index >= row_limit:
                break
            for field in key_fields:
                value = row.get(field)
                if isinstance(value, str) and value.strip():
                    result[field].append(value)
    return result


def _parquet_key_samples(data: bytes, key_fields: set[str], row_limit: int) -> dict[str, list[str]]:
    result = {field: [] for field in key_fields}
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(io.BytesIO(data), columns=[f for f in key_fields if f])
        sample = table.slice(0, row_limit)
        for row in sample.to_pylist():
            for field in key_fields:
                val = row.get(field)
                if val is not None and str(val).strip():
                    result[field].append(str(val))
    except (OSError, ValueError, TypeError, KeyError, ImportError):
        return result
    return result


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ConnectorError("duplicate_json_key", "A JSON object contains duplicate field names")
        result[name] = value
    return result


def _reject_nonfinite_json(_value: str) -> None:
    raise ConnectorError("invalid_json_number", "JSON numbers must be finite")


def _jsonl_rows(data: bytes) -> Iterable[dict[str, Any]]:
    """Parse records from an already byte-bounded, safely opened source file."""
    with io.StringIO(data.decode("utf-8-sig")) as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line, object_pairs_hook=_unique_json_object,
                                 parse_float=Decimal, parse_constant=_reject_nonfinite_json)
            except json.JSONDecodeError as exc:
                raise ConnectorError("parse_error", "JSONL source could not be parsed") from exc
            if not isinstance(row, dict):
                raise ConnectorError("parse_error", "JSONL source must contain object records")
            yield row


def _jsonl_key_samples(data: bytes, key_fields: set[str], row_limit: int) -> dict[str, list[str]]:
    """Keep scalar keys in memory only; missing/null keys never become zero."""
    result = {field: [] for field in key_fields}
    for row in islice(_jsonl_rows(data), row_limit):
        for field in key_fields:
            value = row.get(field)
            if value is None or isinstance(value, (dict, list)):
                continue
            if isinstance(value, str) and not value.strip():
                continue
            result[field].append(_canonical_value(value))
    return result


def _bounded_join_probes(
    assets: list[dict[str, Any]], contents: dict[str, bytes], profile_rows: int,
) -> list[dict[str, Any]]:
    """Count sampled key overlap across CSV/JSONL assets; retain no key values."""
    tables = [item for item in assets if item.get("asset_type") == "table" and item.get("id") in contents]
    sampled: dict[str, dict[str, list[str]]] = {}
    for asset in tables:
        keys = {str(field.get("name")) for field in asset.get("schema", [])
                if str(field.get("name", "")).endswith(("_id", "_key"))}
        if not keys:
            continue
        try:
            suffix = Path(asset["source_pointer"]).suffix.casefold()
            if suffix == ".jsonl":
                sampler = _jsonl_key_samples
            elif suffix == ".parquet":
                sampler = _parquet_key_samples
            else:
                sampler = _csv_key_samples
            sampled[asset["id"]] = sampler(contents[asset["id"]], keys, profile_rows)
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
    probes = []
    for source in tables:
        for target in tables:
            if source["id"] == target["id"]:
                continue
            for field in sorted(set(sampled.get(source["id"], {})) & set(sampled.get(target["id"], {}))):
                source_values = sampled[source["id"]][field]
                target_values = sampled[target["id"]][field]
                if not source_values or not target_values:
                    continue
                target_set = set(target_values)
                source_profile = source.get("profile") or {}
                target_profile = target.get("profile") or {}
                complete = all(
                    profile.get("row_count_is_complete")
                    and profile.get("profile_rows") == profile.get("row_count")
                    for profile in (source_profile, target_profile)
                )
                # A non-scalar key is not compared. Do not call that a complete
                # probe merely because every file row was otherwise profiled.
                complete = complete and all(
                    isinstance(profile.get("null_counts", {}).get(field), int)
                    and len(values) == profile.get("profile_rows", 0) - profile["null_counts"][field]
                        - profile.get("missing_counts", {}).get(field, 0)
                        - profile.get("empty_counts", {}).get(field, 0)
                    for profile, values in ((source_profile, source_values), (target_profile, target_values))
                )
                probes.append({
                    "source_asset_id": source["id"], "target_asset_id": target["id"],
                    "source_field": field, "target_field": field,
                    "source_nonnull_rows_probed": len(source_values),
                    "matched_source_rows": sum(value in target_set for value in source_values),
                    "target_nonnull_rows_probed": len(target_values),
                    "target_distinct_values_probed": len(target_set),
                    "target_unique_in_probe": len(target_set) == len(target_values),
                    "complete_source_and_target": bool(complete),
                    "raw_values_retained": False,
                })
                if len(probes) >= MAX_JOIN_PROBES:
                    return probes
    return probes


def _profile_json(path: Path, data: bytes, profile_rows: int, max_columns: int, retain_semantic_samples: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if path.suffix.casefold() == ".jsonl":
        rows: list[dict[str, Any]] = []
        names: list[str] = []
        row_count = 0
        # Validate all records inside the file byte budget, even beyond the
        # profile sample; an invalid tail cannot certify complete discovery.
        for row in _jsonl_rows(data):
            row_count += 1
            for name in row:
                if name not in names:
                    names.append(name)
                    if len(names) > max_columns:
                        raise ConnectorError("column_limit_exceeded", "The source file exceeds max_columns")
            if len(rows) < profile_rows:
                rows.append(row)
        return _profile_json_rows(rows, known_count=row_count, profile_rows=profile_rows, max_columns=max_columns, retain_semantic_samples=retain_semantic_samples, known_fields=names)

    try:
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_json_object,
                           parse_float=Decimal, parse_constant=_reject_nonfinite_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorError("parse_error", "JSON source could not be parsed") from exc
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        record_lists = [item for item in value.values() if isinstance(item, list) and item and all(isinstance(row, dict) for row in item[:profile_rows])]
        rows = record_lists[0] if len(record_lists) == 1 else [value]
    else:
        rows = [{"value": value}]
    return _profile_json_rows(rows, known_count=len(rows), profile_rows=profile_rows, max_columns=max_columns, retain_semantic_samples=retain_semantic_samples)


def _profile_parquet(data: bytes, profile_rows: int, max_columns: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ConnectorError("parquet_driver_unavailable", "pyarrow is required to profile Parquet files") from exc

    table = pq.read_table(io.BytesIO(data))
    names = table.column_names
    if len(names) > max_columns:
        raise ConnectorError("column_limit_exceeded", "The source file exceeds max_columns")
    states = _column_states(names)
    row_count = table.num_rows
    sample_table = table.slice(0, profile_rows)
    for row in sample_table.to_pylist():
        clean_row = {}
        for k, v in row.items():
            if isinstance(v, (datetime, date)):
                clean_row[k] = v.isoformat()
            else:
                clean_row[k] = v
        _observe_row(states, clean_row)
    schema, profile = _table_profile(states, row_count, len(sample_table), truncated=len(sample_table) < row_count)
    return schema, profile


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.chars = 0
        self.skip = False
        self.source_lines: list[int] = []
        self.truncated = False

    def append(self, value: str) -> None:
        self.parts.append(value)
        self.source_lines.append(self.getpos()[0])

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.skip = True
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.append("\n## ")
        elif tag in {"p", "li", "br"}:
            self.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.skip = False
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}:
            self.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip and self.chars < MAX_DOCUMENT_TEXT_CHARS:
            part = data[: MAX_DOCUMENT_TEXT_CHARS - self.chars]
            self.append(part)
            self.chars += len(part)
            self.truncated |= len(part) < len(data)
        elif not self.skip and data:
            self.truncated = True


def _candidate_paths(root: Path, selected: Path, pattern: str, max_files: int) -> tuple[list[Path], bool, bool]:
    """Walk bounded entries using directory descriptors anchored at the root."""
    max_examined = min(20_000, max(1_000, max_files * 20))
    examined = 0
    matched: list[Path] = []
    pending = [Path(".")]
    root_fd = -1
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        selected_parts = selected.relative_to(root).parts
        while pending:
            relative_dir = pending.pop()
            directory_fd = os.dup(root_fd)
            try:
                for part in (*selected_parts, *relative_dir.parts):
                    if part == ".":
                        continue
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                      dir_fd=directory_fd)
                    os.close(directory_fd)
                    directory_fd = next_fd
                with os.scandir(directory_fd) as scan:
                    for entry in scan:
                        examined += 1
                        if examined > max_examined:
                            return sorted(matched)[:max_files], len(matched) > max_files, True
                        if entry.name.startswith(".") or entry.is_symlink():
                            continue
                        relative_file = relative_dir / entry.name
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(relative_file)
                        elif entry.is_file(follow_symlinks=False) and relative_file.match(pattern):
                            matched.append(selected / relative_file)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise ConnectorError("source_path_unavailable", "A source directory became unavailable") from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)
    return sorted(matched)[:max_files], len(matched) > max_files, False


def _pdf_text_operand_bytes(
    stream: Any, pdf: Any, resources: Any,
    active_forms: set[int] | None = None, form_costs: dict[int, int] | None = None,
) -> int:
    """Count encoded text, including each Form invocation, before font mapping."""
    from pypdf.generic import ContentStream, TextStringObject

    active_forms = active_forms if active_forms is not None else set()
    form_costs = form_costs if form_costs is not None else {}
    operations = ContentStream(stream, pdf).operations
    if len(operations) > MAX_PDF_OPERATIONS:
        raise _PdfTextBudgetExceeded
    total = 0
    for operands, operator in operations:
        if operator == b"Do":
            current = resources.get_object() if resources is not None else None
            xobjects = current.get("/XObject") if isinstance(current, dict) else None
            xobjects = xobjects.get_object() if xobjects is not None else None
            if not operands or not isinstance(xobjects, dict) or operands[0] not in xobjects:
                raise _PdfTextBudgetExceeded
            form = xobjects[operands[0]].get_object()
            if not isinstance(form, dict) or "/Subtype" not in form:
                raise _PdfTextBudgetExceeded
            if form["/Subtype"] == "/Image":
                continue
            identity = id(form)
            if identity in active_forms:
                raise _PdfTextBudgetExceeded
            if identity not in form_costs:
                active_forms.add(identity)
                try:
                    form_costs[identity] = _pdf_text_operand_bytes(
                        form, pdf, form.get_inherited("/Resources"), active_forms, form_costs,
                    )
                finally:
                    active_forms.remove(identity)
            total += form_costs[identity]
            if total > MAX_PDF_TEXT_OPERAND_BYTES:
                raise _PdfTextBudgetExceeded
            continue
        if operator not in {b"Tj", b"TJ", b"'", b'"'}:
            continue
        if not operands:
            raise _PdfTextBudgetExceeded
        if operator == b"TJ":
            pieces = []
            for value in operands:
                if not isinstance(value, list):
                    raise _PdfTextBudgetExceeded
                pieces.extend(value)
        else:
            pieces = operands
        for index, piece in enumerate(pieces):
            if isinstance(piece, TextStringObject):
                total += len(piece.original_bytes)
            elif isinstance(piece, bytes | str):
                total += len(piece)
            elif (not isinstance(piece, (int, float))
                  or (operator != b"TJ" and not (operator == b'"' and index < 2))):
                raise _PdfTextBudgetExceeded
            if total > MAX_PDF_TEXT_OPERAND_BYTES:
                raise _PdfTextBudgetExceeded
    return total


def _check_pdf_page_content_budget(page: Any) -> None:
    """Bound all text-bearing XObjects before pypdf recursively parses operators."""
    contents = page.get_contents()
    total = len(contents.get_data()) if contents is not None else 0
    if total > MAX_PDF_TOTAL_CONTENT_BYTES:
        raise _PdfTextBudgetExceeded
    resources = [page.get_inherited("/Resources")]
    seen_xobjects: set[int] = set()
    while resources:
        current = resources.pop()
        if current is None:
            continue
        current = current.get_object()
        if not isinstance(current, dict):
            continue
        xobjects = current.get("/XObject")
        if xobjects is None:
            continue
        xobjects = xobjects.get_object()
        if not isinstance(xobjects, dict) or len(xobjects) > MAX_PDF_XOBJECT_ENTRIES:
            raise _PdfTextBudgetExceeded
        for reference in xobjects.values():
            form = reference.get_object()
            if not isinstance(form, dict) or "/Subtype" not in form:
                raise _PdfTextBudgetExceeded
            # pypdf skips /Image but recursively extracts every other subtype.
            if form["/Subtype"] == "/Image":
                continue
            identity = id(form)
            if identity in seen_xobjects:
                continue
            seen_xobjects.add(identity)
            if len(seen_xobjects) > MAX_PDF_TEXT_XOBJECTS:
                raise _PdfTextBudgetExceeded
            total += len(form.get_data())
            if total > MAX_PDF_TOTAL_CONTENT_BYTES:
                raise _PdfTextBudgetExceeded
            resources.append(form.get_inherited("/Resources"))
    if contents is not None:
        _pdf_text_operand_bytes(contents, page.pdf, page.get_inherited("/Resources"))


def _document_lines(
    path: Path, data: bytes, *, metadata: dict[str, Any] | None = None,
) -> tuple[list[str], str]:
    """Extract bounded text without retaining bodies in the source registry."""
    suffix = path.suffix.casefold()
    info = metadata if metadata is not None else {}
    info.update({"format": suffix.lstrip("."), "complete": True, "truncated": False,
                 "truncation_reasons": [], "line_locators": [],
                 "locator_basis": "source_lines" if suffix in {".md", ".txt"} else "extracted_text"})

    def finish(text: str, locators: list[dict[str, Any]] | None = None,
               *, empty_status: str = "no_text") -> tuple[list[str], str]:
        if len(text) > MAX_DOCUMENT_TEXT_CHARS:
            info["truncation_reasons"].append("text_character_limit")
        limited = text[:MAX_DOCUMENT_TEXT_CHARS]
        all_lines = limited.splitlines(keepends=True)
        if len(all_lines) > MAX_DOCUMENT_LINES:
            info["truncation_reasons"].append("text_line_limit")
        lines = all_lines[:MAX_DOCUMENT_LINES]
        info["truncated"] = bool(info["truncation_reasons"])
        info["complete"] = not info["truncated"]
        info["line_locators"] = (locators[:len(lines)] if locators is not None else
                                  [{"line": index} for index in range(1, len(lines) + 1)])
        if not any(line.strip() for line in lines):
            info["complete"] = empty_status == "no_text" and not info["truncated"]
            return lines, empty_status
        return lines, "extracted"

    def decode_prefix(prefix: bytes) -> str:
        try:
            # A budget may cut a valid multi-byte character. Drop only that
            # unexamined tail, while refusing invalid bytes in examined text.
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            return decoder.decode(prefix, final=len(prefix) == len(data))
        except UnicodeDecodeError as exc:
            raise ConnectorError("document_decode_error", "Source document must be valid UTF-8") from exc

    try:
        if suffix in {".md", ".txt"}:
            prefix = data[:MAX_DOCUMENT_TEXT_CHARS * 4 + 4]
            if len(prefix) < len(data):
                info["truncation_reasons"].append("decoded_input_limit")
            return finish(decode_prefix(prefix))
        if suffix == ".pdf":
            from pypdf import PdfReader, apply_configuration
            from pypdf.errors import LimitReachedError

            try:
                # Bound decompression before extract_text constructs the string.
                # The retained-character slice below is a separate storage limit.
                with apply_configuration(
                    maximum_declared_stream_length=MAX_PDF_DECODED_STREAM_BYTES,
                    array_based_stream_maximum_output_length=MAX_PDF_DECODED_STREAM_BYTES,
                    zlib_maximum_output_length=MAX_PDF_DECODED_STREAM_BYTES,
                    lzw_maximum_output_length=MAX_PDF_DECODED_STREAM_BYTES,
                    run_length_maximum_output_length=MAX_PDF_DECODED_STREAM_BYTES,
                    image_maximum_buffer_size=MAX_PDF_DECODED_STREAM_BYTES,
                ):
                    reader = PdfReader(io.BytesIO(data), strict=False)
                    if reader.is_encrypted:
                        info["complete"] = False
                        return [], "encrypted"
                    parts = []
                    locators = []
                    chars = 0
                    observed_chars = 0
                    observed_operations = 0
                    budget_exceeded = False

                    def count_text(value: str, *_args: Any) -> None:
                        nonlocal observed_chars, budget_exceeded
                        observed_chars += len(value)
                        if observed_chars > MAX_DOCUMENT_TEXT_CHARS:
                            budget_exceeded = True
                            raise _PdfTextBudgetExceeded

                    def count_operation(*_args: Any) -> None:
                        nonlocal observed_operations, budget_exceeded
                        observed_operations += 1
                        if (observed_operations > MAX_PDF_OPERATIONS
                                or budget_exceeded):
                            budget_exceeded = True
                            raise _PdfTextBudgetExceeded

                    if len(reader.pages) > 8:
                        info["truncation_reasons"].append("pdf_page_limit")
                    for page_number, page in enumerate(reader.pages[:8], 1):
                        if chars >= MAX_DOCUMENT_TEXT_CHARS:
                            info["truncation_reasons"].append("text_character_limit")
                            break
                        _check_pdf_page_content_budget(page)
                        part = (page.extract_text(
                            visitor_text=count_text,
                            visitor_operand_before=count_operation,
                        ) or "")[:MAX_DOCUMENT_TEXT_CHARS - chars]
                        # pypdf can suppress visitor exceptions inside Form XObjects.
                        # The shared flag still prevents accepting an over-budget page.
                        if budget_exceeded:
                            info.update(complete=False, truncated=True,
                                        truncation_reasons=["pdf_text_operation_limit"])
                            return [], "document_text_limit"
                        parts.append(part + "\n")
                        locators.extend({"page": page_number, "extracted_line": index}
                                        for index in range(1, len((part + "\n").splitlines(keepends=True)) + 1))
                        chars += len(part)
                info["locator_basis"] = "pdf_page_and_extracted_text_line"
                return finish("".join(parts), locators, empty_status="no_text_or_ocr_required")
            except _PdfTextBudgetExceeded:
                info.update(complete=False, truncated=True,
                            truncation_reasons=["pdf_text_operation_limit"])
                return [], "document_text_limit"
            except LimitReachedError:
                info.update(complete=False, truncated=True,
                            truncation_reasons=["pdf_decoded_stream_limit"])
                return [], "document_stream_limit"
            except Exception as exc:
                raise ConnectorError("document_parse_error", "The PDF could not be safely profiled") from exc
        if suffix == ".docx":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                member = archive.getinfo("word/document.xml")
                if member.file_size > 2 * 1024 * 1024:
                    info.update(complete=False, truncated=True,
                                truncation_reasons=["docx_xml_limit"])
                    return [], "document_xml_limit"
                root = ElementTree.fromstring(archive.read(member))
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            lines = []
            locators = []
            chars = 0
            for paragraph_number, paragraph in enumerate(root.iter(f"{ns}p"), 1):
                value = "".join(node.text or "" for node in paragraph.iter(f"{ns}t")).strip()
                if not value:
                    continue
                style = paragraph.find(f"{ns}pPr/{ns}pStyle")
                if style is not None and "heading" in str(style.get(f"{ns}val", "")).casefold():
                    value = "## " + value
                lines.append(value + "\n")
                locators.extend({"paragraph": paragraph_number, "extracted_line": index}
                                for index in range(1, len((value + "\n").splitlines(keepends=True)) + 1))
                chars += len(lines[-1])
                if len(lines) >= MAX_DOCUMENT_LINES or chars >= MAX_DOCUMENT_TEXT_CHARS:
                    info["truncation_reasons"].append("docx_text_budget")
                    break
            info["locator_basis"] = "docx_paragraph_and_extracted_text_line"
            return finish("".join(lines), locators)
        if suffix in {".html", ".htm"}:
            parser = _HTMLText()
            prefix = data[:MAX_DOCUMENT_TEXT_CHARS * 4]
            parser.feed(decode_prefix(prefix))
            if len(prefix) < len(data):
                info["truncation_reasons"].append("html_input_limit")
            if parser.truncated:
                info["truncation_reasons"].append("html_text_limit")
            source_lines = []
            current_source = None
            for part, start_line in zip(parser.parts, parser.source_lines, strict=True):
                for fragment in part.splitlines(keepends=True):
                    if fragment.strip() and current_source is None:
                        current_source = start_line
                    if fragment.endswith(("\n", "\r")):
                        source_lines.append({"source_node_line": current_source or start_line})
                        current_source = None
            if current_source is not None:
                source_lines.append({"source_node_line": current_source})
            # Entity expansion and block separators change rendered line numbers;
            # the source coordinate is the parser node's start, not a fabricated
            # exact raw-HTML line for each rendered line.
            info["locator_basis"] = "html_source_node_start_line_and_rendered_text_line"
            return finish("".join(parser.parts), source_lines)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise ConnectorError("document_parse_error", "The document could not be safely profiled") from exc
    info["complete"] = False
    return [], "unsupported"


def _document_profile(path: Path, data: bytes, retain_semantic_samples: bool = False) -> dict[str, Any]:
    terms: list[str] = []
    term_locators: dict[str, dict[str, Any]] = {}
    quantified_claims: list[dict[str, Any]] = []
    descriptions: list[dict[str, Any]] = []
    extraction: dict[str, Any] = {}
    lines, extraction_status = _document_lines(path, data, metadata=extraction)
    locators = extraction.pop("line_locators", [])
    extraction["status"] = extraction_status
    active_term: str | None = path.stem.replace("_", " ")[:96] if extraction_status == "extracted" else None
    if active_term:
        terms.append(active_term)
        term_locators[active_term] = {"format": extraction["format"],
                                     "coordinate_basis": "source_filename", "filename": path.name}
    paragraph: list[tuple[int, str]] = []
    chunk_count = 0
    quantified_count = 0

    def line_locator(index: int, first_col: int, last_col: int) -> dict[str, Any]:
        locator = {"format": extraction["format"], "coordinate_basis": extraction["locator_basis"],
                   "extracted_line_start": index + 1, "extracted_line_end": index + 1,
                   "column_start": first_col, "column_end": last_col}
        native = locators[index] if index < len(locators) else {}
        for key in ("line", "page", "paragraph", "source_node_line"):
            if key in native:
                locator[f"{key}_start"] = native[key]
                locator[f"{key}_end"] = native[key]
        return locator

    def flush_paragraph() -> None:
        nonlocal chunk_count
        if not paragraph:
            return
        text = "".join(value for _, value in paragraph)

        def at(offset: int) -> tuple[int, int, dict[str, Any]]:
            consumed = 0
            for line_index, value in paragraph:
                if offset < consumed + len(value):
                    return line_index, offset - consumed + 1, (
                        locators[line_index] if line_index < len(locators) else {})
                consumed += len(value)
            raise ValueError("Document chunk offset is outside its extracted paragraph")

        if retain_semantic_samples and active_term:
            start = 0
            while start < len(text):
                end = min(start + MAX_DOCUMENT_CHUNK_CHARS, len(text))
                if end < len(text):
                    # Prefer complete source lines, preserving ordinary table
                    # rows and wrapped policy statements whenever they fit.
                    window = text[start:end]
                    boundaries = list(re.finditer(r"\n", window))
                    if not boundaries or boundaries[-1].start() < MAX_DOCUMENT_CHUNK_CHARS // 2:
                        boundaries = list(re.finditer(r"\s+", window))
                    if boundaries and boundaries[-1].start() >= MAX_DOCUMENT_CHUNK_CHARS // 2:
                        end = start + boundaries[-1].end()
                piece = text[start:end]
                excerpt = piece.strip()
                if not excerpt:
                    start = end
                    continue
                # Preserve the preceding complete word at a passage boundary.
                # The overlap counts toward the same 50 x 280 retention budget.
                overlap = re.search(r"\S+\s*$", text[start:end]) if end < len(text) else None
                next_start = (start + overlap.start() if overlap and overlap.start() > 0
                              and len(overlap.group()) <= 32 else end)
                chunk_count += 1
                if len(descriptions) >= MAX_SEMANTIC_SAMPLES:
                    start = next_start
                    continue
                left = start + len(piece) - len(piece.lstrip())
                right = start + len(piece.rstrip()) - 1
                first, first_col, first_locator = at(left)
                last, last_col, last_locator = at(right)
                locator: dict[str, Any] = {
                    "format": extraction["format"], "coordinate_basis": extraction["locator_basis"],
                    "extracted_line_start": first + 1, "extracted_line_end": last + 1,
                    "column_start": first_col, "column_end": last_col,
                }
                for key in ("line", "page", "paragraph", "source_node_line"):
                    if key in first_locator and key in last_locator:
                        locator[f"{key}_start"] = first_locator[key]
                        locator[f"{key}_end"] = last_locator[key]
                descriptions.append({"term": active_term, "excerpt": excerpt,
                                     "line": first_locator.get("line"),
                                     "source_locator": locator, "chunk_index": chunk_count})
                start = next_start
        paragraph.clear()

    for line_index, line in enumerate(lines):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading and len(heading.group(1).strip()) <= 120:
            flush_paragraph()
            active_term = heading.group(1).strip()[:96]
            if active_term and active_term not in terms:
                terms.append(active_term)
                term_locators[active_term] = line_locator(
                    line_index, heading.start(1) + 1, heading.start(1) + len(active_term))
            continue
        if line.strip():
            paragraph.append((line_index, line))
        else:
            flush_paragraph()
        if retain_semantic_samples and active_term:
            matches = re.finditer(
                r"\b(?:within|under|up to|target(?: is| of)?|no more than)\s+(\d+(?:\.\d+)?)\s*(minutes?|hours?|days?|%)\b",
                line,
                re.IGNORECASE,
            )
            for quantified in matches:
                quantified_count += 1
                if len(quantified_claims) >= MAX_DOCUMENT_QUANTIFIED_CLAIMS:
                    continue
                unit = quantified.group(2).casefold().rstrip("s")
                quantified_claims.append(
                    {"term": active_term, "value": float(quantified.group(1)),
                     "unit": unit, "rule": "heading_scoped_numeric_threshold",
                     "source_locator": line_locator(line_index, quantified.start() + 1, quantified.end())}
                )
    flush_paragraph()
    candidate_reasons = []
    if len(terms) > MAX_SEMANTIC_SAMPLES:
        candidate_reasons.append("heading_candidate_limit")
    if quantified_count > MAX_DOCUMENT_QUANTIFIED_CLAIMS:
        candidate_reasons.append("quantified_claim_candidate_limit")
    candidates = {
        "complete": not candidate_reasons, "truncated": bool(candidate_reasons),
        "truncation_reasons": candidate_reasons, "coverage_basis": "bounded_extracted_text",
        "term_limit": MAX_SEMANTIC_SAMPLES, "observed_terms": len(terms),
        "retained_terms": min(len(terms), MAX_SEMANTIC_SAMPLES),
        "quantified_claims_enabled": retain_semantic_samples,
        "quantified_claim_limit": MAX_DOCUMENT_QUANTIFIED_CLAIMS,
        "observed_quantified_claims": quantified_count if retain_semantic_samples else None,
        "retained_quantified_claims": len(quantified_claims),
    }
    retention = {
        "enabled": retain_semantic_samples,
        "complete": (extraction["complete"] and chunk_count <= MAX_SEMANTIC_SAMPLES)
        if retain_semantic_samples else None,
        "truncated": retain_semantic_samples and chunk_count > MAX_SEMANTIC_SAMPLES,
        "truncation_reasons": ["retained_chunk_limit"] if chunk_count > MAX_SEMANTIC_SAMPLES else [],
        "chunk_limit": MAX_SEMANTIC_SAMPLES, "chunk_chars": MAX_DOCUMENT_CHUNK_CHARS,
        "retained_chunks": len(descriptions), "observed_chunks": chunk_count if retain_semantic_samples else None,
        "source_text_complete": extraction["complete"],
    }
    return {
        "row_count": None,
        "row_count_is_complete": False,
        "size_bytes": len(data),
        "line_count": len(lines),
        "extraction_status": extraction_status,
        "document_extraction": extraction,
        "document_retention": retention,
        "document_candidates": candidates,
        "document_signals": {"candidate_terms": terms[:MAX_SEMANTIC_SAMPLES],
                             "candidate_term_locators": {term: term_locators[term]
                                                         for term in terms[:MAX_SEMANTIC_SAMPLES]},
                             "quantified_claims": quantified_claims, "candidate_descriptions": descriptions},
        "content_retained": bool(descriptions),
    }


def discover(config: dict[str, Any]) -> dict[str, Any]:
    """Inventory a root-bounded directory; source samples require explicit opt-in."""
    root, selected, relative_path = _selected_root(config)
    max_files = _limit(config, "max_files", DEFAULT_MAX_FILES, 1_000)
    max_file_bytes = _limit(config, "max_file_bytes", DEFAULT_MAX_FILE_BYTES, 64 * 1024 * 1024)
    max_total_bytes = _limit(config, "max_total_bytes", DEFAULT_MAX_TOTAL_BYTES, 512 * 1024 * 1024)
    profile_rows = _limit(config, "profile_rows", DEFAULT_PROFILE_ROWS, 100_000)
    max_columns = _limit(config, "max_columns", DEFAULT_MAX_COLUMNS, 1_000)
    retain_semantic_samples = config.get("retain_semantic_samples", False)
    if not isinstance(retain_semantic_samples, bool):
        raise ConnectorError("invalid_semantic_sampling", "retain_semantic_samples must be a boolean")
    profile_policy = {"profile_rows": profile_rows, "max_columns": max_columns, "retain_semantic_samples": retain_semantic_samples}
    previous = config.get("_previous_snapshot")
    previous_assets = {
        asset["id"]: asset for asset in previous.get("assets", []) if isinstance(asset, dict) and isinstance(asset.get("id"), str)
    } if isinstance(previous, dict) and previous.get("discovery_complete") is True else {}
    reused_assets: list[str] = []
    reprofiled_assets: list[str] = []
    table_contents: dict[str, bytes] = {}

    include = config.get("include", "**/*")
    if not isinstance(include, str) or not include or Path(include).is_absolute() or ".." in Path(include).parts:
        raise ConnectorError("invalid_include_pattern", "include must be a relative file pattern")
    pattern = include.removeprefix("**/")
    candidates, files_truncated, enumeration_truncated = _candidate_paths(root, selected, pattern, max_files)
    errors: list[dict[str, Any]] = []
    if files_truncated:
        errors.append({"code": "file_limit_exceeded", "message": f"Only the first {max_files} matching files were inspected", "source_pointer": relative_path})
    if enumeration_truncated:
        errors.append({"code": "enumeration_limit_exceeded", "message": "The source directory exceeded the bounded enumeration budget", "source_pointer": relative_path})

    name = config.get("name") if isinstance(config.get("name"), str) else "Local source"
    source_id = config.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        source_id = stable_id("source", "local_files", name, relative_path)
    assets: list[dict[str, Any]] = []
    consumed_bytes = 0
    recognized_any = False

    for path in candidates:
        suffix = path.suffix.casefold()
        if suffix not in SUPPORTED_TABLE_TYPES | SUPPORTED_DOCUMENT_TYPES:
            continue
        recognized_any = True
        pointer = (Path(relative_path) / path.relative_to(selected)).as_posix()
        try:
            size, mtime_ns, data = _safe_file_bytes(root, pointer, max_file_bytes)
        except ConnectorError as exc:
            errors.append({"code": exc.code, "message": exc.public_message, "source_pointer": pointer})
            continue
        if consumed_bytes + size > max_total_bytes:
            errors.append({"code": "scan_budget_exceeded", "message": "The source scan reached max_total_bytes", "source_pointer": pointer})
            break
        consumed_bytes += size
        revision = hashlib.sha256(data).hexdigest() if data is not None else stable_id("partial", size, mtime_ns, path.name)
        asset_id = stable_id("asset", source_id, pointer)
        asset_type = "table" if suffix in SUPPORTED_TABLE_TYPES else "document"
        schema: list[dict[str, Any]] = []
        profile: dict[str, Any]
        old = previous_assets.get(asset_id)
        old_profile = old.get("profile", {}) if isinstance(old, dict) else {}
        profile_version = (PDF_PROFILE_VERSION if suffix == ".pdf" else
                           DOCUMENT_PROFILE_VERSION if suffix in SUPPORTED_DOCUMENT_TYPES else PROFILE_VERSION)
        can_reuse = (
            data is not None and old.get("revision") == revision
            and old.get("source_pointer") == pointer and old.get("asset_type") == asset_type
            and old_profile.get("profile_version") == profile_version
            and old_profile.get("profile_policy") == profile_policy
        ) if isinstance(old, dict) else False
        if can_reuse:
            schema = old["schema"]
            profile = dict(old_profile)
            reused_assets.append(pointer)
        elif data is None:
            profile = {"row_count": None, "row_count_is_complete": False, "profile_rows": 0, "profile_is_sampled": True, "size_bytes": size, "content_retained": False}
            errors.append({"code": "file_size_limit_exceeded", "message": "The source file exceeds max_file_bytes; metadata only was retained", "source_pointer": pointer})
        else:
            try:
                if suffix == ".csv":
                    schema, profile = _profile_csv(data, profile_rows, max_columns)
                elif suffix in {".json", ".jsonl"}:
                    schema, profile = _profile_json(path, data, profile_rows, max_columns, retain_semantic_samples)
                elif suffix == ".parquet":
                    schema, profile = _profile_parquet(data, profile_rows, max_columns)
                else:
                    profile = _document_profile(path, data, retain_semantic_samples)
            except ConnectorError as exc:
                profile = {"row_count": None, "row_count_is_complete": False, "size_bytes": size, "content_retained": False}
                errors.append({"code": exc.code, "message": exc.public_message, "source_pointer": pointer})
            except (OSError, csv.Error, UnicodeDecodeError, json.JSONDecodeError):
                profile = {"row_count": None, "row_count_is_complete": False, "size_bytes": size, "content_retained": False}
                errors.append({"code": "parse_error", "message": "The source file could not be safely profiled", "source_pointer": pointer})
        if not can_reuse:
            reprofiled_assets.append(pointer)
        profile["size_bytes"] = size
        profile["content_retained"] = bool(profile.get("content_retained") or profile.get("semantic_samples"))
        profile["profile_version"] = profile_version
        profile["profile_policy"] = profile_policy
        assets.append(
            {
                "id": asset_id,
                "name": path.name,
                "asset_type": asset_type,
                "source_pointer": pointer,
                "revision": revision,
                "revision_basis": "sha256" if data is not None else "size_mtime_partial_hash",
                "schema": schema,
                "profile": profile,
                "trust_state": "unverified",
            }
        )
        if suffix in {".csv", ".jsonl"} and schema and data is not None:
            table_contents[asset_id] = data

    if not recognized_any and not errors:
        errors.append({"code": "no_supported_files", "message": "No supported source files were found in the selected path", "source_pointer": relative_path})
    aggregate_revision = stable_id("revision", *[(asset["source_pointer"], asset["revision"]) for asset in assets])
    join_probes = _bounded_join_probes(assets, table_contents, profile_rows)
    return {
        "source": {"id": source_id, "name": name, "kind": "local_files", "location": relative_path, "revision": aggregate_revision, "trust_state": "unverified"},
        "assets": assets,
        "capabilities": {"metadata": True, "safe_profiling": True, "documents": True, "change_signals": True, "query_execution": False, "permission_mirroring": False},
        "errors": errors,
        "profiling": {"reused_assets": reused_assets, "reprofiled_assets": reprofiled_assets,
                      "join_probe_count": len(join_probes)},
        "join_probes": join_probes,
    }
