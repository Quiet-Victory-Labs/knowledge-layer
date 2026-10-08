"""Release refusal, identity, checksum and redaction tests; nothing publishes."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
ALPHA_CLASSIFIER = "Development Status :: 3 - Alpha"


@pytest.fixture
def scripts(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return {name: importlib.import_module(name) for name in (
        "release_gate", "release_manifest", "container_scan_policy", "check_release_history",
        "dependency_licenses", "verify_release_attestations")}


@pytest.fixture
def version_tree(tmp_path):
    for relative in ("pyproject.toml", "web/package.json", "web/package-lock.json",
                     "charts/knowledge-layer/Chart.yaml"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, target)
    return tmp_path


@pytest.mark.parametrize("relative", ["web/package.json", "web/package-lock.json",
                                     "charts/knowledge-layer/Chart.yaml"])
def test_version_gate_checks_web_lock_and_chart(scripts, version_tree, relative):
    gate = scripts["release_gate"]
    assert gate.versions(version_tree) == "0.1.0"
    path = version_tree / relative
    path.write_text(path.read_text().replace("0.1.0", "0.2.0", 1))
    with pytest.raises(ValueError, match="disagree"):
        gate.versions(version_tree)


def release_metadata(version_tree, *, version="1.0.0", classifier="Development Status :: 5 - Production/Stable"):
    for path in version_tree.rglob("*"):
        if path.is_file():
            path.write_text(path.read_text().replace("0.1.0", version).replace(ALPHA_CLASSIFIER, classifier))


def test_prerelease_remains_default_and_current_alpha_cannot_be_stable(scripts, version_tree, monkeypatch, capsys):
    gate = scripts["release_gate"]
    assert gate.channel_metadata("prerelease", version_tree)["version"] == "0.1.0"
    with pytest.raises(ValueError, match="Production/Stable"):
        gate.channel_metadata("stable", version_tree)
    monkeypatch.setattr("sys.argv", ["release_gate", "--versions-only"])
    assert gate.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["channel"] == "prerelease" and receipt["ga_review"] is None
    monkeypatch.setattr("sys.argv", ["release_gate", "--versions-only", "--channel", "stable"])
    assert gate.main() == 1
    assert "Release gate failed" in capsys.readouterr().out


@pytest.mark.parametrize("version,classifier", [
    ("0.9.9", "Development Status :: 5 - Production/Stable"),
    ("1.0.0", ALPHA_CLASSIFIER),
    ("1.0.0", "Development Status :: 4 - Beta"),
    ("1.0.0", 'Development Status :: 5 - Production/Stable", "Development Status :: 3 - Alpha'),
])
def test_stable_refuses_pre_one_or_ambiguous_development_metadata(scripts, version_tree, version, classifier):
    release_metadata(version_tree, version=version, classifier=classifier)
    with pytest.raises(ValueError, match="Production/Stable"):
        scripts["release_gate"].channel_metadata("stable", version_tree)


def test_stable_requires_aligned_production_versions(scripts, version_tree):
    release_metadata(version_tree)
    gate = scripts["release_gate"]
    assert gate.channel_metadata("stable", version_tree) == {
        "channel": "stable", "version": "1.0.0", "development_statuses": [gate.STABLE_CLASSIFIER]}
    path = version_tree / "web/package-lock.json"
    path.write_text(path.read_text().replace("1.0.0", "1.0.1", 1))
    with pytest.raises(ValueError, match="disagree"):
        gate.channel_metadata("stable", version_tree)


def protected_ga(monkeypatch, *, review="GA-2026-10", commit=COMMIT):
    monkeypatch.setenv("RELEASE_GA_ENVIRONMENT_READY", "true")
    monkeypatch.setenv("RELEASE_GA_REVIEW_ID", review)
    monkeypatch.setenv("RELEASE_GA_REVIEW_COMMIT", commit)
    return {"review_id": review, "commit": commit, "acknowledged": True}


@pytest.mark.parametrize("changed", ["ready", "id", "commit", "missing_id", "invalid_id"])
def test_ga_review_must_match_protected_exact_commit_acknowledgment(scripts, monkeypatch, changed):
    gate = scripts["release_gate"]
    review = protected_ga(monkeypatch)
    assert gate.verify_ga_review(COMMIT, review["review_id"]) == review
    requested = review["review_id"]
    if changed == "ready":
        monkeypatch.setenv("RELEASE_GA_ENVIRONMENT_READY", "false")
    elif changed == "id":
        monkeypatch.setenv("RELEASE_GA_REVIEW_ID", "GA-another-review")
    elif changed == "commit":
        monkeypatch.setenv("RELEASE_GA_REVIEW_COMMIT", "b" * 40)
    elif changed == "missing_id":
        requested = None
    else:
        requested = "not a review\n"
    with pytest.raises(ValueError, match="review|GA"):
        gate.verify_ga_review(COMMIT, requested)


def test_stable_gate_still_checks_signed_tag_and_all_six_ci_jobs(scripts, version_tree, monkeypatch, capsys):
    gate = scripts["release_gate"]
    release_metadata(version_tree)
    metadata = gate.channel_metadata("stable", version_tree)
    monkeypatch.setattr(gate, "channel_metadata", lambda _channel: metadata)
    review = protected_ga(monkeypatch)
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v1.0.0")
    monkeypatch.setenv("GITHUB_REPOSITORY", "synthetic/repo")
    monkeypatch.setenv("RELEASE_GPG_PUBLIC_KEY", "synthetic key")
    monkeypatch.setenv("RELEASE_GPG_FINGERPRINT", "B" * 40)
    calls = []
    def tag(*arguments):
        calls.append("signed_tag")
        assert arguments[:2] == ("v1.0.0", COMMIT)
        return {"tag": "v1.0.0", "commit": COMMIT, "signed_annotated_tag": "verified"}
    original_ci = gate.verify_ci
    monkeypatch.setattr(gate, "verify_tag", tag)
    monkeypatch.setattr(gate, "verify_ci", lambda repository, commit: (
        calls.append("six_jobs") or original_ci(repository, commit, api=ci_api(gate))))
    monkeypatch.setattr("sys.argv", ["release_gate", "--channel", "stable", "--ga-review-id", review["review_id"],
        "--tag", "v1.0.0", "--commit", COMMIT, "--repository", "synthetic/repo"])
    assert gate.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ga_review"] == review and receipt["required_jobs"] == sorted(gate.REQUIRED_JOBS)
    assert calls == ["signed_tag", "six_jobs"]


def ci_api(gate, *, runs=None, jobs=None):
    if runs is None:
        runs = [{"id": 1, "head_sha": COMMIT, "event": "push", "run_number": 1,
                 "path": ".github/workflows/verify.yaml", "status": "completed",
                 "conclusion": "success", "run_attempt": 1}]
    if jobs is None:
        jobs = [{"name": name, "status": "completed", "conclusion": "success"}
                for name in gate.REQUIRED_JOBS]
    return lambda arguments: json.dumps({"jobs": jobs} if "/jobs?" in arguments[-1]
                                       else {"workflow_runs": runs})


def test_ci_gate_requires_all_successful_jobs_on_exact_commit(scripts):
    gate = scripts["release_gate"]
    assert gate.verify_ci("synthetic/repo", COMMIT, api=ci_api(gate))["exact_commit_ci"] == "verified"
    with pytest.raises(ValueError):
        gate.verify_ci("synthetic/repo", "b" * 40, api=ci_api(gate))
    for jobs in ([], [{"name": name, "status": "completed", "conclusion": "skipped"}
                     for name in gate.REQUIRED_JOBS]):
        with pytest.raises(ValueError):
            gate.verify_ci("synthetic/repo", COMMIT, api=ci_api(gate, jobs=jobs))


def test_missing_self_host_qualification_blocks_release(scripts):
    gate = scripts["release_gate"]
    assert "self-host-integration" in gate.REQUIRED_JOBS
    jobs = [{"name": name, "status": "completed", "conclusion": "success"}
            for name in gate.REQUIRED_JOBS - {"self-host-integration"}]
    with pytest.raises(ValueError, match="missing"):
        gate.verify_ci("synthetic/repo", COMMIT, api=ci_api(gate, jobs=jobs))


def test_ci_gate_cannot_select_an_older_success_over_latest_failure(scripts):
    gate = scripts["release_gate"]
    good = {"id": 1, "head_sha": COMMIT, "event": "workflow_dispatch", "run_number": 1,
            "path": ".github/workflows/verify.yaml", "status": "completed", "conclusion": "success"}
    failed = good | {"id": 2, "run_number": 2, "conclusion": "failure"}
    with pytest.raises(ValueError, match="Latest"):
        gate.verify_ci("synthetic/repo", COMMIT, api=ci_api(gate, runs=[good, failed]))
    with pytest.raises(ValueError, match="No exact"):
        gate.verify_ci("synthetic/repo", COMMIT, api=ci_api(gate, runs=[good | {"event": "pull_request"}]))


def test_signed_tag_requires_approved_fingerprint_and_private_keyring(scripts, monkeypatch):
    gate = scripts["release_gate"]
    fingerprint = "B" * 40
    def command(arguments, **options):
        if arguments[0] == "gpg":
            key = Path(arguments[-1])
            assert key.stat().st_mode & 0o777 == 0o600
            assert key.parent.stat().st_mode & 0o777 == 0o700
            assert options["env"]["GNUPGHOME"] == str(key.parent)
            return ""
        return "tag" if arguments[:3] == ["git", "cat-file", "-t"] else COMMIT
    monkeypatch.setattr(gate, "command", command)
    monkeypatch.setattr(gate.subprocess, "run", lambda *_args, **_options: SimpleNamespace(
        stderr=f"[GNUPG:] VALIDSIG {fingerprint} 2026-10-04 0 0 4 0 1 10 00 {fingerprint}\n"))
    assert gate.verify_tag("v0.1.0", COMMIT, "synthetic public key", fingerprint)["signed_annotated_tag"] == "verified"
    with pytest.raises(ValueError, match="Unapproved"):
        gate.verify_tag("v0.1.0", COMMIT, "synthetic public key", "C" * 40)
    monkeypatch.setattr(gate, "command", lambda *_args, **_options: "commit")
    with pytest.raises(ValueError, match="annotated"):
        gate.verify_tag("v0.1.0", COMMIT, "synthetic public key", fingerprint)


def test_manifest_detects_modified_extra_missing_files_and_symlinks(scripts, tmp_path):
    manifest = scripts["release_manifest"]
    artifact = tmp_path / "synthetic.whl"
    artifact.write_bytes(b"synthetic wheel")
    result = manifest.create(tmp_path, allow_incomplete=True)
    assert result["complete"] is False and result["published"] is False
    (tmp_path / "release-manifest.json").write_text(json.dumps(result))
    (tmp_path / "SHA256SUMS").write_text("".join(
        f"{item['sha256']}  {item['path']}\n" for item in result["artifacts"]))
    assert manifest.verify(tmp_path) == result
    artifact.write_bytes(b"different content")
    with pytest.raises(ValueError, match="changed"):
        manifest.verify(tmp_path)
    artifact.write_bytes(b"synthetic wheel")
    (tmp_path / "extra.json").write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        manifest.verify(tmp_path)
    (tmp_path / "extra.json").unlink()
    (tmp_path / "linked.whl").symlink_to(artifact)
    with pytest.raises(ValueError, match="symlinks"):
        manifest.verify(tmp_path)


@pytest.mark.parametrize("path", [".env", ".env.production", "source.key", "data/raw/customer.csv",
                                   ".local/state.db", "tests/fixtures/customer.csv", "../unsafe"])
def test_history_gate_blocks_private_paths_and_unreviewed_data(scripts, path):
    assert scripts["check_release_history"].path_reason(path)


@pytest.mark.parametrize("path", [".env.example", "data/connectable/northstar/customers.csv",
                                   "data/connectable/juniper/products.csv", "src/knowledge_layer/store.py"])
def test_history_path_policy_keeps_known_synthetic_and_source_paths(scripts, path):
    assert scripts["check_release_history"].path_reason(path) is None


def test_history_errors_never_echo_plugin_diagnostics(scripts, monkeypatch, capsys, tmp_path):
    scanner = scripts["check_release_history"]
    canary = "private-diagnostic-canary"
    def refusal():
        raise RuntimeError(canary)
    monkeypatch.setattr(scanner, "scan_history", refusal)
    monkeypatch.setattr("sys.argv", ["check_release_history", "--output", str(tmp_path / "report.json")])
    assert scanner.main() == 1
    assert canary not in capsys.readouterr().out


def test_historical_reviews_bind_exact_blob_path_hash_candidate_hash_and_rule(scripts, tmp_path):
    scanner = scripts["check_release_history"]
    path = tmp_path / "history.json"
    candidate = {"object": "a" * 40, "path_sha256": "b" * 64, "candidate_sha1": "c" * 40,
                 "kind": "Secret Keyword", "review_basis": "synthetic_environment_variable_reference"}
    document = {"schema_version": "reviewed-history-candidates-v1", "reviewed_candidates": [candidate]}
    path.write_text(json.dumps(document))
    assert scanner.reviewed_history(path) == {("a" * 40, "b" * 64, "c" * 40, "Secret Keyword")}
    document["reviewed_candidates"].append(candidate)
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="Duplicate"):
        scanner.reviewed_history(path)
    document["reviewed_candidates"] = [candidate | {"review_basis": "globally_allow_this_value"}]
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="Invalid"):
        scanner.reviewed_history(path)


def test_license_inventory_identifies_gaps_without_legal_approval(scripts, tmp_path):
    (tmp_path / "python.cdx.json").write_text(json.dumps({"bomFormat": "CycloneDX", "components": [
        {"name": "synthetic-missing-package", "version": "0.1.0", "purl": "pkg:pypi/synthetic-missing-package@0.1.0"}]}))
    inventory = scripts["dependency_licenses"].collect(tmp_path)
    assert inventory["missing_license_metadata"] == 1
    assert inventory["legal_approved"] is False and inventory["notices_complete"] is False


def test_signature_verifier_pins_identity_and_ref_and_redacts_failure(scripts, monkeypatch, tmp_path, capsys):
    verifier = scripts["verify_release_attestations"]
    def verified(_directory, *, repository):
        assert repository == "synthetic/repo"
        return {"complete": True, "commit": COMMIT,
            "version": "0.1.0", "artifacts": [{"path": "synthetic.whl"}]}
    monkeypatch.setattr(verifier, "verify", verified)
    monkeypatch.setattr("sys.argv", ["verify_release_attestations", str(tmp_path), "--repository",
        "synthetic/repo", "--tag", "v0.1.0", "--commit", COMMIT])
    calls = []
    def run(arguments, **options):
        calls.append(arguments)
        assert options["capture_output"] is True
        # gh's real parser treats all four identity selectors as mutually
        # exclusive. The previous SAN + signer-workflow command cannot run.
        identity_flags = {"--cert-identity", "--cert-identity-regex", "--signer-repo", "--signer-workflow"}
        assert len(identity_flags & set(arguments)) == 1
        assert arguments[arguments.index("--cert-identity") + 1] == (
            "https://github.com/synthetic/repo/.github/workflows/release.yaml@refs/tags/v0.1.0")
        assert arguments[arguments.index("--cert-oidc-issuer") + 1] == (
            "https://token.actions.githubusercontent.com")
        assert arguments[arguments.index("--repo") + 1] == "synthetic/repo"
        assert arguments[arguments.index("--source-ref") + 1] == "refs/tags/v0.1.0"
        assert arguments[arguments.index("--source-digest") + 1] == COMMIT
        assert arguments[arguments.index("--signer-digest") + 1] == COMMIT
        assert "--deny-self-hosted-runners" in arguments
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(verifier.subprocess, "run", run)
    assert verifier.main() == 0 and len(calls) == 3
    capsys.readouterr()
    monkeypatch.setattr(verifier.subprocess, "run", lambda *_args, **_options: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, "synthetic", stderr="private-attestation-canary")))
    assert verifier.main() == 1
    assert "private-attestation-canary" not in capsys.readouterr().out


def test_workflow_publication_is_manual_opt_in_and_action_pins_are_immutable():
    workflow = (ROOT / ".github/workflows/release.yaml").read_text()
    assert "workflow_dispatch:" in workflow and "push:" not in workflow
    assert "if: inputs.publish" in workflow and "environment: oss-release" in workflow
    assert "environment: oss-release-candidate" in workflow
    assert 'test "$RELEASE_CANDIDATE_ENVIRONMENT_READY" = true' in workflow
    assert "APPROVED_LEGAL_REVIEW_COMMIT" in workflow and "--preserve-digests" in workflow
    assert workflow.index('test "$GITHUB_REF" = "refs/tags/$RELEASE_TAG"') < workflow.index("actions/checkout@")
    assert "--all-extras --no-extra dev --no-emit-package knowledge-layer" in workflow
    assert "pip-audit --strict --no-deps --disable-pip" in workflow
    assert "npm audit --prefix web --audit-level low" in workflow
    assert "severity-cutoff: negligible" in workflow and "only-fixed: false" in workflow
    assert workflow.count("fail-build: false") == 2
    assert "cp security/container-risk-decisions.json .local/release/container-risk-decisions.json" in workflow
    assert "scripts/release_manifest.py .local/release --check-container-scans" in workflow
    assert "--ignore-vuln" not in workflow and "continue-on-error" not in workflow
    import re
    pins = re.findall(r"uses: [^@\n]+@([^\s]+)", workflow)
    assert pins and all(re.fullmatch(r"[0-9a-f]{40}", pin) for pin in pins)


def test_self_host_ci_runs_configuration_controls_and_customer_flow_on_built_image():
    workflow = (ROOT / ".github/workflows/verify.yaml").read_text()
    assert "KNOWLEDGE_RUNTIME_TEST_IMAGE: knowledge-layer:verify" in workflow
    assert workflow.index("docker build -t knowledge-layer:verify") < workflow.index(
        "pytest -q tests/test_container_runtime_root.py")
    assert "  self-host-integration:" in workflow
    assert workflow.index("docker build -t knowledge-layer:verify") < workflow.index(
        "scripts/drill_self_host.py --image knowledge-layer:verify")


@pytest.mark.parametrize("filename", ["release.yaml", "verify.yaml"])
def test_web_build_pins_declared_npm_before_dependency_install(filename):
    workflow = (ROOT / ".github/workflows" / filename).read_text()
    package = json.loads((ROOT / "web/package.json").read_text())
    npm = package["packageManager"]
    assert npm == "npm@12.2.0"
    assert workflow.index("actions/setup-node@") < workflow.index("npm install --global " + npm)
    assert workflow.index("npm install --global " + npm) < workflow.index("npm ci --prefix web")


def test_shipped_postgres_build_scan_license_and_signed_evidence_are_required_before_prepare():
    workflow = (ROOT / ".github/workflows/release.yaml").read_text()
    build = ("docker buildx build --platform linux/amd64 --file Dockerfile.postgres --provenance=mode=max "
             "--metadata-file .local/release/postgres-build.json "
             "--output type=oci,dest=.local/release-tools/postgres.oci.tar .")
    sbom = ("syft oci-archive:.local/release-tools/postgres.oci.tar "
            "-o cyclonedx-json=.local/release/sbom/postgres.cdx.json")
    assert build in workflow and sbom in workflow
    assert workflow.index(build) < workflow.index(sbom) < workflow.index("scripts/dependency_licenses.py")
    scan = workflow.split("- name: Record raw shipped PostgreSQL findings", 1)[1].split("      - name:", 1)[0]
    for setting in ("anchore/scan-action@27805bf3b4e84b4a5c980df22ed233c00390a439",
                    "sbom: .local/release/sbom/postgres.cdx.json", "grype-version: v0.120.0",
                    "config: .local/release-tools/grype.yaml", "fail-build: false",
                    "severity-cutoff: negligible", "only-fixed: false", "output-format: json",
                    "output-file: .local/release/postgres-vulnerabilities.json"):
        assert setting in scan
    risk_check = "scripts/release_manifest.py .local/release --check-container-scans"
    assert workflow.index("output-file: .local/release/postgres-vulnerabilities.json") < workflow.index(risk_check)
    assert workflow.index(risk_check) < workflow.index('scripts/release_manifest.py .local/release --channel "$RELEASE_CHANNEL"')
    assert workflow.index(risk_check) < workflow.index("Keyless provenance for every file")
    assert "container-risk-evaluation.json" in workflow
    assert "subject-path: '${{ github.workspace }}/.local/release/**/*'" in workflow
    assert "dest=.local/release/postgres.oci.tar" not in workflow
    assert "continue-on-error" not in scan and "--ignore-vuln" not in workflow


@pytest.fixture
def complete_release(tmp_path, scripts, monkeypatch):
    manifest = scripts["release_manifest"]
    files = ["knowledge_layer-0.1.0-py3-none-any.whl", "knowledge_layer-0.1.0.tar.gz", "web-static.tar.gz",
             "knowledge-layer-0.1.0.tgz", "container.oci.tar", "container-build.json", "container-reference.txt",
             "sbom/python.cdx.json", "sbom/python-all-runtime-extras.cdx.json", "sbom/web.cdx.json",
             "sbom/container.cdx.json", "sbom/postgres.cdx.json", "dependency-licenses.json",
             "locked-license-metadata.json", "sbom-bundle.tar.gz", "history-scan.json", "release-gate.json",
             "verification.json", "python-vulnerabilities.json", "web-vulnerabilities.json",
             "third-party-notices.tar.gz", "notice-requirements.txt", "notices/third-party-notices.json"]
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic artifact")
    (tmp_path / "postgres-build.json").write_text(json.dumps({"containerimage.digest": "sha256:" + "b" * 64}))
    (tmp_path / "release-controls.json").write_text(json.dumps({
        "schema_version": "release-controls-v1", "result": "passed", "tag": "v0.1.0",
        "repository": "synthetic/repo", "settings_changed": False, "human_approval_inferred": False,
        "checks": [{"name": name, "result": "passed"} for name in
                   ("main_protection", "oss-release-candidate", "oss-release")]}))
    for name in manifest.CONTAINER_SCANS:
        (tmp_path / name).write_text(json.dumps({"matches": [], "ignoredMatches": []}))
    (tmp_path / "container-risk-decisions.json").write_text(json.dumps({
        "schema_version": "container-risk-decisions-v1", "decisions": []}))
    (tmp_path / "container-risk-evaluation.json").write_text(json.dumps(
        scripts["container_scan_policy"].evaluate(tmp_path)))
    (tmp_path / "release-gate.json").write_text(json.dumps({
        "schema_version": "release-gate-v1", "scope": "signed_tag_and_exact_commit_ci",
        "version": "0.1.0", "commit": COMMIT, "tag": "v0.1.0", "channel": "prerelease",
        "development_statuses": [ALPHA_CLASSIFIER], "ga_review": None,
        "signed_annotated_tag": "verified", "exact_commit_ci": "verified",
        "required_jobs": sorted(scripts["release_gate"].REQUIRED_JOBS),
    }))
    monkeypatch.setattr(manifest.subprocess, "check_output", lambda args, **kwargs: COMMIT if "rev-parse" in args else "")
    monkeypatch.setenv("GITHUB_REPOSITORY", "synthetic/repo")
    # Notice coverage has its own real-archive tests. This fixture supplies only
    # synthetic artifact bytes to isolate release identity/refusal behavior.
    monkeypatch.setattr(manifest, "check_notices", lambda _directory: None)
    return tmp_path


@pytest.mark.parametrize("repository", [None, "another/repo", "synthetic/other"])
def test_control_receipt_cannot_be_transplanted_from_another_repository(scripts, complete_release, monkeypatch, repository):
    manifest = scripts["release_manifest"]
    result = manifest.create(complete_release)
    save_manifest(manifest, complete_release, result)
    monkeypatch.delenv("GITHUB_REPOSITORY")
    with pytest.raises(ValueError, match="repository|protection receipt"):
        manifest.create(complete_release, repository=repository)
    with pytest.raises(ValueError, match="repository"):
        manifest.verify(complete_release, repository=repository)


def test_rehashed_controls_from_another_repo_still_refuse(scripts, complete_release):
    manifest = scripts["release_manifest"]
    result = manifest.create(complete_release)
    path = complete_release / "release-controls.json"
    controls = json.loads(path.read_text())
    controls["repository"] = "another/repo"
    path.write_text(json.dumps(controls))
    result["artifacts"] = manifest.inventory(complete_release)
    save_manifest(manifest, complete_release, result)
    with pytest.raises(ValueError, match="protection receipt"):
        manifest.verify(complete_release)


@pytest.mark.parametrize("name", ["release-controls.json", "third-party-notices.tar.gz",
    "notice-requirements.txt", "notices/third-party-notices.json"])
def test_release_requires_protection_receipt_and_notice_review_inputs(scripts, complete_release, name):
    (complete_release / name).unlink()
    with pytest.raises(ValueError, match="missing"):
        scripts["release_manifest"].create(complete_release)


def test_manifest_notice_coverage_uses_independent_exact_checkout_inputs(scripts, tmp_path, monkeypatch):
    collector = importlib.import_module("collect_release_notices")
    calls = []
    def refusal(directory, **kwargs):
        calls.append((directory, kwargs))
        raise ValueError("notice_coverage_incomplete")
    monkeypatch.setattr(collector, "check_coverage", refusal)
    with pytest.raises(ValueError, match="notice_coverage_incomplete"):
        scripts["release_manifest"].check_notices(tmp_path)
    assert calls == [(tmp_path / "notices", {"sbom_dir": tmp_path / "sbom",
        "uv_lock": Path("uv.lock"), "npm_lock": Path("web/package-lock.json"),
        "scopes": ["python-all-runtime-extras.cdx.json", "web.cdx.json"],
        "platform": "linux/amd64", "requirements": tmp_path / "notice-requirements.txt",
        "publisher_catalog": Path("docs/publisher-notices.json")})]


def test_release_checks_actual_controls_and_notice_coverage_before_attestation_or_publication():
    workflow = (ROOT / ".github/workflows/release.yaml").read_text()
    prepare, publish = workflow.split("\n  publish:", 1)
    preflight = "python3 scripts/check_release_controls.py"
    assert prepare.index(preflight) < prepare.index("Build and smoke Python artifacts")
    assert publish.index(preflight) < publish.index("actions/download-artifact@")
    assert prepare.count("secrets.RELEASE_CONTROLS_READ_TOKEN") == 1
    assert publish.count("secrets.RELEASE_CONTROLS_READ_TOKEN") == 1
    assert prepare.index("scripts/collect_release_notices.py --check") < prepare.index("Keyless provenance")
    assert ".local/release/notice-requirements.txt --verify-tag" in publish
    assert publish.index("third-party-notices.tar.gz -C") < publish.index(".local/published-readback --verify")
    assert '--repository "$GITHUB_REPOSITORY" --channel' in publish


def test_complete_manifest_requires_clean_postgres_build_sbom_and_scan(scripts, complete_release):
    manifest = scripts["release_manifest"]
    result = manifest.create(complete_release)
    assert result["complete"] is True and result["required_artifacts"]["postgres_identity"] is True
    assert {"postgres-build.json", "postgres-vulnerabilities.json", "sbom/postgres.cdx.json"} <= {
        item["path"] for item in result["artifacts"]}
    assert not any(item["path"].endswith("postgres.oci.tar") for item in result["artifacts"])
    for name in ("postgres-build.json", "postgres-vulnerabilities.json", "sbom/postgres.cdx.json"):
        path = complete_release / name
        original = path.read_bytes()
        path.unlink()
        with pytest.raises(ValueError, match="missing"):
            manifest.create(complete_release)
        path.write_bytes(original)


def save_manifest(module, directory, result):
    (directory / "release-manifest.json").write_text(json.dumps(result))
    (directory / "SHA256SUMS").write_text("".join(
        f"{item['sha256']}  {item['path']}\n" for item in result["artifacts"]))


def future_release(scripts, directory, monkeypatch, *, channel="prerelease", ga_review=None):
    gate = scripts["release_gate"]
    metadata = gate.validate_channel_metadata("1.0.0", [gate.STABLE_CLASSIFIER], channel)
    path = directory / "release-gate.json"
    receipt = json.loads(path.read_text())
    receipt.update(metadata, tag="v1.0.0", ga_review=ga_review)
    path.write_text(json.dumps(receipt))
    path = directory / "release-controls.json"
    controls = json.loads(path.read_text())
    controls["tag"] = "v1.0.0"
    path.write_text(json.dumps(controls))
    monkeypatch.setattr(scripts["release_manifest"], "channel_metadata", lambda requested: (
        gate.validate_channel_metadata("1.0.0", [gate.STABLE_CLASSIFIER], requested)))


def test_prepared_prerelease_cannot_be_promoted_even_with_stable_eligible_metadata(scripts, complete_release, monkeypatch):
    module = scripts["release_manifest"]
    future_release(scripts, complete_release, monkeypatch)
    result = module.create(complete_release)
    save_manifest(module, complete_release, result)
    assert result["channel"] == "prerelease" and result["ga_review"] is None
    with pytest.raises(ValueError, match="channel"):
        module.verify(complete_release, channel="stable")
    with pytest.raises(ValueError, match="channel"):
        module.create(complete_release, channel="stable")
    result["channel"] = "stable"
    result["ga_review"] = protected_ga(monkeypatch)
    save_manifest(module, complete_release, result)
    with pytest.raises(ValueError, match="channel"):
        module.verify(complete_release, channel="stable", ga_review_id="GA-2026-10")


def test_stable_artifacts_bind_exact_review_and_recheck_protected_publication_environment(scripts, complete_release, monkeypatch):
    module = scripts["release_manifest"]
    review = protected_ga(monkeypatch)
    future_release(scripts, complete_release, monkeypatch, channel="stable", ga_review=review)
    result = module.create(complete_release, channel="stable")
    save_manifest(module, complete_release, result)
    assert result["ga_review"] == review
    assert module.verify(complete_release, channel="stable", ga_review_id=review["review_id"]) == result
    with pytest.raises(ValueError, match="channel"):
        module.verify(complete_release, channel="prerelease")
    protected_ga(monkeypatch, review="GA-rotated-review")
    with pytest.raises(ValueError, match="prepared artifacts"):
        module.verify(complete_release, channel="stable", ga_review_id="GA-rotated-review")
    protected_ga(monkeypatch, commit="b" * 40)
    with pytest.raises(ValueError, match="exact GA review"):
        module.verify(complete_release, channel="stable", ga_review_id=review["review_id"])


def test_stable_manifest_never_trusts_an_unsigned_gate_without_protected_review(scripts, complete_release, monkeypatch):
    review = protected_ga(monkeypatch)
    future_release(scripts, complete_release, monkeypatch, channel="stable", ga_review=review)
    monkeypatch.delenv("RELEASE_GA_ENVIRONMENT_READY")
    with pytest.raises(ValueError, match="exact GA review"):
        scripts["release_manifest"].create(complete_release, channel="stable")


@pytest.mark.parametrize("changed", ["review_commit", "review_missing", "not_acknowledged", "version_only", "missing_job"])
def test_stable_preparation_refuses_partial_or_wrong_commit_review_and_gates(scripts, complete_release, monkeypatch, changed):
    module = scripts["release_manifest"]
    review = protected_ga(monkeypatch)
    future_release(scripts, complete_release, monkeypatch, channel="stable", ga_review=review)
    path = complete_release / "release-gate.json"
    gate = json.loads(path.read_text())
    if changed == "review_commit":
        gate["ga_review"]["commit"] = "b" * 40
    elif changed == "review_missing":
        gate["ga_review"] = None
    elif changed == "not_acknowledged":
        gate["ga_review"]["acknowledged"] = False
    elif changed == "version_only":
        gate["scope"] = "versions_only"
    else:
        gate["required_jobs"].remove("self-host-integration")
    path.write_text(json.dumps(gate))
    with pytest.raises(ValueError, match="review|gate"):
        module.create(complete_release, channel="stable")


def test_workflow_binds_channel_before_publication_and_checks_actual_release_readback():
    workflow = (ROOT / ".github/workflows/release.yaml").read_text()
    assert "default: prerelease" in workflow and "options: [prerelease, stable]" in workflow
    assert "channel: ${{ steps.identity.outputs.channel }}" in workflow
    for variable in ("RELEASE_GA_ENVIRONMENT_READY", "RELEASE_GA_REVIEW_ID", "RELEASE_GA_REVIEW_COMMIT"):
        assert workflow.count(variable + ": ${{ vars." + variable + " }}") == 2
    assert 'test "$RELEASE_COMMIT" = "$RELEASE_GA_REVIEW_COMMIT"' in workflow
    assert 'test "$RELEASE_CHANNEL" = "$REQUESTED_RELEASE_CHANNEL"' in workflow
    check = 'scripts/release_manifest.py .local/release --verify --repository "$GITHUB_REPOSITORY" --channel "$RELEASE_CHANNEL" --ga-review-id "$GA_REVIEW_ID"'
    assert workflow.index(check) < workflow.index("skopeo copy --all --preserve-digests")
    assert 'name: oss-release-${{ steps.identity.outputs.channel }}' in workflow
    assert 'name: oss-release-${{ needs.prepare.outputs.channel }}' in workflow
    assert 'release_options=(--prerelease --title "Knowledge Layer $RELEASE_TAG alpha")' in workflow
    assert 'release_options=(--title "Knowledge Layer $RELEASE_TAG")' in workflow
    assert 'scripts/release_manifest.py .local/published-readback --verify --repository "$GITHUB_REPOSITORY" --channel "$RELEASE_CHANNEL"' in workflow
    assert "release.get('prerelease') is not (channel == 'prerelease')" in workflow


@pytest.mark.parametrize("name", ["container-vulnerabilities.json", "postgres-vulnerabilities.json"])
@pytest.mark.parametrize("result", [
    {"matches": [{"vulnerability": {"severity": "Negligible"}}], "ignoredMatches": []},
    {"matches": [{"vulnerability": {"severity": "Unknown"}}], "ignoredMatches": []},
    {"matches": [], "ignoredMatches": [{"vulnerability": {"severity": "Critical"}}]},
    {"matches": [], "ignoredMatches": {}}, {"matches": None}, {}, [],
])
def test_invalid_container_report_refuses_release(scripts, complete_release, name, result):
    manifest = scripts["release_manifest"]
    (complete_release / name).write_text(json.dumps(result))
    with pytest.raises((ValueError, TypeError)):
        manifest.check_container_scans(complete_release)
    with pytest.raises((ValueError, TypeError)):
        manifest.create(complete_release)


def test_unreviewed_medium_container_match_is_retained_without_blocking_release(scripts, complete_release):
    policy = scripts["container_scan_policy"]
    match = {"vulnerability": {"id": "CVE-2026-10001", "namespace": "debian:distro:debian:13",
                               "severity": "Medium", "fix": {"state": "not-fixed", "versions": []}},
             "artifact": {"name": "libexample", "version": "1.0",
                          "purl": "pkg:deb/debian/libexample@1.0"}}
    (complete_release / "container-vulnerabilities.json").write_text(json.dumps({"matches": [match]}))
    report = policy.evaluate(complete_release)
    assert report["result"] == "passed" and report["raw_matches"] == 1
    assert report["tracked_matches"] == 1 and report["blocking_matches"] == 0
    (complete_release / policy.EVALUATION).write_text(json.dumps(report))
    scripts["release_manifest"].check_container_scans(complete_release)
    assert scripts["release_manifest"].create(complete_release)["complete"] is True


@pytest.mark.parametrize("severity", ["Critical", "High", "Unknown"])
def test_unreviewed_serious_container_match_blocks_but_scoped_review_can_pass(scripts, complete_release,
                                                                               severity):
    policy = scripts["container_scan_policy"]
    match = {"vulnerability": {"id": "CVE-2026-10002", "namespace": "debian:distro:debian:13",
                               "severity": severity, "fix": {"state": "not-fixed", "versions": []}},
             "artifact": {"name": "libexample", "version": "1.0",
                          "purl": "pkg:deb/debian/libexample@1.0"}}
    (complete_release / "container-vulnerabilities.json").write_text(json.dumps({
        "matches": [match], "ignoredMatches": []}))
    blocked = policy.evaluate(complete_release)
    assert blocked["result"] == "blocked" and blocked["blocking_matches"] == 1
    (complete_release / policy.EVALUATION).write_text(json.dumps(blocked))
    with pytest.raises(ValueError, match="unresolved serious"):
        scripts["release_manifest"].check_container_scans(complete_release)
    from datetime import UTC, datetime, timedelta
    today = datetime.now(UTC).date()
    expires = today + timedelta(days=30)
    decision = {"image": "application", "namespace": "debian:distro:debian:13",
                "advisory": "CVE-2026-10002", "purl": "pkg:deb/debian/libexample@1.0",
                "disposition": "risk_accepted", "rationale": "Restricted local-only execution path with compensating controls.",
                "evidence": ["docs/container-runtime.md"], "reviewer": "maintainer-example",
                "reviewed_on": today.isoformat(), "expires_on": expires.isoformat()}
    (complete_release / policy.DECISIONS).write_text(json.dumps({
        "schema_version": "container-risk-decisions-v1", "decisions": [decision]}))
    reviewed = policy.evaluate(complete_release, today=today)
    assert reviewed["result"] == "passed" and reviewed["reviewed_matches"] == 1
    (complete_release / policy.EVALUATION).write_text(json.dumps(reviewed))
    policy.check(complete_release, today=today)
    assert scripts["release_manifest"].create(complete_release)["complete"] is True
    with pytest.raises(ValueError, match="stale"):
        policy.check(complete_release, today=expires)
    decision["purl"] = "pkg:deb/debian/libexample@2.0"
    (complete_release / policy.DECISIONS).write_text(json.dumps({
        "schema_version": "container-risk-decisions-v1", "decisions": [decision]}))
    with pytest.raises(ValueError, match="does not match"):
        policy.evaluate(complete_release, today=today)


@pytest.mark.parametrize("result", [{}, {"containerimage.digest": "mutable:tag"},
                                   {"containerimage.digest": None}, []])
def test_postgres_build_requires_exact_digest(scripts, complete_release, result):
    (complete_release / "postgres-build.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="identity"):
        scripts["release_manifest"].create(complete_release)


def test_scan_only_cli_fails_for_postgres_ignored_findings_before_manifest(scripts, complete_release,
                                                                       monkeypatch, capsys):
    manifest = scripts["release_manifest"]
    monkeypatch.setattr("sys.argv", ["release_manifest", str(complete_release), "--check-container-scans"])
    assert manifest.main() == 0
    capsys.readouterr()
    (complete_release / "postgres-vulnerabilities.json").write_text(json.dumps({
        "matches": [], "ignoredMatches": [{"vulnerability": {"severity": "Unknown"}, "reason": "private-canary"}]}))
    assert manifest.main() == 1
    assert "private-canary" not in capsys.readouterr().out
    assert not (complete_release / "release-manifest.json").exists()


def test_signed_manifest_verification_cannot_accept_rehashed_postgres_findings(scripts, complete_release):
    manifest = scripts["release_manifest"]
    result = manifest.create(complete_release)
    (complete_release / "postgres-vulnerabilities.json").write_text(json.dumps({"matches": [{"severity": "Unknown"}]}))
    result["artifacts"] = manifest.inventory(complete_release)
    (complete_release / "release-manifest.json").write_text(json.dumps(result))
    (complete_release / "SHA256SUMS").write_text("".join(
        f"{item['sha256']}  {item['path']}\n" for item in result["artifacts"]))
    with pytest.raises((ValueError, TypeError), match="scanner match|Container scan"):
        manifest.verify(complete_release)


def test_postgres_sbom_components_retain_license_inventory_scope(scripts, tmp_path):
    component = {"name": "synthetic-libssl", "version": "1.0", "purl": "pkg:apk/alpine/synthetic-libssl@1.0",
                 "licenses": [{"license": {"id": "Apache-2.0"}}], "type": "library"}
    for scope in ("container.cdx.json", "postgres.cdx.json"):
        (tmp_path / scope).write_text(json.dumps({"bomFormat": "CycloneDX", "components": [component]}))
    result = scripts["dependency_licenses"].collect(tmp_path)
    assert result["scopes"] == ["container.cdx.json", "postgres.cdx.json"]
    assert result["components"][0]["scopes"] == result["scopes"]
    assert result["components"][0]["declared_licenses"] == component["licenses"]
    assert result["legal_approved"] is False and result["notices_complete"] is False
