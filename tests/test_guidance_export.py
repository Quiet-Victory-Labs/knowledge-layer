"""Portable exports preserve meaning and installers preserve customer workspaces."""

import hashlib
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path, PurePosixPath

import pytest

from knowledge_layer.guidance_export import GuidanceExportError, export_guidance


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@pytest.fixture
def resolution():
    payload = {
        "title": "Sales evidence review", "description": "Use current company guidance to qualify a sales analysis.",
        "business_units": ["synthetic-analytics"], "tasks": ["sales-analysis"],
        "audience": {"company_wide": True, "subjects": [], "groups": []},
        "connection_ids": ["conn-warehouse"],
        "skills": [{"name": "sales-review", "description": 'Review sales evidence: "current" sources.\nUse before advice.',
                    "instructions": "Read current evidence.\nPreserve missing, zero, empty and checked-none.\nExact text: £ • café.\n",
                    "required_capabilities": ["knowledge_context", "read_only_sql", "result_validation"]}],
        "rules": [{"key": "missingness", "statement": "Preserve missing values.\nNever substitute zero.", "value": False},
                  {"key": "cutoff", "statement": "Use the observed native cutoff.", "value": None}],
        "dependencies": [{"connection_id": "conn-warehouse", "snapshot_id": "snapshot-17", "snapshot_version": 3}],
        "effective_from": "2026-10-01T00:00:00.000000Z", "effective_until": None,
    }
    return {"status": "ready", "blocked": [], "conflicts": [],
            "context": {"business_unit": "synthetic-analytics", "task": "sales-analysis", "connection_ids": ["conn-warehouse"]},
            "evaluated_at": "2026-10-03T12:00:00.000000Z",
            "bundles": [{"slug": "synthetic-analytics", "revision": 2, "digest": _digest(payload), "payload": payload,
                         "state": "published", "dependency_state": "current", "published_revision": 2,
                         "created_at": "2026-10-02T12:00:00.000000Z", "created_by": "steward",
                         "publication": {"published_at": "2026-10-02T13:00:00.000000Z", "published_by": "steward",
                                         "rationale": "Reviewed as procedural company guidance.", "revoked_at": None,
                                         "revoked_by": None, "revoke_reason": None}}]}


def _revise(resolution):
    for bundle in resolution["bundles"]:
        bundle["digest"] = _digest(bundle["payload"])


def test_portable_export_is_exact_deterministic_and_does_not_mutate_resolution(resolution):
    before = deepcopy(resolution)
    result = export_guidance(resolution)
    assert resolution == before and result == export_guidance(deepcopy(resolution))
    manifest = result["manifest"]
    assert manifest["enforcement"] == "guidance_only" and manifest["revalidate_before_use"] is True
    assert manifest["context"] == resolution["context"] and manifest["evaluated_at"] == resolution["evaluated_at"]
    record = manifest["bundles"][0]
    assert record["payload"] == resolution["bundles"][0]["payload"]
    assert record["digest"] == resolution["bundles"][0]["digest"] and record["revision"] == 2
    assert record["dependencies"] == record["payload"]["dependencies"]
    assert record["payload"]["connection_ids"] == ["conn-warehouse"]  # Applicability is separate from dependencies.
    assert record["publication"] == resolution["bundles"][0]["publication"]
    skill_path = ".agents/skills/kl-synthetic-analytics-sales-review/SKILL.md"
    text = result["files"][skill_path]
    frontmatter = text.split("---\n", 2)[1]
    assert "name: kl-synthetic-analytics-sales-review\n" in frontmatter
    description = next(line[len("description: "):] for line in frontmatter.splitlines() if line.startswith("description: "))
    assert json.loads(description) == record["payload"]["skills"][0]["description"]  # JSON strings are valid YAML scalars.
    assert "## Instructions\n\n" + record["payload"]["skills"][0]["instructions"] + "\n\n" in text
    for rule in record["payload"]["rules"]:
        assert rule["statement"] in text
    assert '```json\nfalse\n```' in text and '```json\nnull\n```' in text
    reference = result["files"][".agents/skills/kl-synthetic-analytics-sales-review/references/company-guidance.md"]
    assert "snapshot-17" in reference and '"snapshot_version": 3' in reference
    assert "live registry" in reference and "stop" in reference
    for path, content in result["files"].items():
        assert PurePosixPath(path).is_absolute() is False and ".." not in PurePosixPath(path).parts
        assert manifest["files"][path] == {"sha256": hashlib.sha256(content.encode()).hexdigest(), "bytes": len(content.encode())}
        assert path.endswith(".md") and "allowed-tools:" not in content
    assert not any(path.endswith(("AGENTS.md", "CLAUDE.md", ".json", ".toml", ".py", ".sh", ".mdc")) for path in result["files"])


