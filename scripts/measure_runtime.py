#!/usr/bin/env python3
"""Measure bounded synthetic local registry work; no services, sources or models.

Reports warm lexical search and source-scoped candidate context separately from
refresh/setup. This is a local engineering measurement, not an enterprise SLO,
full installation footprint, customer authorization or cloud cost qualification.
"""

from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
import math
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledge_layer import retrieval
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeStore

CAPS = {"sources": 10, "tables_per_source": 50, "fields_per_table": 20,
        "rows_per_table": 100, "repetitions": 500, "warmup": 100,
        "total_files": 200, "total_fields": 4000, "total_rows": 20000,
        "total_fixture_bytes": 8 * 1024 * 1024}
OPTIONAL_PACKAGES = ("fastembed", "numpy", "pyiceberg", "pyarrow", "boto3", "clickhouse-connect")


def _arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in [("sources", 5), ("tables_per_source", 20), ("fields_per_table", 10),
                          ("rows_per_table", 8), ("repetitions", 100), ("warmup", 10)]:
        parser.add_argument("--" + name.replace("_", "-"), type=int, default=default)
    parser.add_argument("--output", type=Path, required=True,
                        help="New JSON artifact; refuses an existing file or symlink")
    args = parser.parse_args(argv)
    for name in CAPS.keys() - {"total_files", "total_fields", "total_rows", "total_fixture_bytes"}:
        if not 1 <= getattr(args, name) <= CAPS[name]:
            parser.error(f"{name} must be 1-{CAPS[name]}")
    files = args.sources * args.tables_per_source
    if (files > CAPS["total_files"] or files * args.fields_per_table > CAPS["total_fields"]
            or files * args.rows_per_table > CAPS["total_rows"]):
        parser.error("Combined synthetic fixture scope exceeds the absolute caps")
    if args.repetitions < args.sources or args.warmup < args.sources:
        parser.error("Repetitions and warmup must each visit every synthetic source")
    if args.output.exists() or args.output.is_symlink():
        parser.error("The output artifact already exists; it will not be overwritten")
    if not args.output.parent.is_dir():
        parser.error("The output artifact needs an existing parent directory")
    return parser, args


def _peak_rss():
    try:
        import resource
    except ImportError:
        return {"bytes": None, "raw_value": None, "raw_unit": None,
                "reason": "resource.getrusage is unavailable on this platform"}
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return {"bytes": int(raw), "raw_value": raw, "raw_unit": "bytes"}
    if sys.platform.startswith("linux"):
        return {"bytes": int(raw * 1024), "raw_value": raw, "raw_unit": "KiB"}
    return {"bytes": None, "raw_value": raw, "raw_unit": "platform_native_unknown",
            "reason": "No verified ru_maxrss unit conversion for this platform"}


def _packages():
    result = {}
    for name in OPTIONAL_PACKAGES:
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = None
        result[name] = {"installed_version": version,
                        "module_loaded": name.replace("-", "_") in sys.modules}
    return result


def _latency(samples):
    ordered = sorted(samples)
    return {"request_count": len(ordered), "unit": "milliseconds", "percentile_method": "nearest_rank",
            "p50": ordered[math.ceil(len(ordered) * .50) - 1],
            "p95": ordered[math.ceil(len(ordered) * .95) - 1],
            "minimum": ordered[0], "maximum": ordered[-1], "samples": samples}


def _fixtures(root, args):
    total = 0
    columns = ["record_id"] + [f"measure_{index:02}" for index in range(1, args.fields_per_table)]
    for source in range(args.sources):
        directory = root / f"source_{source:02}"
        directory.mkdir()
        for table in range(args.tables_per_source):
            path = directory / f"fixture_table_{table:02}.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(columns)
                for row in range(args.rows_per_table):
                    writer.writerow([f"S{source}T{table}R{row}"]
                                    + [(row + 1) * (index + 1) for index in range(1, args.fields_per_table)])
            total += path.stat().st_size
            if total > CAPS["total_fixture_bytes"]:
                raise ValueError("Synthetic files exceeded the absolute byte budget")
    return total


