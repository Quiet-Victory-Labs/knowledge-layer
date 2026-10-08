"""Measure a disposable API/UI + worker + PostgreSQL installation over real HTTP.

Synthetic, loopback demo credentials only. No customer data, IdP, cloud cost or
model. Uses a nonce Compose project and verifies ownership before removing it.
The caller must build the local image first; no services or images are borrowed.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup_registry import backup

SEED = r'''
import json, os
from sqlalchemy import text
from knowledge_layer.store import KnowledgeStore
store = KnowledgeStore(database_url=os.environ['DATABASE_URL'], initialize_schema=False,
                       local_files_root='/fixtures')
sources = []
for index in range(int(os.environ['MEASURE_SOURCES'])):
    source = store.create_connection('demo', kind='local_files', name=f'Synthetic {index}',
        config={'path':f'source_{index}', 'include':'*.csv',
                'max_files':int(os.environ['MEASURE_TABLES']), 'max_columns':10, 'profile_rows':4})
    refreshed = store.refresh_connection('demo', source['id'])
    snapshot = refreshed['snapshot']
    assert snapshot['discovery_complete'] is True
    assert len(snapshot['assets']) == int(os.environ['MEASURE_TABLES'])
    sources.append({'id':source['id'], 'term':snapshot['concepts'][0]['name']})
with store.engine.connect() as connection:
    db_bytes = connection.execute(text('SELECT pg_database_size(current_database())')).scalar_one()
print(json.dumps({'sources':sources, 'registry_database_bytes':db_bytes}))
store.close()
'''


def _run(*args: str, **kwargs) -> bytes:
    result = subprocess.run(args, capture_output=True, timeout=180, check=False, **kwargs)
    if result.returncode:
        raise RuntimeError("installation_command_failed")
    return result.stdout


def _latency(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {"requests": len(samples), "unit": "milliseconds", "percentile_method": "nearest_rank",
            "p50": ordered[math.ceil(len(samples) * .5) - 1],
            "p95": ordered[math.ceil(len(samples) * .95) - 1], "maximum": max(samples)}


def _request(base: str, token: str, path: str) -> tuple[dict, float]:
    start = monotonic()
    request = Request(base + path, headers={"Authorization": "Bearer " + token})
    with urlopen(request, timeout=20) as response:
        raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise RuntimeError("measurement_response_too_large")
    return json.loads(raw), (monotonic() - start) * 1000


def _stats(compose: list[str]) -> list[dict]:
    identifiers = _run(*compose, "ps", "-q").decode().splitlines()
    result = []
    for identifier in identifiers:
        stat = json.loads(_run("docker", "stats", "--no-stream", "--format", "{{json .}}", identifier))
        inspected = json.loads(_run("docker", "inspect", identifier))[0]
        result.append({"service": inspected["Config"]["Labels"]["com.docker.compose.service"],
                       "memory_usage_display": stat["MemUsage"], "cpu_percent_display": stat["CPUPerc"],
                       "pids": stat["PIDs"], "restarts": inspected["RestartCount"],
                       "memory_limit_bytes": inspected["HostConfig"]["Memory"],
                       "oom_killed": inspected["State"]["OOMKilled"]})
    return result


def measure(image: str, sources: int, tables: int, repetitions: int, concurrency: int,
            postgres_image: str | None = None) -> dict:
    image_info = json.loads(_run("docker", "image", "inspect", image))[0]
    postgres_info = json.loads(_run("docker", "image", "inspect", postgres_image or "postgres:18.6-alpine"))[0]
    project = "knowledge-measure-" + secrets.token_hex(6)
    label = "com.docker.compose.project=" + project
    began, timestamp = monotonic(), datetime.now(UTC).isoformat()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token, password = secrets.token_hex(32), secrets.token_hex(32)
    with tempfile.TemporaryDirectory(prefix=project + "-") as temporary:
        root = Path(temporary).resolve()
        root.chmod(0o700)
        fixtures = root / "fixtures"
        fixtures.mkdir(mode=0o755)
        # The Docker daemon needs to traverse the parent for bind mounts.
        for index in range(sources):
            directory = fixtures / f"source_{index}"
            directory.mkdir()
            for table in range(tables):
                with (directory / f"table_{table}.csv").open("w", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(["record_id"] + [f"measure_{n}" for n in range(1, 10)])
                    writer.writerows([[n] + [n * j for j in range(1, 10)] for n in range(1, 5)])
        database_url = f"postgresql+psycopg://knowledge_layer:{password}@postgres:5432/knowledge_layer"
        environment = {"DATABASE_URL": database_url, "KNOWLEDGE_AUTH_MODE": "demo",
                       "KNOWLEDGE_AGENT_TOKEN": token, "KNOWLEDGE_AGENT_SOURCE_MANAGEMENT": "1",
                       "KNOWLEDGE_ENABLED_KINDS": "local_files", "KNOWLEDGE_AGENT_ALLOWED_KINDS": "local_files",
                       "KNOWLEDGE_FIXTURE_ROOT": "/fixtures", "MEASURE_SOURCES": str(sources),
                       "MEASURE_TABLES": str(tables)}
        common = {"image": image_info["Id"], "mem_limit": "512m", "cpus": 1, "read_only": True,
                  "tmpfs": ["/tmp:size=64m,mode=1777"], "cap_drop": ["ALL"], "pids_limit": 128,
                  "environment": environment, "volumes": [f"{fixtures}:/fixtures:ro"]}
        config = {"name": project, "services": {
            "postgres": {"image": postgres_info["Id"], "mem_limit": "384m", "cpus": 1,
                "environment": {"POSTGRES_USER": "knowledge_layer", "POSTGRES_DB": "knowledge_layer",
                                "POSTGRES_PASSWORD": password},
                "tmpfs": ["/var/lib/postgresql:rw,size=256m"],
                "healthcheck": {"test": ["CMD", "pg_isready", "-U", "knowledge_layer", "-d", "knowledge_layer"],
                                "interval": "1s", "timeout": "3s", "retries": 30}},
            "api": {**common, "command": ["uvicorn", "knowledge_layer.platform:app", "--host", "0.0.0.0",
                                          "--port", "8000", "--no-access-log", "--limit-concurrency", "64",
                                          "--timeout-keep-alive", "5"],
                "ports": [f"127.0.0.1:{port}:8000"]},
            "worker": {**common, "command": ["python", "-m", "knowledge_layer.worker"]}}}
        if postgres_image:
            config["services"]["postgres"].update({"user": "10001:10001", "read_only": True,
                "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "pids_limit": 64,
                "tmpfs": ["/var/lib/postgresql:rw,size=256m,uid=10001,gid=10001,mode=0700",
                          "/var/run/postgresql:rw,size=8m,uid=10001,gid=10001,mode=0700",
                          "/tmp:rw,size=32m,mode=1777"]})
        compose_file = root / "compose.yaml"
        fd = os.open(compose_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream)
        compose = ["docker", "compose", "-f", str(compose_file), "-p", project]
        cleanup = False
        try:
            _run(*compose, "up", "-d", "--wait", "postgres")
            _run(*compose, "run", "--rm", "--no-deps", "api", "python", "-m", "knowledge_layer.migrate", "upgrade")
            start_api = monotonic()
            _run(*compose, "up", "-d", "api", "worker")
            base = f"http://127.0.0.1:{port}"
            deadline = monotonic() + 30
            while monotonic() < deadline:
                try:
                    _request(base, token, "/api/ready")
                    break
                except OSError:
                    sleep(.2)
            else:
                raise RuntimeError("installation_readiness_timeout")
            startup_ms = (monotonic() - start_api) * 1000
            idle = _stats(compose)
            refresh_start = monotonic()
            seeded = json.loads(_run(*compose, "exec", "-T", "api", "python", "-", input=SEED.encode()))
            refresh_seconds = monotonic() - refresh_start
            workload = seeded["sources"]

            def lookup(iteration):
                source = workload[iteration % len(workload)]
                search, search_ms = _request(base, token, "/agent/api/search?" + urlencode({"q": source["term"]}))
                context, context_ms = _request(base, token, "/agent/api/context?" + urlencode(
                    {"term": source["term"], "connection_id": source["id"]}))
                if not search.get("results") or not context.get("matches"):
                    raise RuntimeError("measured_lookup_has_no_fixture_hits")
                return search_ms, context_ms

            for index in range(sources):
                lookup(index)
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                pairs = list(executor.map(lookup, range(repetitions)))
            loaded = _stats(compose)
            if any(item["oom_killed"] or item["restarts"] for item in idle + loaded):
                raise RuntimeError("measured_service_unhealthy")
            # Stop only this nonce project's writers. Restore verification
            # compares all table rows against the same quiescent source.
            _run(*compose, "stop", "--timeout", "5", "api", "worker")
            recovered = backup(root, root / "backups", verify=True)
            report = {"schema_version": "knowledge-installation-measurement-v1", "started_at": timestamp,
                "finished_at": datetime.now(UTC).isoformat(),
                "runtime": {"host": platform.platform(), "docker_server": json.loads(_run("docker", "version", "--format", "{{json .Server}}"))["Version"],
                            "image_architecture": image_info["Architecture"], "auth_mode": "loopback_demo"},
                "image": {"id": image_info["Id"], "docker_reported_size_bytes": image_info["Size"]},
                "postgres_image": {"id": postgres_info["Id"], "docker_reported_size_bytes": postgres_info["Size"]},
                "fixture_scope": {"synthetic": True, "sources": sources, "assets": sources * tables,
                    "fields": sources * tables * 10, "rows_per_asset": 4, "approved_meaning": False,
                    "published_meaning": False, "registry_database_bytes": seeded["registry_database_bytes"]},
                "concurrency": concurrency, "http_requests_measured": repetitions * 2,
                "startup_to_ready_ms": startup_ms, "direct_refresh_seconds": refresh_seconds,
                "warm_http_search": _latency([p[0] for p in pairs]),
                "warm_http_context": _latency([p[1] for p in pairs]),
                "idle_container_samples": idle, "after_load_container_samples": loaded,
                "elapsed_seconds": monotonic() - began,
                "local_recovery": {key: recovered[key] for key in
                    ("bytes", "backup_seconds", "restore_seconds", "restore_verified",
                     "temporary_database_removed", "customer_rpo_rto_qualified")},
                "claim_scope": "Local Linux containers on this Docker host, synthetic scoped candidate metadata and real HTTP. "
                "Worker is idle; source refresh is direct and synchronous. No OIDC, MCP latency, approved semantic accuracy, "
                "cloud sizing/cost, peak refresh RSS, seven-day endurance or large-estate support is qualified.",
                "model_calls": 0, "customer_accounts": 0}
        finally:
            ids = _run("docker", "ps", "-aq", "--filter", "label=" + label).decode().splitlines()
            for identifier in ids:
                labels = json.loads(_run("docker", "inspect", "--format", "{{json .Config.Labels}}", identifier))
                if labels.get("com.docker.compose.project") != project:
                    raise RuntimeError("cleanup_ownership_unverified")
            _run(*compose, "down", "--volumes", "--remove-orphans", "--timeout", "5")
            if _run("docker", "ps", "-aq", "--filter", "label=" + label).strip():
                raise RuntimeError("cleanup_absence_unverified")
            cleanup = True
        report["temporary_services_removed"] = cleanup
        return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Existing local image reference")
    parser.add_argument("--postgres-image", help="Existing nonroot PostgreSQL image; use the shipped derivative")
    parser.add_argument("--sources", type=int, default=5)
    parser.add_argument("--tables-per-source", type=int, default=20)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if (not 1 <= args.sources <= 10 or not 1 <= args.tables_per_source <= 50
            or args.sources * args.tables_per_source > 200 or not 10 <= args.repetitions <= 500
            or not 1 <= args.concurrency <= 10 or args.output.exists() or args.output.is_symlink()
            or not args.output.parent.is_dir()):
        parser.error("Bounded workload or new output path required")
    try:
        report = measure(args.image, args.sources, args.tables_per_source, args.repetitions, args.concurrency,
                         args.postgres_image)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        parser.exit(2, "Installation measurement failed; check owned containers and private inputs\n")
    print(json.dumps({"report": str(args.output), "search_p95_ms": report["warm_http_search"]["p95"],
                      "context_p95_ms": report["warm_http_context"]["p95"],
                      "temporary_services_removed": report["temporary_services_removed"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