@pytest.mark.parametrize(("client", "prefix"), [("portable", ".agents"), ("codex", ".agents"),
                                              ("claude", ".claude"), ("cursor", ".cursor")])
def test_client_only_changes_document_placement(resolution, client, prefix):
    result = export_guidance(resolution, client=client)
    assert f"{prefix}/skills/kl-synthetic-analytics-sales-review/SKILL.md" in result["files"]
    assert result["manifest"]["client"] == client
    assert result["manifest"]["bundles"][0]["payload"] == resolution["bundles"][0]["payload"]


@pytest.mark.parametrize("problem", ["blocked", "empty", "conflict", "draft", "stale", "revoked",
                                     "superseded", "wrong_digest", "future", "expired", "naive_clock"])
def test_refuses_noncurrent_or_conflicted_resolution(resolution, problem):
    bundle = resolution["bundles"][0]
    if problem in ("blocked", "empty"):
        resolution["status"] = problem
    elif problem == "conflict":
        resolution["conflicts"] = [{"key": "missingness", "values": []}]
    elif problem == "draft":
        bundle["state"] = "draft"
    elif problem == "stale":
        bundle["dependency_state"] = "stale"
    elif problem == "revoked":
        bundle["publication"]["revoked_at"] = "2026-10-03T10:00:00Z"
    elif problem == "superseded":
        bundle["published_revision"] = 1
    elif problem == "wrong_digest":
        bundle["payload"]["skills"][0]["instructions"] += "Changed after publication."
    elif problem == "future":
        bundle["payload"]["effective_from"] = "2026-10-04T00:00:00Z"
        _revise(resolution)
    elif problem == "expired":
        bundle["payload"]["effective_until"] = resolution["evaluated_at"]
        _revise(resolution)
    else:
        resolution["evaluated_at"] = "2026-10-03T12:00:00"
    with pytest.raises(GuidanceExportError):
        export_guidance(resolution)


def test_independently_detects_conflicting_rules_and_never_silently_omits_them(resolution):
    other = deepcopy(resolution["bundles"][0])
    other["slug"] = "enterprise"
    other["payload"]["rules"][0]["value"] = True
    resolution["bundles"].append(other)
    _revise(resolution)
    with pytest.raises(GuidanceExportError, match="Conflicting company rules"):
        export_guidance(resolution)


def test_full_rules_from_every_applicable_bundle_appear_in_each_skill(resolution):
    other = deepcopy(resolution["bundles"][0])
    other["slug"] = "enterprise"
    other["payload"]["rules"] = [{"key": "results", "statement": "Validate observed results independently.", "value": "required"}]
    resolution["bundles"].append(other)
    _revise(resolution)
    result = export_guidance(resolution)
    for path, text in result["files"].items():
        if path.endswith("SKILL.md"):
            assert "Validate observed results independently." in text and "Never substitute zero." in text


@pytest.mark.parametrize(("first", "second"), [(1, 1.0), (False, False), ("required", "required")])
def test_equivalent_rule_values_preserve_each_exact_statement(resolution, first, second):
    resolution["bundles"][0]["payload"]["rules"][0]["value"] = first
    other = deepcopy(resolution["bundles"][0])
    other["slug"] = "enterprise"
    other["payload"]["rules"][0].update(value=second, statement="Different exact phrasing of the same declared rule value.")
    resolution["bundles"].append(other)
    _revise(resolution)
    result = export_guidance(resolution)
    for path, text in result["files"].items():
        if path.endswith("SKILL.md"):
            assert "Never substitute zero." in text
            assert "Different exact phrasing of the same declared rule value." in text


@pytest.mark.parametrize(("first", "second"), [(True, 1), ("1", 1), (None, "null")])
def test_rule_conflicts_keep_boolean_string_numeric_and_absence_types_distinct(resolution, first, second):
    resolution["bundles"][0]["payload"]["rules"][0]["value"] = first
    other = deepcopy(resolution["bundles"][0])
    other["slug"] = "enterprise"
    other["payload"]["rules"][0]["value"] = second
    resolution["bundles"].append(other)
    _revise(resolution)
    with pytest.raises(GuidanceExportError, match="Conflicting company rules"):
        export_guidance(resolution)