def measure(args):
    started = datetime.now(UTC).isoformat()
    initial_peak = _peak_rss()
    overall = time.perf_counter_ns()
    with TemporaryDirectory(prefix="knowledge-runtime-measure-") as temporary:
        temp_root = Path(temporary)
        source_root = temp_root / "fixtures"
        source_root.mkdir()
        begin_setup = time.perf_counter_ns()
        fixture_bytes = _fixtures(source_root, args)
        store = KnowledgeStore(database_url=f"sqlite:///{temp_root / 'registry.sqlite3'}",
                               local_files_root=source_root)
        try:
            sources, approvals = [], []
            for index in range(args.sources):
                selector = {"path": f"source_{index:02}", "include": "*.csv"}
                source = store.create_connection("runtime", kind="local_files", name=f"Synthetic source {index}",
                    config={**selector, "max_files": args.tables_per_source,
                            "max_columns": args.fields_per_table, "profile_rows": args.rows_per_table})
                subject = f"synthetic-reader-{index}"
                store.grant_source_access("runtime", source["id"], principal_type="user",
                                          principal_id=subject, role="read")
                approvals.append({"tenant_id": "runtime", "kind": "local_files", "selector": selector,
                                  "allowed_subjects": [subject], "allowed_groups": []})
                sources.append({"connection_id": source["id"], "subject": subject})
            setup_ms = (time.perf_counter_ns() - begin_setup) / 1e6
            refresh_samples, assets, fields, concepts, evidence_rows = [], 0, 0, 0, 0
            for source in sources:
                before = time.perf_counter_ns()
                result = store.refresh_connection("runtime", source["connection_id"])
                refresh_samples.append((time.perf_counter_ns() - before) / 1e6)
                snapshot = result["snapshot"]
                observed_assets = snapshot.get("assets") or []
                observed_concepts = snapshot.get("concepts") or []
                if (snapshot.get("discovery_complete") is not True
                        or len(observed_assets) != args.tables_per_source or not observed_concepts):
                    raise ValueError("Synthetic source did not completely discover the declared fixture scope")
                observed_fields = sum(len(asset.get("schema") or []) for asset in observed_assets)
                if observed_fields != args.tables_per_source * args.fields_per_table:
                    raise ValueError("Synthetic field count differs from the declared scope")
                assets += len(observed_assets)
                fields += observed_fields
                concepts += len(observed_concepts)
                evidence_rows += len(snapshot.get("evidence") or [])
                source["query"] = observed_concepts[0]["name"]
            store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": approvals}))
            store.access_enforced = True
            context_times, search_times = [], []
            observed_search_hits, observed_context_hits = [], []
            # Explicit lexical measurement; optional embeddings must never load or
            # download a model because it happens to be installed on the host.
            with patch.object(retrieval, "_semantic_scores", return_value=None), patch.object(
                retrieval, "_model", side_effect=RuntimeError("Model loading forbidden in runtime measurement")
            ) as forbidden_model:
                for iteration in range(args.warmup + args.repetitions):
                    source = sources[iteration % len(sources)]
                    principal = Principal(source["subject"], frozenset())
                    with principal_scope(principal):
                        before = time.perf_counter_ns()
                        search = retrieval.search_knowledge(store, tenant_id="runtime", query=source["query"], limit=5)
                        search_ms = (time.perf_counter_ns() - before) / 1e6
                        before = time.perf_counter_ns()
                        context = store.get_context("runtime", term=source["query"], connection_id=source["connection_id"])
                        context_ms = (time.perf_counter_ns() - before) / 1e6
                    search_hits = search.get("results") or []
                    context_hits = (context or {}).get("matches") or []
                    if not search_hits or not context_hits:
                        raise ValueError("A measured lookup returned no actual fixture hits")
                    if any(hit.get("connection_id") != source["connection_id"] for hit in search_hits):
                        raise ValueError("A measured search exceeded its one-source principal grant")
                    if context.get("connection_id") != source["connection_id"]:
                        raise ValueError("A measured context exceeded its explicit source selector")
                    if iteration >= args.warmup:
                        search_times.append(search_ms)
                        context_times.append(context_ms)
                        observed_search_hits.append(len(search_hits))
                        observed_context_hits.append(len(context_hits))
                if forbidden_model.call_count:
                    raise ValueError("A model was attempted; no model-free measurement is available")
            registry_bytes = (temp_root / "registry.sqlite3").stat().st_size
            peak = _peak_rss()
            optional = _packages()
        finally:
            store.close()
    return {
        "schema_version": "knowledge-local-runtime-measurement-v1", "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "runtime": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                    "platform": platform.platform(), "machine": platform.machine(),
                    "sqlite_registry": True, "postgresql_measured": False},
        "fixture_scope": {"synthetic": True, "sources": args.sources, "tables_per_source": args.tables_per_source,
            "fields_per_table": args.fields_per_table, "rows_per_table": args.rows_per_table,
            "fixture_bytes": fixture_bytes, "observed_assets": assets, "observed_fields": fields,
            "observed_candidate_concepts": concepts, "observed_evidence_rows": evidence_rows,
            "approved_meaning": False, "publication_performed": False, "temporary_registry_bytes": registry_bytes},
        "setup_ms": setup_ms, "refresh_ms": {"total": sum(refresh_samples), "per_source": refresh_samples},
        "warmup_request_count": args.warmup * 2, "measured_request_count": args.repetitions * 2,
        "warm_scoped_search": _latency(search_times), "warm_scoped_context": _latency(context_times),
        "hits": {"search_minimum": min(observed_search_hits), "context_minimum": min(observed_context_hits)},
        "authorization_scope": "Verified request principals, each granted exactly one synthetic connection; "
                               "source-policy and explicit context selector active during measured reads.",
        "peak_process_rss": peak, "initial_process_peak_rss": initial_peak,
        "optional_packages": optional, "semantic_reranking": "explicitly_disabled",
        "model_calls": 0, "model_downloads": 0, "services_started": 0, "customer_sources": 0,
        "temporary_directory_cleaned": True, "absolute_caps": CAPS,
        "overall_elapsed_ms": (time.perf_counter_ns() - overall) / 1e6,
        "claim_scope": "One synthetic, warm, direct Python SQLite/local_files measurement. No HTTP/MCP "
        "transport, PostgreSQL, customer/cloud runtime, optional embedding inference, large-estate scale "
        "or approved/published semantics qualified. Peak RSS covers this process lifetime, including "
        "imports/setup/refresh; it is not total installation footprint. Latency is not an enterprise SLO, "
        "and no cloud cost or model uplift is inferred.",
    }


def main(argv=None):
    parser, args = _arguments(argv)
    try:
        report = measure(args)
        report["artifact_path"] = str(args.output.resolve())
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(2, f"Runtime measurement refused: {exc}\n")
    print(json.dumps({"artifact_path": report["artifact_path"],
                      "measured_request_count": report["measured_request_count"],
                      "search_p95_ms": report["warm_scoped_search"]["p95"],
                      "context_p95_ms": report["warm_scoped_context"]["p95"],
                      "peak_process_rss_bytes": report["peak_process_rss"]["bytes"]}, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
