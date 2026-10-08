"""License evidence is scoped, exact-version and digest verified, never approval."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return tuple(importlib.import_module(name) for name in (
        "dependency_licenses", "fetch_license_metadata"))


def scope(path, components):
    path.write_text(json.dumps({"bomFormat": "CycloneDX", "components": components}))


def package(**changes):
    return {"name": "synthetic-uninstalled", "version": "1.2.3", "type": "library",
            "purl": "pkg:pypi/synthetic-uninstalled@1.2.3"} | changes


def test_later_sbom_license_declarations_are_merged_without_duplicate_scopes(modules, tmp_path):
    inventory, _ = modules
    declaration = {"license": {"id": "MIT"}}
    scope(tmp_path / "a.cdx.json", [package(), package()])
    scope(tmp_path / "b.cdx.json", [package(licenses=[declaration, declaration])])
    result = inventory.collect(tmp_path)
    assert result["component_count"] == 1
    assert result["missing_license_metadata"] == 0
    assert result["components"][0]["declared_licenses"] == [declaration]
    assert result["components"][0]["scopes"] == ["a.cdx.json", "b.cdx.json"]
    assert result["legal_approved"] is False and result["notices_complete"] is False


def test_file_entries_are_retained_but_not_counted_as_package_gaps(modules, tmp_path):
    inventory, _ = modules
    scope(tmp_path / "a.cdx.json", [package(), package(purl="", type="file"),
                                   package(purl="", type="library")])
    result = inventory.collect(tmp_path)
    assert result["component_count"] == 3  # Identical names do not collapse file/package types.
    assert result["component_type_counts"] == {"file": 1, "library": 2}
    assert result["non_file_component_count"] == 2
    assert result["missing_license_metadata"] == 3
    assert result["non_file_missing_license_metadata"] == 2


def test_conflicting_component_types_are_explicitly_ambiguous(modules, tmp_path):
    inventory, _ = modules
    scope(tmp_path / "a.cdx.json", [package(), package(type="application")])
    result = inventory.collect(tmp_path)
    assert result["components"][0]["component_type"] == "ambiguous"
    assert result["non_file_component_count"] == 1


@pytest.mark.parametrize("changes", [{"name": "different-package"}, {"version": "2.0.0"}])
def test_same_purl_cannot_merge_license_evidence_for_different_identity(modules, tmp_path, changes):
    inventory, _ = modules
    scope(tmp_path / "a.cdx.json", [package()])
    scope(tmp_path / "b.cdx.json", [package(**changes, licenses=[{"license": {"id": "MIT"}}])])
    with pytest.raises(ValueError, match="Conflicting component identity"):
        inventory.collect(tmp_path)


def evidence():
    wheel_url = "https://files.pythonhosted.org/packages/synthetic/synthetic-1.2.3.whl"
    metadata = (b"Metadata-Version: 2.4\nName: synthetic-uninstalled\nVersion: 1.2.3\n"
                b"License-Expression: MIT\nLicense-File: LICENSE\n\n")
    release = {"info": {"name": "synthetic-uninstalled", "version": "1.2.3"}, "urls": [{
        "filename": "synthetic-1.2.3.whl", "url": wheel_url, "packagetype": "bdist_wheel",
        "digests": {"sha256": "a" * 64},
        "core-metadata": {"sha256": hashlib.sha256(metadata).hexdigest()}}]}
    return wheel_url, metadata, release


def fetch_record(fetcher, release, metadata, locked):
    index = json.dumps(release).encode()
    calls = []
    def reader(url, deadline):
        assert deadline > time.monotonic()
        calls.append(url)
        return index if url.startswith("https://pypi.org/") else metadata
    result = fetcher.fetch_one(package(), locked, time.monotonic() + 5, reader=reader)
    return result, calls


def test_core_metadata_joins_only_the_exact_locked_wheel_with_verified_digest(modules, tmp_path):
    inventory, fetcher = modules
    wheel, metadata, release = evidence()
    result, calls = fetch_record(fetcher, release, metadata, {wheel: "a" * 64})
    assert calls == ["https://pypi.org/pypi/synthetic-uninstalled/1.2.3/json", wheel + ".metadata"]
    assert result["core_metadata_sha256"] == hashlib.sha256(metadata).hexdigest()
    assert result["license_expression"] == "MIT"
    assert result["license_files"] == ["LICENSE"]
    assert result["platform_coverage"] == "one_locked_wheel_not_all_variants"
    scope(tmp_path / "a.cdx.json", [package()])
    report = tmp_path / "metadata.json"
    report.write_text(json.dumps({"schema_version": "locked-license-metadata-v1", "entries": [result]}))
    joined = inventory.collect(tmp_path, report)
    assert joined["non_file_missing_license_metadata"] == 0
    assert joined["legal_approved"] is False and joined["notices_complete"] is False
    result["version"] = "1.2.4"
    report.write_text(json.dumps({"schema_version": "locked-license-metadata-v1", "entries": [result]}))
    with pytest.raises(ValueError, match="outside"):
        inventory.collect(tmp_path, report)


@pytest.mark.parametrize("failure", ["archive", "core_digest", "metadata_identity", "release_identity", "origin"])
def test_changed_or_unlocked_metadata_never_becomes_license_evidence(modules, failure):
    _, fetcher = modules
    wheel, metadata, release = evidence()
    locked = {wheel: "a" * 64}
    if failure == "archive":
        locked[wheel] = "b" * 64
    elif failure == "core_digest":
        metadata += b"tampered"
    elif failure == "metadata_identity":
        metadata = metadata.replace(b"Version: 1.2.3", b"Version: 1.2.4")
        release["urls"][0]["core-metadata"]["sha256"] = hashlib.sha256(metadata).hexdigest()
    elif failure == "release_identity":
        release["info"]["version"] = "1.2.4"
    else:
        release["urls"][0]["url"] = "https://foreign.invalid/synthetic.whl"
        locked = {release["urls"][0]["url"]: "a" * 64}
    with pytest.raises(ValueError):
        fetch_record(fetcher, release, metadata, locked)


@pytest.mark.parametrize("url", ["http://pypi.org/pypi/a/json", "https://foreign.invalid/a",
                                "https://pypi.org:443/a", "https://user@pypi.org/a",
                                "https://pypi.org/a?token=synthetic", "https://pypi.org/a#fragment"])
def test_fetch_refuses_foreign_origins_before_network_access(modules, url):
    with pytest.raises(ValueError, match="origin"):
        modules[1].read_url(url, time.monotonic() + 5)


def test_dripping_response_process_is_terminated_at_total_deadline(modules, monkeypatch):
    _, fetcher = modules
    run = subprocess.run
    def dripping(_arguments, **options):
        assert options["timeout"] <= .15
        # Continuously readable output cannot extend the caller's deadline.
        return run([sys.executable, "-c",
                    "import sys,time\nwhile True:\n sys.stdout.write('x');sys.stdout.flush();time.sleep(.01)"],
                   **options)
    monkeypatch.setattr(fetcher.subprocess, "run", dripping)
    began = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        fetcher.read_url("https://pypi.org/pypi/synthetic/1/json", began + .15)
    assert time.monotonic() - began < 2


def test_unavailable_optional_package_is_preserved_as_a_gap(modules, monkeypatch, tmp_path):
    _, fetcher = modules
    scope(tmp_path / "a.cdx.json", [package()])
    lock = tmp_path / "uv.lock"
    lock.write_text('[[package]]\nname = "synthetic-uninstalled"\nversion = "1.2.3"\n')
    def refused(*args, **kwargs):
        raise ValueError("private-diagnostic-canary")
    monkeypatch.setattr(fetcher, "fetch_one", refused)
    result = fetcher.fetch(tmp_path, lock)
    assert result["entries"] == [] and len(result["missing"]) == 1
    assert "private-diagnostic-canary" not in json.dumps(result)
    assert result["legal_approved"] is False and result["notices_complete"] is False


def test_available_metadata_with_no_license_declaration_remains_a_license_gap(modules, monkeypatch, tmp_path):
    inventory, fetcher = modules
    scope(tmp_path / "a.cdx.json", [package()])
    lock = tmp_path / "uv.lock"
    lock.write_text('[[package]]\nname = "synthetic-uninstalled"\nversion = "1.2.3"\n')
    monkeypatch.setattr(fetcher, "fetch_one", lambda *args, **kwargs: package())
    result = fetcher.fetch(tmp_path, lock)
    assert len(result["entries"]) == 1 and result["missing"] == []
    assert result["metadata_without_license_declaration"] == [package()["purl"]]
    report = tmp_path / "metadata.json"
    report.write_text(json.dumps(result))
    assert inventory.collect(tmp_path, report)["non_file_missing_license_metadata"] == 1
