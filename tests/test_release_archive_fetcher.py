from __future__ import annotations

import base64
import hashlib
import importlib
import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def fetcher(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("fetch_release_archives")


def entry(data=b"synthetic archive", **changes):
    return {"purl": "pkg:pypi/synthetic@1.0", "url": "https://files.pythonhosted.org/synthetic.whl",
            "hash": "sha256:" + hashlib.sha256(data).hexdigest(), "directory": "wheels",
            "filename": "synthetic.whl", **changes}


@pytest.mark.parametrize("url", ["http://files.pythonhosted.org/a.whl", "https://evil.test/a.whl",
    "https://user:secret@files.pythonhosted.org/a.whl", "https://files.pythonhosted.org:443/a.whl",
    "https://files.pythonhosted.org/a.whl?token=secret", "https://registry.npmjs.org/a.tgz#x"])
def test_archive_fetcher_refuses_credentials_redirect_origins_and_non_https(fetcher, url):
    with pytest.raises(ValueError): fetcher.validate_url(url)


@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512"])
def test_archive_digest_matches_exact_bytes(fetcher, tmp_path, algorithm):
    path = tmp_path / "archive"
    path.write_bytes(b"synthetic archive")
    value = algorithm + "-" + base64.b64encode(hashlib.new(algorithm, path.read_bytes()).digest()).decode()
    assert fetcher.matches(path, value)
    path.write_bytes(b"different")
    assert not fetcher.matches(path, value)


def test_failed_or_unverified_download_cannot_be_notice_evidence(fetcher, tmp_path):
    def wrong(arguments, **kwargs):
        Path(arguments[arguments.index("--destination") + 1]).write_bytes(b"wrong archive")
    output = tmp_path / "archives"
    result = fetcher.fetch([entry()], output, runner=wrong)
    assert not result["archives"] and len(result["missing"]) == 1
    assert result["downloaded_bytes"] == len(b"wrong archive")
    assert not (output / "wheels/synthetic.whl").exists()
    assert result["package_code_executed"] is False


def test_child_deadline_is_bounded_and_private_failure_is_not_recorded(fetcher, tmp_path):
    def timeout(arguments, **kwargs):
        assert kwargs["timeout"] <= 20 and kwargs["capture_output"] is True
        raise subprocess.TimeoutExpired(arguments, kwargs["timeout"], stderr="PRIVATE_MARKER")
    result = fetcher.fetch([entry()], tmp_path / "archives", runner=timeout)
    assert len(result["missing"]) == 1 and "PRIVATE_MARKER" not in json.dumps(result)


@pytest.mark.parametrize("items", [[entry(), entry(purl="pkg:pypi/duplicate@1.0")], [entry(filename="../escape")], [entry(directory="../outside")]])
def test_duplicate_and_traversal_destinations_refuse_before_creation(fetcher, tmp_path, items):
    output = tmp_path / "archives"
    with pytest.raises(ValueError): fetcher.fetch(items, output)
    assert not output.exists()


def test_successful_fetch_never_installs_or_executes_package_code(fetcher, tmp_path):
    data = b"synthetic archive"
    def download(arguments, **kwargs):
        assert "--fetch-url" in arguments and "pip" not in arguments and "install" not in arguments
        Path(arguments[arguments.index("--destination") + 1]).write_bytes(data)
    output = tmp_path / "archives"
    result = fetcher.fetch([entry(data)], output, runner=download)
    assert len(result["archives"]) == 1 and result["missing"] == []
    assert result["downloaded_bytes"] == len(data) and result["packages_installed"] == 0
    assert (output.stat().st_mode & 0o777) == 0o700


def test_target_tags_refuse_free_threaded_or_other_architecture_wheels(fetcher):
    from packaging.tags import Tag
    tags = fetcher.target_tags("linux/amd64")
    assert Tag("cp314", "cp314", "manylinux_2_28_x86_64") in tags
    assert Tag("cp311", "abi3", "manylinux_2_17_x86_64") in tags
    assert Tag("py3", "none", "any") in tags
    assert Tag("cp314", "cp314t", "manylinux_2_28_x86_64") not in tags
    assert Tag("cp314", "cp314", "manylinux_2_28_aarch64") not in tags


def test_platform_exclusions_require_explicit_false_exact_version_markers(fetcher, tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("synthetic==1.0 ; sys_platform == 'win32'\n"
                    "synthetic==1.0 ; sys_platform == 'linux'\n"
                    "windows-only==2.0 ; sys_platform == 'win32'\n")
    assert fetcher.requirement_markers(path, "linux/amd64") == {
        ("synthetic", "1.0"): True, ("windows-only", "2.0"): False}
    assert fetcher.requirement_markers(None, "linux/amd64") == {}
    path.write_text("synthetic>=1.0\n")
    with pytest.raises(ValueError): fetcher.requirement_markers(path, "linux/amd64")


def test_selection_binds_locked_version_architecture_and_scoped_npm_identity(fetcher, tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text('''[[package]]
name = "synthetic"
version = "1.0"
wheels = [
 { url = "https://files.pythonhosted.org/synthetic-1.0-cp314-cp314-manylinux_2_28_aarch64.whl", hash = "sha256:wrong-architecture" },
 { url = "https://files.pythonhosted.org/synthetic-1.0-py3-none-any.whl", hash = "sha256:exact-version" }
]
''')
    npm = tmp_path / "package-lock.json"
    npm.write_text(json.dumps({"packages": {"node_modules/@scope/synthetic": {
        "version": "1.0", "resolved": "https://registry.npmjs.org/@scope/synthetic/-/synthetic-1.0.tgz", "integrity": "sha512-exact"}}}))
    (tmp_path / "python.cdx.json").write_text(json.dumps({"bomFormat": "CycloneDX", "components": [
        {"name": "synthetic", "version": "1.0", "purl": "pkg:pypi/synthetic@1.0"},
        {"name": "synthetic", "version": "2.0", "purl": "pkg:pypi/synthetic@2.0"}]}))
    (tmp_path / "web.cdx.json").write_text(json.dumps({"bomFormat": "CycloneDX", "components": [
        {"name": "@scope/synthetic", "version": "1.0", "purl": "pkg:npm/%40scope/synthetic@1.0"}]}))
    entries, missing = fetcher.selection(tmp_path, ["python.cdx.json", "web.cdx.json"], lock, npm, "linux/amd64")
    assert len(entries) == 2 and {item["hash"] for item in entries} == {"sha256:exact-version", "sha512-exact"}
    assert missing == [{"purl": "pkg:pypi/synthetic@2.0", "reason": "compatible_locked_wheel_missing"}]


@pytest.fixture
def frozen_inputs(tmp_path, monkeypatch):
    paths = {"uv_lock": tmp_path / "uv.lock", "npm_lock": tmp_path / "package-lock.json",
             "sbom": tmp_path / "python.cdx.json", "requirements": tmp_path / "requirements.txt"}
    paths["uv_lock"].write_text('[[package]]\nname = "synthetic"\nversion = "1.0"\n')
    paths["npm_lock"].write_text(json.dumps({"packages": {}}))
    paths["sbom"].write_text(json.dumps({"bomFormat": "CycloneDX", "components": [
        {"name": "synthetic", "version": "1.0", "purl": "pkg:pypi/synthetic@1.0"}]}))
    paths["requirements"].write_text("synthetic==1.0 ; sys_platform == 'win32'\n")
    output = tmp_path / "archives"
    monkeypatch.setattr("sys.argv", ["fetch", "--sbom-dir", str(tmp_path), "--scope", "python.cdx.json",
        "--uv-lock", str(paths["uv_lock"]), "--npm-lock", str(paths["npm_lock"]),
        "--platform", "linux/amd64", "--requirements", str(paths["requirements"]),
        "--output-dir", str(output)])
    return paths, output


@pytest.mark.parametrize("changed_input", ["requirements", "uv_lock", "npm_lock", "sbom"])
def test_input_mutation_during_fetch_refuses_before_receipt(fetcher, frozen_inputs, monkeypatch,
                                                          capsys, changed_input):
    paths, output = frozen_inputs
    original = fetcher.fetch
    def mutate(entries, destination):
        assert entries == []
        if changed_input == "requirements":
            paths[changed_input].write_text("synthetic==1.0 ; sys_platform == 'linux'\n")
        else:
            with paths[changed_input].open("ab") as handle:
                handle.write(b"\n")
        return original(entries, destination)
    monkeypatch.setattr(fetcher, "fetch", mutate)
    assert fetcher.main() == 1
    assert not (output / "archives.json").exists()
    assert "refused" in capsys.readouterr().out


def test_unchanged_inputs_bind_exclusions_and_all_selected_digests(fetcher, frozen_inputs, monkeypatch):
    paths, output = frozen_inputs
    original = fetcher.fetch
    original_markers = fetcher.markers_from_bytes
    marker_reads = []
    def observe(raw, target):
        marker_reads.append(raw)
        return original_markers(raw, target)
    def offline(entries, destination):
        assert entries == []
        return original(entries, destination)
    monkeypatch.setattr(fetcher, "fetch", offline)
    monkeypatch.setattr(fetcher, "markers_from_bytes", observe)
    assert fetcher.main() == 0
    result = json.loads((output / "archives.json").read_text())
    assert marker_reads == [paths["requirements"].read_bytes()]
    assert result["platform_excluded_requirements"] == [{"name": "synthetic", "version": "1.0"}]
    assert result["requirements_sha256"] == hashlib.sha256(paths["requirements"].read_bytes()).hexdigest()
    assert result["selection_inputs_sha256"] == {
        "uv_lock": hashlib.sha256(paths["uv_lock"].read_bytes()).hexdigest(),
        "npm_lock": hashlib.sha256(paths["npm_lock"].read_bytes()).hexdigest(),
        "sbom_scopes": {"python.cdx.json": hashlib.sha256(paths["sbom"].read_bytes()).hexdigest()}}
    assert not result["archives"] and not result["missing"]


def test_requirements_snapshot_refuses_oversized_bytes_before_marker_parsing(fetcher, tmp_path, monkeypatch):
    path = tmp_path / "requirements.txt"
    path.write_bytes(b"synthetic==1.0 ; sys_platform == 'win32'\n")
    monkeypatch.setattr(fetcher, "MAX_REQUIREMENTS", 16)
    def unexpected(*args):
        pytest.fail("oversized requirements must not be parsed")
    monkeypatch.setattr(fetcher, "markers_from_bytes", unexpected)
    with pytest.raises(ValueError, match="size_limit"):
        fetcher.requirement_markers(path, "linux/amd64")


def test_input_snapshot_has_a_total_byte_budget(fetcher, frozen_inputs, monkeypatch):
    paths, _output = frozen_inputs
    monkeypatch.setattr(fetcher, "MAX_INPUT_TOTAL", paths["uv_lock"].stat().st_size + 1)
    with pytest.raises(ValueError, match="size_limit"):
        fetcher.snapshot_inputs(paths["sbom"].parent, [paths["sbom"].name], paths["uv_lock"],
                                paths["npm_lock"], paths["requirements"])


def test_explicit_source_selection_adds_only_exact_scoped_locked_sdist(fetcher, tmp_path):
    lock, npm = tmp_path / "uv.lock", tmp_path / "package-lock.json"
    source_hash = "sha256:" + "a" * 64
    lock.write_text('''[[package]]
name = "synthetic"
version = "1.0"
wheels = [{ url = "https://files.pythonhosted.org/synthetic-1.0-py3-none-any.whl", hash = "sha256:wheel" }]
sdist = { url = "https://files.pythonhosted.org/synthetic-1.0.tar.gz", hash = "'''+source_hash+'" }\n')
    npm.write_text(json.dumps({"packages": {}}))
    (tmp_path / "python.cdx.json").write_text(json.dumps({"bomFormat": "CycloneDX", "components": [
        {"name": "synthetic", "version": "1.0", "purl": "pkg:pypi/synthetic@1.0"}]}))
    entries, missing = fetcher.selection(tmp_path, ["python.cdx.json"], lock, npm, "linux/amd64",
                                         source_packages=["synthetic"])
    assert not missing and len(entries) == 2
    assert next(item for item in entries if item["directory"] == "sdists") == {
        "purl": "pkg:pypi/synthetic@1.0", "url": "https://files.pythonhosted.org/synthetic-1.0.tar.gz",
        "hash": source_hash, "directory": "sdists", "filename": "synthetic-1.0.tar.gz"}
    for sources in (["absent"], ["synthetic", "synthetic"], ["../synthetic"]):
        with pytest.raises(ValueError):
            fetcher.selection(tmp_path, ["python.cdx.json"], lock, npm, "linux/amd64", source_packages=sources)
    marker = tmp_path / "requirements.txt"
    marker.write_text("synthetic==1.0 ; sys_platform == 'win32'\n")
    with pytest.raises(ValueError, match="not_in_selected_scope"):
        fetcher.selection(tmp_path, ["python.cdx.json"], lock, npm, "linux/amd64", marker,
                          source_packages=["synthetic"])


def test_source_archive_refuses_nonlocked_digest_without_substituting_for_wheel(fetcher, tmp_path):
    lock, npm = tmp_path / "uv.lock", tmp_path / "package-lock.json"
    lock.write_text('''[[package]]
name = "synthetic"
version = "1.0"
sdist = { url = "https://files.pythonhosted.org/synthetic-1.0.tar.gz", hash = "sha256:unverified" }
''')
    npm.write_text(json.dumps({"packages": {}}))
    (tmp_path / "python.cdx.json").write_text(json.dumps({"bomFormat": "CycloneDX", "components": [
        {"name": "synthetic", "version": "1.0", "purl": "pkg:pypi/synthetic@1.0"}]}))
    entries, missing = fetcher.selection(tmp_path, ["python.cdx.json"], lock, npm, "linux/amd64",
                                         source_packages=["synthetic"])
    assert entries == [] and {item["reason"] for item in missing} == {
        "compatible_locked_wheel_missing", "locked_source_archive_missing"}
