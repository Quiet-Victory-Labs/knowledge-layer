"""Exercise smoke acceptance and archive refusal; no distribution/install claims."""

from __future__ import annotations

import builtins
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, insert, update

from knowledge_layer.access import Principal
from knowledge_layer.adapters.conformance import check_inventory_page, check_response
from knowledge_layer.inventory import InventoryStore
from knowledge_layer.inventory_retention import InventoryRetention
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeError, KnowledgeStore

ROOT = Path(__file__).resolve().parents[1]
WHEEL_FILES = {
    "knowledge_layer/iap.py", "knowledge_layer/registry_bootstrap.py",
    "knowledge_layer/origin.py", "knowledge_layer/local_auth.py",
    "knowledge_layer/local_profile.py", "knowledge_layer/setup.py",
    "knowledge_layer/source_permissions.py", "knowledge_layer/connector_isolation.py",
    "knowledge_layer/connector_process.py", "knowledge_layer/source_io_audit.py",
    "knowledge_layer/refresh_authorization.py", "knowledge_layer/composite_metrics.py",
    "knowledge_layer/builds.py", "knowledge_layer/build_schema.py", "knowledge_layer/build_transport.py",
    "knowledge_layer/build_worker.py", "knowledge_layer/build_handoff.py", "knowledge_layer/review_batches.py",
    "knowledge_layer/migrations/versions/v0016_builds.py",
    "knowledge_layer/platform.py", "knowledge_layer/planning.py",
    "knowledge_layer/metric_semantics.py", "knowledge_layer/task_context.py",
    "knowledge_layer/inventory.py", "knowledge_layer/inventory_transport.py",
    "knowledge_layer/inventory_citations.py",
    "knowledge_layer/inventory_concepts.py", "knowledge_layer/inventory_concept_schema.py",
    "knowledge_layer/inventory_concept_search.py",
    "knowledge_layer/migrations/versions/v0015_inventory_concepts.py",
    "knowledge_layer/inventory_retention.py", "knowledge_layer/inventory_retention_cli.py",
    "knowledge_layer/inventory_refresh.py", "knowledge_layer/connectors/external_inventory.py",
    "knowledge_layer/adapters/conformance.py",
    "knowledge_layer/adapters/dbt.py", "knowledge_layer/adapters/datahub.py",
    "knowledge_layer/adapters/paging.py",
    "knowledge_layer/adapters/catalog.py", "knowledge_layer/adapters/catalog_mapping.py",
    "knowledge_layer/adapters/catalog_cli.py",
    "knowledge_layer/autonomy.py", "knowledge_layer/autonomy_schema.py", "knowledge_layer/autonomy_transport.py",
    "knowledge_layer/migrations/versions/v0013_autonomy.py",
    "knowledge_layer/migrations/versions/v0014_inventory_results.py",
    "knowledge_layer/migrations/versions/v0012_inventory.py",
    "knowledge_layer/schemas/ossie-schema-v0.2.0.dev0.json",
    "knowledge_layer/rdf_validation.py", "knowledge_layer/schemas/knowledge_shapes.ttl",
    "knowledge_layer-0.1.0.dist-info/licenses/LICENSE",
}
SOURCE_FILES = {
    "src/knowledge_layer/iap.py", "src/knowledge_layer/registry_bootstrap.py",
    "src/knowledge_layer/origin.py", "src/knowledge_layer/local_auth.py",
    "src/knowledge_layer/local_profile.py", "src/knowledge_layer/setup.py",
    "knowledge-layer", "scripts/self_host.py", "scripts/configure_agent.py", "docs/self-host.md",
    "scripts/self_host_recovery.py", "scripts/drill_self_host.py",
    "deploy/self-host/init.sql", "deploy/self-host/worker.override.example.yaml",
    "web/src/SetupAuthGate.tsx", "web/src/SetupGuide.tsx", "web/src/setup.css",
    "docs/iap-cloud-run.md", "docs/source-worker-isolation.md",
    "deploy/gcp/cloud-run.md", "deploy/gcp/cloud-run.env.yaml.example",
    "src/knowledge_layer/builds.py", "src/knowledge_layer/build_schema.py", "src/knowledge_layer/build_transport.py",
    "src/knowledge_layer/build_worker.py", "src/knowledge_layer/migrations/versions/v0016_builds.py",
    "LICENSE", "NOTICE", "README.md", "pyproject.toml", "uv.lock", "Dockerfile", "Dockerfile.postgres", "compose.yaml",
    "scripts/collect_release_notices.py", "scripts/fetch_release_archives.py",
    "scripts/fetch_publisher_notices.py", "docs/publisher-notices.json",
    "scripts/check_release_controls.py", "docs/ga-review-packet.md",
    "scripts/apply_runtime_patches.py", "third_party/runtime-patches/tempfile-rmtree-original.txt",
    "third_party/runtime-patches/CPython-LICENSE.txt", "third_party/runtime-patches/NOTICE.md",
    "examples/enterprise_consumer.py", "scripts/start.sh", "web/package-lock.json",
    "scripts/drill_autonomy.py", "docs/autonomy-policies.md", "data/connectable/juniper/products.csv",
    "scripts/check_adapter_contract.py", "docs/adapter-conformance.md",
    "examples/native_semantic_adapter.py", "examples/reference_adapter.py",
    "scripts/drill_native_semantic.py", "docs/native-semantic-adapters.md",
    "docs/catalog-agnostic-import.md",
    "docs/inventory-semantic-citations.md", "src/knowledge_layer/inventory_citations.py",
    "docs/inventory-concepts.md", "src/knowledge_layer/inventory_concepts.py",
    "src/knowledge_layer/inventory_concept_search.py",
    "src/knowledge_layer/inventory_concept_schema.py",
    "src/knowledge_layer/migrations/versions/v0015_inventory_concepts.py",
    "docs/inventory-retention.md",
    "docs/rdf-validation.md", "src/knowledge_layer/rdf_validation.py",
    "src/knowledge_layer/schemas/knowledge_shapes.ttl",
    "src/knowledge_layer/inventory_refresh.py", "src/knowledge_layer/connectors/external_inventory.py",
    "src/knowledge_layer/migrations/versions/v0014_inventory_results.py",
}


