"""Qualify a fresh portable owner installation through real HTTP and native MCP.

Uses the standard library, a caller-built image, synthetic files, and a nonce
Compose project. No model, customer system, cloud resource, or existing service
is used. Credentials and database files remain in private temporary storage.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

ROOT = Path(__file__).resolve().parents[1]
MAX_RESPONSE = 512 * 1024
DESCRIPTION = "Synthetic order records with an order identifier and observed total."
MIXED_DESCRIPTIONS = {
    "orders.csv": DESCRIPTION,
    "products.json": "Synthetic products with a product identifier, category, and listed price.",
    "events.jsonl": "Synthetic order events with an event identifier and order reference.",
    "policy.md": "Synthetic order policy defining the order identifier and total.",
    "glossary.txt": "Synthetic glossary defining an order and a product.",
    "handbook.html": "Synthetic handbook describing the order review process.",
    "terms.pdf": "Synthetic PDF stating the order review target.",
    "runbook.docx": "Synthetic runbook describing order review ownership.",
}


def _write_text_pdf(path: Path) -> None:
    """Write one real, extractable PDF without adding a drill dependency."""
    content = b"BT /F1 12 Tf 20 150 Td (Order review target is within 2 days.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets = [0]
    for number, item in enumerate(objects, 1):
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n".encode() + item + b"\nendobj\n")
    xref = output.tell()
    output.write(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(output.getvalue())


def _write_text_docx(path: Path) -> None:
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                     '</Types>')
    relationships = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                     '</Relationships>')
    document = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Order review runbook</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>The order steward reviews the proposed description.</w:t></w:r></w:p>'
                '</w:body></w:document>')
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)


def _write_mixed_fixture(folder: Path) -> None:
    (folder / "orders.csv").write_text("order_id,total\nO-1,12\nO-2,0\n", encoding="utf-8")
    (folder / "products.json").write_text(json.dumps([
        {"product_id": "P-1", "category": "book", "listed_price": 12},
        {"product_id": "P-2", "category": "tool", "listed_price": 20},
    ]), encoding="utf-8")
    (folder / "events.jsonl").write_text(
        '{"event_id":"E-1","order_id":"O-1","event_type":"created"}\n'
        '{"event_id":"E-2","order_id":"O-2","event_type":"reviewed"}\n', encoding="utf-8")
    (folder / "policy.md").write_text("# Orders\nAn order has an order identifier and total.\n", encoding="utf-8")
    (folder / "glossary.txt").write_text("# Glossary\nAn order is a recorded purchase. A product is a listed item.\n", encoding="utf-8")
    (folder / "handbook.html").write_text("<!doctype html><html><body><h1>Order handbook</h1><p>The steward reviews each proposed description.</p></body></html>", encoding="utf-8")
    _write_text_pdf(folder / "terms.pdf")
    _write_text_docx(folder / "runbook.docx")


def _latency(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {"requests": len(ordered), "unit": "milliseconds", "p50": round(ordered[(len(ordered) - 1) // 2], 2),
            "p95": round(ordered[(95 * len(ordered) + 99) // 100 - 1], 2), "maximum": round(ordered[-1], 2)}


class RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a private cookie or bearer to another URL.


class Client:
    def __init__(self, origin: str, *, token: str | None = None):
        self.origin, self.token, self.csrf = origin, token, None
        self.opener = build_opener(ProxyHandler({}), RefuseRedirect(),
                                   HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.sequence = 0
        self.protocol = "2025-11-25"

    def request(self, path: str, *, body=None, expected=200, headers=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("relative_installation_route_required")
        values = {"Accept": "application/json"}
        if self.token:
            values["Authorization"] = "Bearer " + self.token
        if body is not None:
            values.update({"Content-Type": "application/json", "Origin": self.origin})
            if self.csrf:
                values["X-Knowledge-CSRF"] = self.csrf
        values.update(headers or {})
        request = Request(self.origin + path, headers=values,
                          data=None if body is None else json.dumps(body).encode())
        try:
            response = self.opener.open(request, timeout=15)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(MAX_RESPONSE + 1)
            if response.status != expected or len(raw) > MAX_RESPONSE:
                raise RuntimeError("unexpected_http_result_or_budget")
        return json.loads(raw) if raw else {}

    def rpc(self, method: str, params: dict):
        self.sequence += 1
        result = self.request("/agent/mcp/", body={"jsonrpc": "2.0", "id": self.sequence,
            "method": method, "params": params}, headers={
                "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": self.protocol})
        if result.get("id") != self.sequence or "error" in result or "result" not in result:
            raise RuntimeError("native_mcp_request_failed")
        return result["result"]

    def initialize(self):
        result = self.rpc("initialize", {"protocolVersion": self.protocol, "capabilities": {},
            "clientInfo": {"name": "knowledge-layer-self-host-qualification", "version": "1.0"}})
        version = result.get("protocolVersion")
        if not isinstance(version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", version):
            raise RuntimeError("native_mcp_protocol_missing")
        self.protocol = version
        self.request("/agent/mcp/", body={"jsonrpc": "2.0", "method": "notifications/initialized"},
                     expected=202, headers={"Accept": "application/json, text/event-stream",
                                            "MCP-Protocol-Version": self.protocol})

    def tool(self, name: str, **arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError("native_mcp_tool_failed")
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        content = result.get("content", [])
        if len(content) != 1 or content[0].get("type") != "text":
            raise RuntimeError("native_mcp_tool_payload_missing")
        return json.loads(content[0]["text"])


def command(arguments: list[str], *, timeout=60) -> str:
    result = subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("qualification_command_failed")
    return result.stdout


def cli(install: Path, *arguments: str) -> str:
    return command([sys.executable, "-I", "-S", str(ROOT / "scripts/self_host.py"),
                    "--install-dir", str(install), *arguments], timeout=300)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def check(report: dict, name: str, passed: bool):
    report["checks"].append({"name": name, "passed": bool(passed)})
    if not passed:
        raise RuntimeError("qualification_check_failed")


def poll_job(owner: Client, job_id: str):
    deadline = monotonic() + 90
    while monotonic() < deadline:
        job = owner.request("/api/knowledge/jobs/" + job_id)["job"]
        if job["status"] == "succeeded":
            return
        if job["status"] in {"failed", "cancelled"}:
            raise RuntimeError("qualification_refresh_failed")
        sleep(0.25)
    raise RuntimeError("qualification_refresh_deadline")


def login(origin: str, password: str) -> Client:
    owner = Client(origin)
    response = owner.request("/auth/login", body={"name": "Synthetic owner", "password": password})
    owner.csrf = response["csrf_token"]
    return owner


def qualify(image: str, report: dict, postgres_image: str | None = None, *, mixed_corpus: bool = False):
    image_id = json.loads(command(["docker", "image", "inspect", image]))[0]["Id"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise ValueError("local_image_identity_required")
    report["image_id"] = image_id
    postgres_id = None
    if postgres_image:
        postgres_id = json.loads(command(["docker", "image", "inspect", postgres_image]))[0]["Id"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", postgres_id):
            raise ValueError("local_postgres_image_identity_required")
        report["postgres_image_id"] = postgres_id
    aliases, installations = [], []
    private = Path(tempfile.mkdtemp(prefix="knowledge-self-host-qualification-")).resolve()
    try:
        private.chmod(0o700)
        install, port = private / "original", free_port()
        origin = f"http://127.0.0.1:{port}"
        password = secrets.token_hex(24)
        try:
            report["stage"] = "initialize"
            cli(install, "init", "--port", str(port))
            config = json.loads((install / "installation.json").read_text())
            installations.append((install, config))
            alias = "knowledge-layer-local:" + config["project"]
            command(["docker", "image", "tag", image_id, alias])
            aliases.append(alias)
            if postgres_id:
                alias = "knowledge-layer-postgres:" + config["project"]
                command(["docker", "image", "tag", postgres_id, alias])
                aliases.append(alias)
            fixture = install / "sources" / "synthetic"
            fixture.mkdir(mode=0o700)
            if mixed_corpus:
                _write_mixed_fixture(fixture)
            else:
                (fixture / "orders.csv").write_text("order_id,total\nO-1,12\nO-2,0\n", encoding="utf-8")
                (fixture / "policy.md").write_text("# Orders\nAn order has an order identifier and total.\n", encoding="utf-8")
            for item in fixture.iterdir():
                item.chmod(0o600)
            report["stage"] = "start"
            start_began = monotonic()
            cli(install, "start", "--no-build")
            report["startup_to_ready_ms"] = round((monotonic() - start_began) * 1000, 2)
            check(report, "private_default_files", install.stat().st_mode & 0o077 == 0
                  and all(item.stat().st_mode & 0o077 == 0 for item in (install / "secrets").iterdir()))
            owner = Client(origin)
            check(report, "fresh_owner_claim_required", owner.request("/auth/status")["bootstrap_required"] is True)
            owner.request("/api/setup", expected=401)
            report["stage"] = "claim_owner"
            claimed = owner.request("/auth/setup", body={"name": "Synthetic owner", "password": password,
                "bootstrap_token": (install / "secrets/bootstrap_token").read_text()})
            owner.csrf = claimed["csrf_token"]
            check(report, "owner_setup_and_secret_separation", password not in
                  (install / "state/auth/local-auth.json").read_text())
            source_body = {"kind": "local_files", "name": "Synthetic orders and documentation",
                           "config": {"path": "synthetic", "include": "**/*"}}
            owner.request("/api/knowledge/connections", body=source_body, expected=403,
                          headers={"X-Knowledge-CSRF": "invalid"})
            owner.request("/api/knowledge/connections", body=source_body, expected=403,
                          headers={"Origin": "https://untrusted.test"})
            owner.request("/api/knowledge/connections", body={"kind": "postgres", "name": "Unapproved",
                          "config": {}}, expected=403)
            check(report, "origin_csrf_and_disabled_source_kind_refused",
                  owner.request("/api/knowledge/connections")["total"] == 0)
            source = owner.request("/api/knowledge/connections", body={"kind": "local_files",
                "name": "Synthetic orders and documentation", "config": {"path": "synthetic", "include": "**/*"}},
                expected=201)["connection"]["id"]
            refresh_began = monotonic()
            job = owner.request(f"/api/knowledge/connections/{source}/refresh", body={}, expected=202)["job"]
            report["stage"] = "worker_discovery"
            poll_job(owner, job["id"])
            report["refresh_to_completed_ms"] = round((monotonic() - refresh_began) * 1000, 2)
            snapshot = owner.request(f"/api/knowledge/connections/{source}")["snapshot"]
            check(report, "real_worker_profiles_structured_and_document_files", snapshot["discovery_complete"] is True
                  and len(snapshot["assets"]) == (len(MIXED_DESCRIPTIONS) if mixed_corpus else 2)
                  and snapshot["agent_ready"] is False)
            if mixed_corpus:
                assets = {item["name"]: item for item in snapshot["assets"]}
                expected_tables = {"orders.csv", "products.json", "events.jsonl"}
                check(report, "mixed_files_have_correct_types_and_extracted_evidence",
                      set(assets) == set(MIXED_DESCRIPTIONS)
                      and all(assets[name]["asset_type"] == ("table" if name in expected_tables else "document")
                              for name in assets)
                      and all(assets[name]["schema"] for name in expected_tables)
                      and all(assets[name]["profile"].get("extraction_status") == "extracted"
                              for name in set(assets) - expected_tables)
                      and not snapshot["errors"])
                report["fixture_scope"] = {"synthetic": True, "files": sorted(assets),
                    "table_assets": len(expected_tables), "document_assets": len(assets) - len(expected_tables),
                    "source_kind": "local_files"}
            agent = Client(origin, token=(install / "secrets/agent_token").read_text())
            agent.request("/agent/api/sources", body=source_body, expected=405)
            agent.initialize()
            tools = agent.rpc("tools/list", {})["tools"]
            names = {item["name"] for item in tools}
            check(report, "native_mcp_has_build_without_human_approval_tools", "build_knowledge" in names
                  and not any("publish" in name or "approve" in name or "accept" in name for name in names))
            report["stage"] = "client_configuration"
            for name in ("codex", "claude", "cursor"):
                project = private / ("client-" + name)
                project.mkdir(mode=0o700)
                cli(install, "agent", "--client", name, "--project", str(project))
                check(report, name + "_configuration_contains_no_credentials", all(
                    agent.token not in path.read_text() and password not in path.read_text()
                    for path in project.rglob("*") if path.is_file()))
            report["stage"] = "durable_agent_build"
            build_began = monotonic()
            max_items = 200 if mixed_corpus else 32
            run = agent.tool("build_knowledge", operation="start", arguments={"connection_ids": [source],
                "max_items": max_items, "max_seconds": 1200})["build"]
            task = agent.tool("build_knowledge", operation="claim", arguments={"run_id": run["id"]})["task"]
            check(report, "bounded_build_captures_complete_fixture_work", run["coverage"]["complete"] is True and task is not None)
            report["stage"] = "leased_build_stop"
            cli(install, "stop")
            report["stage"] = "leased_build_restart"
            cli(install, "start", "--no-build")
            report["stage"] = "leased_build_reconnect"
            agent.initialize()
            owner = login(origin, password)
            proposal_ids, asset_ids, abstained = [], [], 0
            for _ in range(max_items):
                if task is None:
                    break
                packet = task["packet"]
                result = {"action": "abstain", "reason": "This fixture does not establish additional business meaning."}
                if packet["track"] == "contract" and packet["kind"] == "dataset_description":
                    name = packet["asset"]["name"]
                    description = MIXED_DESCRIPTIONS[name] if mixed_corpus else (
                        DESCRIPTION if name.endswith("orders.csv") else "Synthetic order documentation.")
                    result = {"action": "propose", "reason": "The exact stored file evidence supports this source description.",
                              "evidence_ids": packet["evidence_ids"], "payload": {"description": description}}
                else:
                    abstained += 1
                arguments = {"run_id": run["id"], "task_id": task["id"], "lease_token": task["lease_token"], "result": result}
                report["stage"] = "agent_checkpoint"
                receipt = agent.tool("build_knowledge", operation="complete", arguments=arguments)
                report["stage"] = "agent_checkpoint_replay"
                replay = agent.tool("build_knowledge", operation="complete", arguments=arguments)
                check(report, "checkpoint_replay_" + str(len(proposal_ids) + abstained), replay["replayed"] is True
                      and replay.get("proposal_id") == receipt.get("proposal_id"))
                if receipt.get("proposal_id"):
                    proposal_ids.append(receipt["proposal_id"])
                    asset_ids.append(packet["asset_id"])
                task = agent.tool("build_knowledge", operation="claim", arguments={"run_id": run["id"]})["task"]
            report["agent_build_to_completed_ms"] = round((monotonic() - build_began) * 1000, 2)
            check(report, "agent_drafts_remain_pending_with_honest_abstention",
                  len(proposal_ids) == (len(MIXED_DESCRIPTIONS) if mixed_corpus else 2) and abstained > 0
                  and task is None and owner.request(f"/api/knowledge/connections/{source}")["snapshot"]["agent_ready"] is False)
            review = {"proposal_ids": proposal_ids, "expected_snapshot_id": snapshot["id"],
                      "expected_snapshot_version": snapshot["version"]}
            prefix = f"/api/knowledge/connections/{source}/review-batch"
            preview = owner.request(prefix + "/preview", body=review)
            acceptance = review | {"expected_review_digest": preview["review_digest"], "publish": True,
                                   "rationale": "The synthetic owner reviewed these exact supported descriptions."}
            agent.request(prefix + "/accept", body=acceptance, expected=401)
            agent.request(f"/api/knowledge/connections/{source}/publication", body=acceptance, expected=401)
            owner.request(prefix + "/accept", body=acceptance | {"expected_review_digest": "0" * 64}, expected=409)
            check(report, "agent_and_changed_review_cannot_publish", owner.request(
                f"/api/knowledge/connections/{source}")["snapshot"]["id"] == snapshot["id"])
            report["stage"] = "human_review_publish"
            publish_began = monotonic()
            published = owner.request(prefix + "/accept", body=acceptance)
            report["review_publish_ms"] = round((monotonic() - publish_began) * 1000, 2)
            check(report, "human_publishes_exact_selection", published["status"] == "published"
                  and published["reviewed_proposal_ids"] == proposal_ids)
            contracts = [agent.tool("get_contract", connection_id=source, asset_id=asset) for asset in asset_ids]
            check(report, "native_agent_reads_reviewed_published_pins", all(item["agent_ready"] is True
                  and item["snapshot_id"] == published["snapshot_id"] and item["assertions"]["approved"] for item in contracts))
            if mixed_corpus:
                samples = []
                for _ in range(20):
                    began = monotonic()
                    result = agent.tool("get_contract", connection_id=source, asset_id=asset_ids[0])
                    if not result["agent_ready"] or not result["assertions"]["approved"]:
                        raise RuntimeError("published_contract_missing_during_measurement")
                    samples.append((monotonic() - began) * 1000)
                report["warm_native_mcp_contract"] = _latency(samples)
                search_samples, context_samples = [], []
                term = snapshot["concepts"][0]["name"]
                search_path = "/agent/api/search?" + urlencode({"q": term})
                context_path = "/agent/api/context?" + urlencode({"term": term, "connection_id": source})
                for _ in range(20):
                    began = monotonic()
                    search = agent.request(search_path)
                    search_samples.append((monotonic() - began) * 1000)
                    began = monotonic()
                    context = agent.request(context_path)
                    context_samples.append((monotonic() - began) * 1000)
                    if not search.get("results") or not context.get("matches"):
                        raise RuntimeError("mixed_corpus_lookup_has_no_evidence")
                report["warm_http_search"] = _latency(search_samples)
                report["warm_http_context"] = _latency(context_samples)
            report["stage"] = "publication_restart"
            cli(install, "stop")
            cli(install, "start", "--no-build")
            owner = login(origin, password)
            agent.initialize()
            check(report, "owner_and_accepted_context_survive_restart", contracts == [
                agent.tool("get_contract", connection_id=source, asset_id=asset) for asset in asset_ids])
            report["stage"] = "cold_backup_restore"
            previous_owner = owner
            cli(install, "stop")
            bundle = private / "recovery.tar"
            cli(install, "backup", "--output", str(bundle))
            recovered, recovery_port = private / "recovered", free_port()
            cli(recovered, "restore", "--backup", str(bundle), "--port", str(recovery_port))
            config = json.loads((recovered / "installation.json").read_text())
            installations.append((recovered, config))
            alias = "knowledge-layer-local:" + config["project"]
            command(["docker", "image", "tag", image_id, alias])
            aliases.append(alias)
            cli(recovered, "start", "--no-build")
            recovery_origin = f"http://127.0.0.1:{recovery_port}"
            previous_owner.origin = recovery_origin
            previous_owner.request("/auth/me", expected=401)
            check(report, "restoration_invalidates_pre_recovery_browser_session",
                  previous_owner.request("/auth/status")["authenticated"] is False)
            owner = login(recovery_origin, password)
            agent = Client(recovery_origin, token=(recovered / "secrets/agent_token").read_text())
            agent.initialize()
            check(report, "complete_cold_restore_preserves_owner_and_publication_pins", contracts == [
                agent.tool("get_contract", connection_id=source, asset_id=asset) for asset in asset_ids])
            check(report, "restored_project_identity_is_separate",
                  config["project"] != installations[0][1]["project"])
            owner.request(f"/api/knowledge/connections/{source}/publication/withdraw", body={
                "reason": "Synthetic owner withdraws this publication for the release qualification."})
            check(report, "withdrawal_immediately_removes_agent_readiness", all(
                agent.tool("get_contract", connection_id=source, asset_id=asset)["agent_ready"] is False for asset in asset_ids))
            owner.request("/auth/logout", body={})
            owner.request("/api/setup", expected=401)
            check(report, "owner_logout_revokes_browser_authority", owner.request("/auth/status")["authenticated"] is False)
        finally:
            report["stage_before_cleanup"] = report["stage"]
            report["stage"] = "cleanup"
            clean = True
            for path, config in reversed(installations):
                try:
                    cli(path, "remove", "--purge")
                    remaining = command(["docker", "ps", "--all", "--quiet", "--filter",
                                         "label=com.docker.compose.project=" + config["project"]]).strip()
                    clean = clean and not remaining and not path.exists()
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    clean = False
            for alias in aliases:
                try:
                    command(["docker", "image", "rm", alias])
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    clean = False
            if clean:
                shutil.rmtree(private)
            else:
                report["private_cleanup_directory"] = str(private)
            check(report, "all_owned_containers_credentials_data_and_image_aliases_removed", clean)
    except BaseException:
        # A pre-initialization failure has no service to preserve. Later cleanup
        # failures keep the private directory for the operator's exact retry.
        if not installations and private.exists():
            shutil.rmtree(private)
        raise


def write_report(path: Path, report: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Existing image built from the checkout being qualified")
    parser.add_argument("--postgres-image", required=True, help="Existing nonroot image built with Dockerfile.postgres")
    parser.add_argument("--output", type=Path, required=True, help="New private result file, never overwritten")
    parser.add_argument("--mixed-corpus", action="store_true",
                        help="Exercise CSV, JSON, JSONL, Markdown, text, HTML, PDF, and DOCX in the owner flow")
    args = parser.parse_args(argv)
    began = monotonic()
    report = {"schema_version": "self-host-qualification-v1", "result": "running", "checks": [],
              "started_at": datetime.now(UTC).isoformat(), "stage": "preflight", "model_invoked": False,
              "cloud_resources_created": False, "mixed_corpus": args.mixed_corpus,
              "drill_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("new_report_path_required")
        report["source_checkout"] = command(["git", "rev-parse", "HEAD"], timeout=10).strip()
        report["checkout_dirty"] = bool(command(["git", "status", "--porcelain"], timeout=10).strip())
        qualify(args.image, report, args.postgres_image, mixed_corpus=args.mixed_corpus)
        report["result"] = "passed"
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        report["result"] = "failed"
        # Arbitrary driver/HTTP/subprocess diagnostics can contain credentials.
    report["elapsed_seconds"] = round(monotonic() - began, 3)
    try:
        write_report(args.output, report)
    except (OSError, ValueError, TypeError):
        print(json.dumps({"result": "failed", "stage": "report_write"}))
        return 1
    print(json.dumps({"result": report["result"], "checks": len(report["checks"]), "stage": report["stage"],
                      "report": str(args.output)}))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
