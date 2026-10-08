"""Optional offline SHACL Core checks for one bounded snapshot RDF projection.

Only bundled shapes execute. No inference, imports, remote graph fetching,
advanced rules or JavaScript; this module is not an API publication gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib.resources import files
from pathlib import Path
from threading import RLock
from typing import Any

from rdflib import RDF, SH, Graph, URIRef

SCHEMA_VERSION = "knowledge-rdf-shacl-v1"
MAX_INPUT_BYTES = 5 * 1024 * 1024
MAX_TRIPLES = 50_000
MAX_RESULTS = 100
_DIAGNOSTICS_ACTIVE: ContextVar[bool] = ContextVar("rdf_validation_diagnostics", default=False)
_DIAGNOSTICS_LOCK = RLock()


class RDFValidationError(ValueError):
    """A safe refusal code, without source contents or parser diagnostics."""


class _QuietDiagnostics(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _DIAGNOSTICS_ACTIVE.get()


@contextmanager
def _quiet_library_diagnostics() -> Iterator[None]:
    """Suppress value-bearing library logs only in this offline operation.

    pySHACL adds a console handler on each call. Restore its handlers and level
    afterward, and leave unrelated threads' RDFLib logging available.
    """
    with _DIAGNOSTICS_LOCK:
        token = _DIAGNOSTICS_ACTIVE.set(True)
        quiet = _QuietDiagnostics()
        term_logger = logging.getLogger("rdflib.term")
        validator_logger = logging.getLogger("pyshacl-validate")
        handlers = tuple(validator_logger.handlers)
        level = validator_logger.level
        for logger in (term_logger, validator_logger):
            logger.addFilter(quiet)
        try:
            yield
        finally:
            for handler in tuple(validator_logger.handlers):
                if handler not in handlers:
                    validator_logger.removeHandler(handler)
                    handler.close()
            validator_logger.setLevel(level)
            for logger in (term_logger, validator_logger):
                logger.removeFilter(quiet)
            _DIAGNOSTICS_ACTIVE.reset(token)


def shapes_graph(schema: str = "snapshot") -> Graph:
    """Load the same project-owned SHACL Core shapes from source or a wheel."""
    return Graph().parse(data=_shapes_bytes(schema).decode("utf-8"), format="turtle")


def _shapes_bytes(schema: str = "snapshot") -> bytes:
    if schema not in {"snapshot", "workspace"}:
        raise RDFValidationError("unknown_schema")
    name = "workspace_shapes.ttl" if schema == "workspace" else "knowledge_shapes.ttl"
    return files("knowledge_layer").joinpath("schemas", name).read_bytes()


def validate_graph(graph: Graph, *, schema: str = "snapshot") -> dict[str, Any]:
    """Validate a local graph without mutating it or deriving new business facts."""
    if len(graph) > MAX_TRIPLES:
        raise RDFValidationError("graph_too_large")
    try:
        from pyshacl import validate
    except ImportError as exc:
        raise RDFValidationError("semantic_validation_extra_required") from exc
    with _quiet_library_diagnostics():
        conforms, report, _text = validate(
            graph, shacl_graph=shapes_graph(schema), inference="none", advanced=False,
            js=False, do_owl_imports=False, iterate_rules=False, inplace=False,
            meta_shacl=False, max_validation_depth=16,
        )
    if not isinstance(report, Graph):
        raise RDFValidationError("validator_failure")
    violations = []
    for result in report.subjects(RDF.type, SH.ValidationResult):
        focus = report.value(result, SH.focusNode)
        path = report.value(result, SH.resultPath)
        component = report.value(result, SH.sourceConstraintComponent)
        # Do not expose offending values, source text or generated diagnostics.
        violations.append({
            "focus_node": str(focus)[:512] if isinstance(focus, URIRef) else None,
            "path": str(path) if isinstance(path, URIRef) else None,
            "constraint": str(component).removeprefix(str(SH)),
        })
    violations.sort(key=lambda item: (item["focus_node"] or "", item["path"] or "", item["constraint"]))
    return {
        "schema_version": "knowledge-workspace-rdf-shacl-v1" if schema == "workspace" else SCHEMA_VERSION,
        "shapes_sha256": hashlib.sha256(_shapes_bytes(schema)).hexdigest(),
        "result": "passed" if conforms else "failed", "conforms": bool(conforms),
        "triple_count": len(graph), "violation_count": len(violations),
        "violations": violations[:MAX_RESULTS],
        "violations_truncated": len(violations) > MAX_RESULTS,
        "inference": "none",
    }


def validate_turtle(raw: bytes, *, schema: str = "snapshot") -> dict[str, Any]:
    """Parse explicit Turtle bytes; references remain IRIs and are never fetched."""
    if len(raw) > MAX_INPUT_BYTES:
        raise RDFValidationError("input_too_large")
    try:
        with _quiet_library_diagnostics():
            graph = Graph().parse(data=raw.decode("utf-8"), format="turtle")
    except Exception as exc:  # parser errors may embed private input
        raise RDFValidationError("invalid_turtle") from exc
    return validate_graph(graph, schema=schema)


def _read_input(input_file: Path) -> bytes:
    descriptor = os.open(input_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RDFValidationError("input_not_regular")
        if before.st_size > MAX_INPUT_BYTES:
            raise RDFValidationError("input_too_large")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
        after = os.fstat(descriptor)
        if len(raw) > MAX_INPUT_BYTES:
            raise RDFValidationError("input_too_large")
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RDFValidationError("input_changed")
        return raw
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Explicit local Turtle export")
    parser.add_argument("--schema", choices=("snapshot", "workspace"), default="snapshot")
    args = parser.parse_args(argv)
    try:
        raw = _read_input(args.input)
        receipt = validate_turtle(raw, schema=args.schema)
    except (OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, RDFValidationError) else "input_unavailable"
        receipt = {"schema_version": SCHEMA_VERSION, "result": "refused", "conforms": None,
                   "code": code}
    except Exception:  # noqa: BLE001 - keep source/library errors out of CLI output
        receipt = {"schema_version": SCHEMA_VERSION, "result": "refused", "conforms": None,
                   "code": "validator_failure"}
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["result"] == "passed" else 1 if receipt["result"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