@pytest.mark.parametrize("slug", ["true", "null", "123"])
def test_yaml_metadata_values_are_strings_even_for_yaml_literal_bundle_names(resolution, slug):
    resolution["bundles"][0]["slug"] = slug
    result = export_guidance(resolution)
    text = next(content for path, content in result["files"].items() if path.endswith("SKILL.md"))
    line = next(line.strip() for line in text.splitlines() if "knowledge-layer-bundle:" in line)
    assert json.loads(line.split(": ", 1)[1]) == slug


@pytest.mark.parametrize("separator", ["\x85", "\u2028", "\u2029"])
def test_yaml_description_preserves_unicode_separators_and_emoji(resolution, separator):
    description = "Research " + separator + " published guidance 😀."
    resolution["bundles"][0]["payload"]["skills"][0]["description"] = description
    _revise(resolution)
    result = export_guidance(resolution)
    text = next(content for path, content in result["files"].items() if path.endswith("SKILL.md"))
    line = next(line for line in text.splitlines() if line.startswith("description: "))
    assert json.loads(line[len("description: "):]) == description
    assert separator not in line


@pytest.mark.parametrize("name", ["../escape", "x/y", "bad\x00name", "UPPER", "x--y", "-x", "café"])
@pytest.mark.parametrize("target", ["bundle", "skill"])
def test_refuses_unsafe_or_non_spec_names(resolution, name, target):
    if target == "bundle":
        resolution["bundles"][0]["slug"] = name
    else:
        resolution["bundles"][0]["payload"]["skills"][0]["name"] = name
        _revise(resolution)
    with pytest.raises(GuidanceExportError):
        export_guidance(resolution)


def test_long_names_are_bounded_namespaced_and_collision_resistant(resolution):
    bundle = resolution["bundles"][0]
    bundle["slug"] = "a" * 64
    bundle["payload"]["skills"][0]["name"] = "b" * 64
    other = deepcopy(bundle["payload"]["skills"][0])
    other["name"] = "b" * 63 + "c"
    bundle["payload"]["skills"].append(other)
    _revise(resolution)
    result = export_guidance(resolution)
    names = [PurePosixPath(path).parent.name for path in result["files"] if path.endswith("SKILL.md")]
    assert len(set(names)) == 2 and all(len(name) <= 64 and "--" not in name for name in names)


@pytest.mark.parametrize("problem", ["capability", "allowed_tools", "credentials", "secret_text", "duplicate_skill", "duplicate_bundle"])
def test_refuses_tool_escalation_credentials_and_collisions(resolution, problem):
    bundle = resolution["bundles"][0]
    skill = bundle["payload"]["skills"][0]
    if problem == "capability":
        skill["required_capabilities"] = ["Bash(*)"]
    elif problem == "allowed_tools":
        skill["allowed-tools"] = "Bash(*)"
    elif problem == "credentials":
        bundle["payload"]["credentials"] = {"api_key": "private-value"}  # pragma: allowlist secret - synthetic rejected credential fixture
    elif problem == "secret_text":
        skill["instructions"] = "Authorization: Bearer private-value"
    elif problem == "duplicate_skill":
        bundle["payload"]["skills"].append(deepcopy(skill))
    else:
        resolution["bundles"].append(deepcopy(bundle))
    _revise(resolution)
    with pytest.raises(GuidanceExportError):
        export_guidance(resolution)


def test_overbudget_export_refuses_without_truncating_text(resolution, monkeypatch):
    monkeypatch.setattr("knowledge_layer.guidance_export.MAX_EXPORT_BYTES", 5000)
    with pytest.raises(GuidanceExportError, match="budget"):
        export_guidance(resolution)


def test_rules_only_guidance_retains_scalar_absence_zero_and_empty(resolution):
    payload = resolution["bundles"][0]["payload"]
    payload["skills"] = []
    payload["rules"] = [{"key": "zero", "statement": "Observed zero is distinct.", "value": 0},
                        {"key": "empty", "statement": "Observed empty string is distinct.", "value": ""}]
    _revise(resolution)
    result = export_guidance(resolution)
    assert set(result["files"]) == {"knowledge-layer-guidance/BOOTSTRAP.md"}
    assert result["manifest"]["bundles"][0]["payload"] == payload