@pytest.fixture
def verifier():
    spec = importlib.util.spec_from_file_location("distribution_verifier", ROOT / "scripts/verify_distribution.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_base_smoke_acceptance_executes_synthetic_exact_proof_resolution(verifier, monkeypatch, capsys):
    # This intentionally declares the source tree as the unit fixture's import
    # root. Actual wheel/sdist verification uses the fresh runtime's sys.prefix.
    monkeypatch.setattr(sys, "prefix", str(ROOT))
    original_import = builtins.__import__

    def base_only_import(name, *args, **kwargs):
        if name == "pyshacl" or name.startswith("pyshacl."):
            raise ImportError("Optional validator is absent from the synthetic base runtime")
        return original_import(name, *args, **kwargs)

    # Contributor environments include all extras. Simulate absence only here;
    # the real verifier must still prove a fresh base install lacks the extra.
    monkeypatch.setattr(builtins, "__import__", base_only_import)
    exec(verifier.BASE_SMOKE, {})  # noqa: S102 - fixed repository program, no external input
    assert capsys.readouterr().out == version("knowledge-layer") + "\n"


def test_base_smoke_refuses_imports_outside_the_declared_runtime(verifier, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    with pytest.raises(AssertionError, match="not installed in isolated runtime"):
        exec(verifier.BASE_SMOKE, {})  # noqa: S102 - fixed repository program, no external input


@pytest.mark.parametrize("corruption", ["missing-proof", "wrong-manifest", "invented-version", "unlinked-accepted", "failed-source-accepted"])
def test_installed_citation_smoke_requires_exact_proof_and_current_source(verifier, monkeypatch, corruption):
    from knowledge_layer import inventory_citations

    monkeypatch.setattr(sys, "prefix", str(ROOT))
    original = inventory_citations.resolve_inventory_citation
    captured = None
    def changed(*args, **kwargs):
        nonlocal captured
        try:
            state, proof = original(*args, **kwargs)
        except KnowledgeError:
            evidence_id = args[2]["evidence_id"]
            if (corruption == "unlinked-accepted" and evidence_id == "unlinked.proof"
                    or corruption == "failed-source-accepted" and evidence_id == "synthetic.proof"):
                return captured
            raise
        captured = state, proof
        if corruption == "missing-proof":
            proof = {}
        elif corruption == "wrong-manifest":
            state["manifest_digest"] = "0" * 64
        elif corruption == "invented-version":
            state["entity"]["payload"]["native_version"] = 0
        return state, proof
    monkeypatch.setattr(inventory_citations, "resolve_inventory_citation", changed)
    with pytest.raises(AssertionError):
        exec(verifier.BASE_SMOKE, {})  # noqa: S102 - fixed repository program, no external input


@pytest.mark.parametrize("provider", ["databricks", "atlan"])
def test_native_drill_local_review_receipt_and_retained_head_invalidation_are_real_registry_effects(tmp_path, monkeypatch, provider):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("native_drill_fixture", ROOT / "scripts/drill_native_semantic.py")
    native_drill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native_drill)
    (tmp_path / "catalog").mkdir()
    owner = Principal("fixture-owner", frozenset({"fixture-stewards"}), frozenset({"knowledge.sources.manage"}))
    tenant, config = "fixture_native", {"path": "catalog", "include": "*.json"}
    policy = {"version": 1, "sources": [{"tenant_id": tenant, "kind": "local_files", "selector": config,
                                        "allowed_subjects": [owner.subject], "allowed_groups": []}]}
    policy_file = tmp_path / "policy.json"
    native_drill._private_json(policy_file, policy)
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    report, source_ids = {"checks": []}, []
    try:
        store.source_admin_group = "fixture-stewards"
        store.source_policy = SourcePolicy.from_json(json.dumps(policy))
        source = store.create_connection(tenant, kind="local_files", name="Synthetic catalog", config=config)
        source_ids.append(source["id"])
        store.grant_source_access(tenant, source["id"], principal_type="user", principal_id=owner.subject, role="manage")
        with native_drill._manager(store, owner):
            inventory = InventoryStore(store)
            run = inventory.begin(tenant, source["id"], scope={"namespace": "fixture", "selectors": ["fixture.term"]},
                policy={"id": "fixture", "revision": None}, watermark={"kind": "unknown", "value": None},
                limits={"max_pages": 1, "max_entities": 1, "max_evidence": 1, "max_total_bytes": 16384})
            run = inventory.append(tenant, source["id"], run["id"], page_index=0, cursor=None, next_cursor=None,
                entities=[{"id": "fixture.term", "kind": "semantic_definition", "payload": {"name": "Revenue"},
                           "evidence_ids": ["fixture.proof"]}],
                evidence=[{"id": "fixture.proof", "kind": "catalog_native_record", "payload": {"native_record": {"definition": "x" * 5000}}}],
                scope_digest=run["scope_digest"], policy_digest=run["policy_digest"], watermark_digest=run["watermark_digest"])
            run = inventory.commit(tenant, source["id"], run["id"], completion={"listing_complete": True,
                "pages": 1, "entities": 1, "evidence": 1, "manifest_digest": run["manifest_digest"]})
            selected = inventory.select(tenant, source["id"], run["id"], expected_manifest_digest=run["manifest_digest"], entity_ids=["fixture.term"])
        reviewed = native_drill._review_inventory_metric(report, store, tenant, owner, tmp_path, provider,
                                                         selected, source_ids, policy, policy_file)
        with store.engine.connect() as conn:
            before = [dict(row) for row in conn.execute(store.inventory_heads.select()).mappings()]
        with store.engine.begin() as conn:
            conn.execute(update(store.connections).where(store.connections.c.id == source["id"]).values(status="error"))
        native_drill._assert_inventory_metric_invalidated(report, store, tenant, owner, provider, reviewed)
        with store.engine.connect() as conn:
            assert [dict(row) for row in conn.execute(store.inventory_heads.select()).mappings()] == before
        assert len(report["checks"]) == 12 and all(check["passed"] for check in report["checks"])
        assert store.get_snapshot(tenant, source["id"]) is None
        assert len(source_ids) == 2
    finally:
        store.close()


@pytest.mark.parametrize("corruption", ["proof-missing", "native-version-invented", "text-clipped", "approved"])
def test_smoke_cannot_pass_lost_proof_text_unknowns_or_invented_authority(verifier, monkeypatch, corruption):
    monkeypatch.setattr(sys, "prefix", str(ROOT))
    original = InventoryStore.select

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if corruption == "proof-missing":
            result["evidence"] = []
        elif corruption == "native-version-invented":
            result["entities"][0]["payload"]["native_version"] = 0
        elif corruption == "text-clipped":
            result["entities"][0]["payload"]["definition"] = "Complete synthetic text."
        else:
            result["agent_ready"] = True
        return result

    monkeypatch.setattr(InventoryStore, "select", changed)
    with pytest.raises(AssertionError):
        exec(verifier.BASE_SMOKE, {})  # noqa: S102 - fixed repository program, no external input


@pytest.mark.parametrize("missing", ["knowledge_layer/adapters/conformance.py",
                                    "knowledge_layer/origin.py", "knowledge_layer/local_auth.py",
                                    "knowledge_layer/local_profile.py", "knowledge_layer/setup.py",
                                    "src/knowledge_layer/origin.py", "src/knowledge_layer/local_auth.py",
                                    "src/knowledge_layer/local_profile.py", "src/knowledge_layer/setup.py",
                                    "knowledge-layer", "scripts/self_host.py", "scripts/configure_agent.py",
                                    "scripts/self_host_recovery.py", "scripts/drill_self_host.py",
                                    "Dockerfile.postgres",
                                    "scripts/collect_release_notices.py", "scripts/fetch_release_archives.py",
                                    "scripts/fetch_publisher_notices.py", "docs/publisher-notices.json",
                                    "scripts/check_release_controls.py", "docs/ga-review-packet.md",
                                    "scripts/apply_runtime_patches.py", "third_party/runtime-patches/tempfile-rmtree-original.txt",
                                    "third_party/runtime-patches/CPython-LICENSE.txt", "third_party/runtime-patches/NOTICE.md",
                                    "docs/self-host.md", "deploy/self-host/init.sql",
                                    "deploy/self-host/worker.override.example.yaml",
                                    "web/src/SetupAuthGate.tsx", "web/src/SetupGuide.tsx", "web/src/setup.css",
                                    "knowledge_layer/adapters/catalog.py", "knowledge_layer/adapters/catalog_mapping.py",
                                    "knowledge_layer/adapters/catalog_cli.py", "docs/catalog-agnostic-import.md",
                                    "knowledge_layer/inventory_citations.py", "src/knowledge_layer/inventory_citations.py",
                                    "docs/inventory-semantic-citations.md",
                                    "knowledge_layer/adapters/paging.py", "knowledge_layer/adapters/dbt.py",
                                    "knowledge_layer/adapters/datahub.py", "examples/native_semantic_adapter.py",
                                    "examples/reference_adapter.py", "scripts/drill_native_semantic.py",
                                    "docs/native-semantic-adapters.md",
                                    "scripts/check_adapter_contract.py", "docs/adapter-conformance.md",
                                    "knowledge_layer/inventory_retention.py",
                                    "knowledge_layer/inventory_retention_cli.py", "docs/inventory-retention.md",
                                    "knowledge_layer/inventory_refresh.py",
                                    "knowledge_layer/connectors/external_inventory.py",
                                    "knowledge_layer/migrations/versions/v0014_inventory_results.py",
                                    "src/knowledge_layer/inventory_refresh.py",
                                    "src/knowledge_layer/connectors/external_inventory.py",
                                    "src/knowledge_layer/migrations/versions/v0014_inventory_results.py",
                                    "knowledge_layer/rdf_validation.py", "knowledge_layer/schemas/knowledge_shapes.ttl",
                                    "src/knowledge_layer/rdf_validation.py",
                                    "src/knowledge_layer/schemas/knowledge_shapes.ttl", "docs/rdf-validation.md"])
def test_missing_contributor_artifacts_refuse_before_install(verifier, monkeypatch, tmp_path, missing):
    # Tiny structural archive fixtures, not built or installed distributions.
    _archives(tmp_path, missing)

    def unexpected(*_args, **_kwargs):
        pytest.fail("Invalid archives must refuse before creating/installing a runtime")

    monkeypatch.setattr(verifier.subprocess, "run", unexpected)
    monkeypatch.setattr(sys, "argv", ["verify_distribution.py", "--dist", str(tmp_path)])
    with pytest.raises(RuntimeError, match="missing"):
        verifier.main()
    assert not (tmp_path / "verification.json").exists()
    assert not (tmp_path / "SHA256SUMS").exists()


def _archives(tmp_path, missing=None, *, launcher_mode=0o755):
    excluded = set() if missing is None else {missing}
    with zipfile.ZipFile(tmp_path / "knowledge_layer-0.1.0.whl", "w") as archive:
        for name in WHEEL_FILES - excluded:
            archive.writestr(name, "synthetic fixture")
    with tarfile.open(tmp_path / "knowledge_layer-0.1.0.tar.gz", "w:gz") as archive:
        for name in SOURCE_FILES - excluded:
            content = b"synthetic fixture"
            info = tarfile.TarInfo("knowledge_layer-0.1.0/" + name)
            info.size = len(content)
            info.mode = launcher_mode if name == "knowledge-layer" else 0o644
            archive.addfile(info, io.BytesIO(content))


def test_nonexecutable_source_launcher_refuses_before_install(verifier, monkeypatch, tmp_path):
    _archives(tmp_path, launcher_mode=0o600)
    monkeypatch.setattr(verifier.subprocess, "run", lambda *a, **k: pytest.fail("Refuse before installation"))
    monkeypatch.setattr(sys, "argv", ["verify_distribution.py", "--dist", str(tmp_path)])
    with pytest.raises(RuntimeError, match="executable regular file"):
        verifier.main()
    assert not (tmp_path / "verification.json").exists()


@pytest.mark.parametrize("corruption", ["deletion-not-executed", "audit-lost", "source-clock-written",
                                        "legacy-snapshot-written", "wrong-count", "source-io-claimed"])
def test_smoke_refuses_retention_without_actual_deletion_audit_or_boundary_preservation(
        verifier, monkeypatch, corruption):
    monkeypatch.setattr(sys, "prefix", str(ROOT))
    original = InventoryRetention.apply

    def changed(self, tenant, source, **options):
        if corruption == "deletion-not-executed":
            preview_options = {key: value for key, value in options.items() if key != "expected_preview_digest"}
            result = self.preview(tenant, source, **preview_options)
            return {**result, "operation": "apply", "status": "applied", "registry_writes_executed": True}
        result = original(self, tenant, source, **options)
        if corruption == "wrong-count":
            return {**result, "counts": {**result["counts"], "total_rows": 0}}
        if corruption == "source-io-claimed":
            return {**result, "source_writes_executed": True}
        with self.store.engine.begin() as conn:
            if corruption == "audit-lost":
                conn.execute(delete(self.store.audit_events).where(
                    self.store.audit_events.c.event_type == "inventory.retention"))
            elif corruption == "source-clock-written":
                conn.execute(update(self.store.connections).where(self.store.connections.c.id == source)
                             .values(last_complete_refresh_at="2026-01-01T00:00:00Z"))
            else:
                conn.execute(insert(self.store.snapshots).values(
                    row_id="synthetic-unexpected", tenant_id=tenant, connection_id=source,
                    version=1, snapshot_id="synthetic-unexpected", status="candidate",
                    created_at="2026-01-01T00:00:00Z", payload={}))
        return result

    monkeypatch.setattr(InventoryRetention, "apply", changed)
    with pytest.raises(AssertionError):
        exec(verifier.BASE_SMOKE, {})  # noqa: S102 - fixed repository program, no external input


@pytest.mark.parametrize("name", ["inventory_retention", "inventory_retention_cli"])
def test_retention_import_provenance_cannot_be_bypassed(verifier, monkeypatch, tmp_path, name):
    from knowledge_layer import inventory_retention, inventory_retention_cli

    monkeypatch.setattr(sys, "prefix", str(ROOT))
    module = {"inventory_retention": inventory_retention, "inventory_retention_cli": inventory_retention_cli}[name]
    monkeypatch.setattr(module, "__file__", str(tmp_path / "unexpected.py"))
    with pytest.raises(AssertionError, match="not installed in isolated runtime"):
        exec(verifier.BASE_SMOKE, {})  # noqa: S102 - fixed repository program, no external input


@pytest.mark.parametrize("help_mode", ["complete", "incomplete", "failure"])
def test_packaged_retention_help_gates_receipt_fields_without_sqlite_cli_execution(
        verifier, monkeypatch, tmp_path, help_mode):
    # These archives and subprocess responses test verifier control flow only;
    # they do not claim an installed distribution. BASE_SMOKE runs separately.
    _archives(tmp_path)
    calls = _mock_runtime(verifier, monkeypatch, help_mode=help_mode)
    monkeypatch.setattr(sys, "argv", ["verify_distribution.py", "--dist", str(tmp_path)])
    if help_mode == "complete":
        assert verifier.main() == 0
        receipt = json.loads((tmp_path / "verification.json").read_text())
        for key in ("wheel_inventory_retention", "source_inventory_retention",
                    "source_inventory_retention_cli_help", "wheel_inventory_selection",
                    "source_inventory_selection", "source_adapter_conformance_cli",
                    "wheel_inventory_page_conformance", "source_inventory_page_conformance",
                    "source_inventory_page_conformance_cli", "wheel_native_semantic_inventory",
                    "source_native_semantic_inventory", "wheel_catalog_export_mapping_cli",
                    "source_catalog_export_mapping_cli", "wheel_inventory_citations", "source_inventory_citations",
                    "wheel_optional_shacl", "source_optional_shacl"):
            assert receipt[key] == "passed"
        assert receipt["package_version"] == version("knowledge-layer")
        assert receipt["signed"] is False and receipt["published"] is False
    else:
        with pytest.raises(RuntimeError if help_mode == "incomplete" else subprocess.CalledProcessError):
            verifier.main()
        assert not (tmp_path / "verification.json").exists()
    assert sum("knowledge_layer.inventory_retention_cli" in command for command in calls) == 1


def test_optional_validation_smoke_executes_valid_and_invalid_export_checks(verifier):
    pytest.importorskip("pyshacl")
    exec(verifier.SHACL_SMOKE, {})  # noqa: S102 - fixed repository program, no external input


def test_optional_smoke_rejects_validator_that_accepts_an_invalid_export(verifier, monkeypatch):
    from knowledge_layer import rdf_validation

    monkeypatch.setattr(rdf_validation, "validate_graph", lambda _graph: {"result": "passed", "conforms": True})
    with pytest.raises(AssertionError):
        exec(verifier.SHACL_SMOKE, {})  # noqa: S102 - fixed repository program, no external input


@pytest.mark.parametrize("failed_smoke", [1, 2])
def test_failed_optional_wheel_or_source_smoke_cannot_write_a_receipt(
        verifier, monkeypatch, tmp_path, failed_smoke):
    _archives(tmp_path)
    calls = _mock_runtime(verifier, monkeypatch, failed_shacl_smoke=failed_smoke)
    monkeypatch.setattr(sys, "argv", ["verify_distribution.py", "--dist", str(tmp_path)])
    with pytest.raises(subprocess.CalledProcessError):
        verifier.main()
    assert sum(command[-1] == verifier.SHACL_SMOKE for command in calls) == failed_smoke
    assert not (tmp_path / "verification.json").exists()
    assert not (tmp_path / "SHA256SUMS").exists()


def test_optional_smoke_follows_both_base_installs_and_reinstalled_source(
        verifier, monkeypatch, tmp_path):
    _archives(tmp_path)
    calls = _mock_runtime(verifier, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["verify_distribution.py", "--dist", str(tmp_path)])
    assert verifier.main() == 0
    qualification = [command for command in calls
                     if command[:3] == ["uv", "pip", "install"]
                     or command[-1] in (verifier.BASE_SMOKE, verifier.SHACL_SMOKE)]
    assert len(qualification) == 8
    assert qualification[0][-1] == str(tmp_path / "knowledge_layer-0.1.0.whl")
    assert qualification[1][-1] == verifier.BASE_SMOKE
    assert qualification[2][-3:] == ["--no-deps", "--force-reinstall",
                                     str(tmp_path / "knowledge_layer-0.1.0.tar.gz")]
    assert qualification[3][-1] == verifier.BASE_SMOKE
    assert qualification[4][-3:] == ["--reinstall-package", "knowledge-layer",
                                     str(tmp_path / "knowledge_layer-0.1.0.whl") + "[semantic-validation]"]
    assert qualification[5][-1] == verifier.SHACL_SMOKE
    assert qualification[6][-3:] == qualification[2][-3:]
    assert qualification[7][-1] == verifier.SHACL_SMOKE


def _mock_runtime(verifier, monkeypatch, *, help_mode="complete", paged_change=None, failed_shacl_smoke=None):
    """Control subprocess outcomes, without claiming a built or installed package."""
    calls = []
    shacl_smokes = 0

    def run(command, **options):
        nonlocal shacl_smokes
        calls.append(command)
        if "knowledge_layer.inventory_retention_cli" in command:
            assert command[-3:] == ["-m", "knowledge_layer.inventory_retention_cli", "--help"]
            assert "-I" in command and options["check"] is True
            if help_mode == "failure":
                raise subprocess.CalledProcessError(1, command)
            stdout = "--created-before --apply --expected-preview-digest" if help_mode == "complete" else "--apply"
        elif command[-1] == verifier.BASE_SMOKE:
            stdout = version("knowledge-layer") + "\n"
        elif command[-1] == verifier.SHACL_SMOKE:
            assert command[-3:] == ["-I", "-c", verifier.SHACL_SMOKE]
            assert options["check"] is True and options["capture_output"] is True
            assert options["cwd"] != str(ROOT)
            assert not any(key.startswith(("KNOWLEDGE_", "DATABASE_URL", "PYTHONPATH"))
                           for key in options["env"])
            shacl_smokes += 1
            if shacl_smokes == failed_shacl_smoke:
                raise subprocess.CalledProcessError(1, command)
            stdout = ""
        elif any(str(item).endswith("/enterprise_consumer.py") for item in command):
            stdout = json.dumps({"result": "passed", "model_calls": 0})
        elif "--inventory-request" in command:
            assert "-I" in command and options["check"] is True and options["timeout"] == 30
            assert command[command.index("--protocol-version") + 1] == "1.3"
            assert options["cwd"] != str(ROOT)
            assert not any(key.startswith(("KNOWLEDGE_", "DATABASE_URL", "PYTHONPATH"))
                           for key in options["env"])
            request_path = Path(command[command.index("--inventory-request") + 1])
            page_path = Path(command[command.index("--input") + 1])
            controls, page = verifier._inventory_fixture()
            assert json.loads(request_path.read_bytes()) == controls
            assert json.loads(page_path.read_bytes()) == page
            result = check_inventory_page(page_path.read_bytes(), controls)
            if paged_change is not None:
                result = paged_change(result)
            stdout = json.dumps(result)
        elif any(str(item).endswith("/check_adapter_contract.py") for item in command):
            stdout = json.dumps(check_response(json.dumps({
                "protocol_version": "1.2", "listing_complete": True, "assets": [{
                    "name": "Synthetic", "pointer": "tables/synthetic", "schema": [{"name": "id"}]}],
            }).encode(), provider="synthetic", scope="readings", protocol_version="1.2"))
        else:
            stdout = ""
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr(verifier.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("name", ["external_inventory", "inventory_refresh", "inventory_results_migration",
                                 "catalog", "catalog_mapping", "catalog_cli", "inventory_citations"])
def test_paged_module_and_migration_import_provenance_cannot_be_bypassed(
        verifier, monkeypatch, tmp_path, name):
    from knowledge_layer import inventory_citations, inventory_refresh
    from knowledge_layer.adapters import catalog, catalog_cli, catalog_mapping
    from knowledge_layer.connectors import external_inventory
    from knowledge_layer.migrations.versions import v0014_inventory_results

    monkeypatch.setattr(sys, "prefix", str(ROOT))
    module = {"external_inventory": external_inventory, "inventory_refresh": inventory_refresh,
              "inventory_results_migration": v0014_inventory_results,
              "catalog": catalog, "catalog_mapping": catalog_mapping, "catalog_cli": catalog_cli,
              "inventory_citations": inventory_citations}[name]
    monkeypatch.setattr(module, "__file__", str(tmp_path / "unexpected.py"))
    with pytest.raises(AssertionError, match="not installed in isolated runtime"):
        exec(verifier.BASE_SMOKE, {})  # noqa: S102 - fixed repository program


@pytest.mark.parametrize("field,value", [("revision", "0013_autonomy"), ("down_revision", "0012_inventory")])
def test_smoke_requires_exact_current_migration_lineage(verifier, monkeypatch, field, value):
    from knowledge_layer.migrations.versions import v0014_inventory_results

    monkeypatch.setattr(sys, "prefix", str(ROOT))
    monkeypatch.setattr(v0014_inventory_results, field, value)
    with pytest.raises(AssertionError):
        exec(verifier.BASE_SMOKE, {})  # noqa: S102 - fixed repository program


@pytest.mark.parametrize("corruption", ["count", "full-page", "inventory-authority", "clock-authority",
                                       "watermark-invented", "source-io", "publication", "private-text"])
def test_smoke_refuses_false_page_conformance_or_unsupported_qualification(verifier, monkeypatch, corruption):
    from knowledge_layer.adapters import conformance

    monkeypatch.setattr(sys, "prefix", str(ROOT))
    original = conformance.check_inventory_page

    def changed(*args, **kwargs):
        return _corrupt_page_receipt(original(*args, **kwargs), corruption)

    monkeypatch.setattr(conformance, "check_inventory_page", changed)
    with pytest.raises(AssertionError):
        exec(verifier.BASE_SMOKE, {})  # noqa: S102 - fixed repository program


def _corrupt_page_receipt(result, corruption):
    if corruption == "count":
        result["counts"]["entities"] = 0
    elif corruption == "full-page":
        result["listing"]["full_page_validated"] = False
    elif corruption == "inventory-authority":
        result["listing"]["inventory_complete_verified"] = True
    elif corruption == "clock-authority":
        result["scan"]["clock_live_validated"] = True
    elif corruption == "watermark-invented":
        result["watermark"]["state"] = "declared"
    elif corruption == "source-io":
        result["execution"]["network_calls"] = 1
    elif corruption == "publication":
        result["qualification"]["publication"] = True
    else:
        result["private_text"] = "Complete synthetic page text."
    return result


@pytest.mark.parametrize("corruption", ["native-version-invented", "zero-lost", "text-clipped"])
def test_smoke_asserts_actual_pure_page_validation_preserves_payload(verifier, monkeypatch, corruption):
    from knowledge_layer.connectors import external_inventory

    monkeypatch.setattr(sys, "prefix", str(ROOT))
    original = external_inventory.validate_inventory_page

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        payload = result["entities"][0]["payload"]
        if corruption == "native-version-invented":
            payload["native_version"] = 0
        elif corruption == "zero-lost":
            payload["observed_zero"] = None
        else:
            payload["text"] = "Complete synthetic"
        return result

    monkeypatch.setattr(external_inventory, "validate_inventory_page", changed)
    with pytest.raises(AssertionError):
        exec(verifier.BASE_SMOKE, {})  # noqa: S102 - fixed repository program


@pytest.mark.parametrize("corruption", ["count", "full-page", "inventory-authority", "clock-authority",
                                       "watermark-invented", "source-io", "publication", "private-text"])
def test_packaged_page_cli_receipt_must_pass_all_evidence_gates(
        verifier, monkeypatch, tmp_path, corruption):
    _archives(tmp_path)
    _mock_runtime(verifier, monkeypatch, paged_change=lambda result: _corrupt_page_receipt(result, corruption))
    monkeypatch.setattr(sys, "argv", ["verify_distribution.py", "--dist", str(tmp_path)])
    with pytest.raises(RuntimeError, match="inventory page conformance"):
        verifier.main()
    assert not (tmp_path / "verification.json").exists()
    assert not (tmp_path / "SHA256SUMS").exists()