def test_actual_registry_sample_resolution_preserves_omitted_source_selector_and_long_slug():
    from knowledge_layer.guidance import GuidanceRegistry
    from knowledge_layer.store import KnowledgeStore

    sample = Path(__file__).resolve().parents[1] / "examples/company-guidance-synthetic.json"
    payload = json.loads(sample.read_text(encoding="utf-8"))
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        registry = GuidanceRegistry(store)
        slug = "synthetic-analytics-" + "a" * 40  # Combined bundle/skill namespace needs the portable hash suffix.
        row = registry.draft("demo", slug, payload)
        registry.publish("demo", slug, revision=row["revision"], expected_digest=row["digest"],
                         rationale="Reviewed this procedural template without asserting source facts.")
        current = registry.resolve("demo", business_unit="synthetic-analytics", task="sales-analysis")
        assert current["status"] == "ready" and current["context"]["connection_ids"] is None
        result = export_guidance(current)
        assert result["manifest"]["context"] == current["context"]
        assert result["manifest"]["bundles"][0]["payload"] == current["bundles"][0]["payload"]
        assert result["manifest"]["bundles"][0]["slug"] == slug
        assert result["manifest"]["bundles"][0]["dependencies"] == []
        for path, text in result["files"].items():
            if path.endswith("SKILL.md"):
                assert len(PurePosixPath(path).parent.name) <= 64
                assert payload["skills"][0]["instructions"] in text
    finally:
        store.close()


def test_actual_registry_numeric_rule_normalization_and_exact_phrasing():
    from knowledge_layer.guidance import GuidanceRegistry
    from knowledge_layer.store import KnowledgeStore

    sample = Path(__file__).resolve().parents[1] / "examples/company-guidance-synthetic.json"
    payload = json.loads(sample.read_text(encoding="utf-8"))
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        registry = GuidanceRegistry(store)
        for slug, value, statement in (("first", 1, "First exact numeric rule."), ("second", 1.0, "Second exact numeric rule.")):
            current = deepcopy(payload)
            current["rules"] = [{"key": "review-count", "statement": statement, "value": value}]
            row = registry.draft("demo", slug, current)
            registry.publish("demo", slug, revision=row["revision"], expected_digest=row["digest"],
                             rationale="Reviewed both equivalent declarations.")
        current = registry.resolve("demo", business_unit="synthetic-analytics", task="sales-analysis")
        assert current["status"] == "ready" and current["conflicts"] == []
        result = export_guidance(current)
        for path, text in result["files"].items():
            if path.endswith("SKILL.md"):
                assert "First exact numeric rule." in text and "Second exact numeric rule." in text
    finally:
        store.close()


@pytest.fixture
def installer(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts" / "install_agent_skill.py"
    spec = importlib.util.spec_from_file_location("installer_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source" / "knowledge-layer"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: knowledge-layer\ndescription: Connect approved sources.\n---\nRead exact evidence.\n")
    monkeypatch.setattr(module, "SOURCE", source)
    project = tmp_path / "project"
    project.mkdir()
    return module, source, project


def _install(installer, monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["install_agent_skill.py", *args])
    return installer.main()


@pytest.mark.parametrize(("client", "directory"), [("portable", ".agents"), ("codex", ".agents"),
                                                 ("claude", ".claude"), ("cursor", ".cursor")])
def test_installer_placement_idempotence_and_existing_user_rules(installer, monkeypatch, client, directory):
    module, source, project = installer
    (project / "AGENTS.md").write_text("Customer rules stay here.")
    (project / "CLAUDE.md").write_text("Customer Claude rules stay here.")
    args = ("--client", client, "--project", str(project))
    assert _install(module, monkeypatch, *args) == 0
    target = project / directory / "skills" / "knowledge-layer"
    assert (target / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
    assert _install(module, monkeypatch, *args) == 0
    assert list(target.parent.glob("*.backup-*")) == []
    assert (project / "AGENTS.md").read_text() == "Customer rules stay here."
    assert (project / "CLAUDE.md").read_text() == "Customer Claude rules stay here."


def test_installer_explicit_destination_and_backup_preserve_customer_content(installer, monkeypatch):
    module, source, project = installer
    target = project / "custom" / "knowledge-layer"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("My own skill instructions.")
    (target / "customer.txt").write_text("Preserve this too.")
    args = ("--client", "portable", "--destination", str(target))
    with pytest.raises(SystemExit) as exc:
        _install(module, monkeypatch, *args)
    assert exc.value.code == 2 and (target / "SKILL.md").read_text() == "My own skill instructions."
    assert _install(module, monkeypatch, *args, "--replace") == 0
    backup, = target.parent.glob("knowledge-layer.backup-*")
    assert (backup / "SKILL.md").read_text() == "My own skill instructions."
    assert (backup / "customer.txt").read_text() == "Preserve this too."
    assert (target / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()


def test_installer_rejects_symlink_ancestor_without_touching_external_directory(installer, monkeypatch, tmp_path):
    module, _, project = installer
    external = tmp_path / "external"
    external.mkdir()
    (project / ".agents").symlink_to(external, target_is_directory=True)
    with pytest.raises(SystemExit):
        _install(module, monkeypatch, "--client", "portable", "--project", str(project), "--replace")
    assert list(external.iterdir()) == [] and (project / ".agents").is_symlink()


def test_installer_replaces_only_the_target_symlink_and_backs_it_up(installer, monkeypatch, tmp_path):
    module, source, project = installer
    external = tmp_path / "external"
    external.mkdir()
    (external / "SKILL.md").write_text("External skill stays untouched.")
    target = project / "custom" / "knowledge-layer"
    target.parent.mkdir()
    target.symlink_to(external, target_is_directory=True)
    assert _install(module, monkeypatch, "--client", "portable", "--destination", str(target), "--replace") == 0
    backup, = target.parent.glob("knowledge-layer.backup-*")
    assert backup.is_symlink() and backup.resolve() == external
    assert (external / "SKILL.md").read_text() == "External skill stays untouched."
    assert not target.is_symlink() and (target / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()


def test_copy_failure_does_not_move_existing_skill(installer, monkeypatch):
    module, _, project = installer
    target = project / "knowledge-layer"
    target.mkdir()
    (target / "SKILL.md").write_text("Customer original.")

    def failed_copy(*args, **kwargs):
        raise OSError("Copy failed")

    monkeypatch.setattr(module.shutil, "copytree", failed_copy)
    with pytest.raises(OSError, match="Copy failed"):
        _install(module, monkeypatch, "--client", "portable", "--destination", str(target), "--replace")
    assert (target / "SKILL.md").read_text() == "Customer original."
    assert list(project.glob("*.backup-*")) == []


@pytest.mark.parametrize("destination", ["../knowledge-layer", "different-name"])
def test_installer_rejects_traversal_or_mismatched_name(installer, monkeypatch, destination):
    module, _, project = installer
    with pytest.raises(SystemExit):
        _install(module, monkeypatch, "--client", "portable", "--destination", str(project / destination))


def test_installer_refuses_linked_source_files(installer, monkeypatch, tmp_path):
    module, source, project = installer
    external = tmp_path / "private.txt"
    external.write_text("External data.")
    (source / "private.txt").symlink_to(external)
    with pytest.raises(SystemExit):
        _install(module, monkeypatch, "--client", "portable", "--project", str(project))
    assert not (project / ".agents").exists()


def test_user_scope_and_destination_override_have_explicit_locations(installer, monkeypatch):
    module, _, project = installer
    monkeypatch.setattr(module.Path, "home", lambda: project)
    assert _install(module, monkeypatch, "--client", "portable", "--scope", "user") == 0
    assert (project / ".agents/skills/knowledge-layer/SKILL.md").is_file()
    target = project / "custom/knowledge-layer"
    assert _install(module, monkeypatch, "--client", "portable", "--destination", str(target),
                    "--project", str(project / "unused-missing-project")) == 0
    assert (target / "SKILL.md").is_file()


@pytest.mark.parametrize("overlap", ["ancestor", "descendant"])
def test_installer_refuses_source_overlap_before_moving_or_copying(installer, monkeypatch, overlap):
    module, source, project = installer
    if overlap == "ancestor":
        target = project / "knowledge-layer"
        target.mkdir()
        nested = target / "source/knowledge-layer"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_bytes((source / "SKILL.md").read_bytes())
        monkeypatch.setattr(module, "SOURCE", nested)
    else:
        target = source / "child/knowledge-layer"
    with pytest.raises(SystemExit):
        _install(module, monkeypatch, "--client", "portable", "--destination", str(target), "--replace")
    assert (module.SOURCE / "SKILL.md").is_file()
    assert list(project.glob("*.backup-*")) == []
