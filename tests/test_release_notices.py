from __future__ import annotations

import base64
import gzip
import hashlib
import importlib
import io
import json
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def collector(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("collect_release_notices")


def wheel(path, *, name="synthetic", declared="LICENSE", notice=True, extra=()):
    metadata = (f"Metadata-Version: 2.4\nName: {name}\nVersion: 1.2.3\n"
                + (f"License-File: {declared}\n" if declared else "")
                + "Project-URL: Source, https://example.test/source\n\n").encode()
    members = [("synthetic-1.2.3.dist-info/METADATA", metadata),
               ("synthetic.py", b"raise RuntimeError('PACKAGE_CODE_MUST_NOT_RUN')")]
    if notice:
        members.append((f"synthetic-1.2.3.dist-info/licenses/{declared}", b"Synthetic copyright\nMIT terms\n"))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member, raw in [*members, *extra]:
            archive.writestr(member, raw)


def npm(path, *, extra=(), notice=True, format=tarfile.USTAR_FORMAT, metadata=None):
    info = {"name": "@team/synthetic-web", "version": "4.5.6",
            "repository": {"url": "git+https://example.test/web"}, **(metadata or {})}
    members = [("package/package.json", json.dumps(info).encode()),
               ("package/index.js", b"throw Error('PACKAGE_CODE_MUST_NOT_RUN')")]
    if notice:
        members.append(("package/LICENSE.md", b"Web copyright\nApache terms\n"))
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=format) as archive:
        for member, raw in [*members, *extra]:
            if isinstance(member, str):
                member = tarfile.TarInfo(member)
                member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw) if member.isfile() else None)
    path.write_bytes(gzip.compress(stream.getvalue(), mtime=0))


def lock_files(directory):
    wheel_path, npm_path = directory / "wheels/synthetic.whl", directory / "npm/web.tgz"
    digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    (directory / "uv.lock").write_text('''[[package]]
name = "synthetic"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/synthetic.whl", hash = "sha256:'''+digest+'" }]\n')
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(npm_path.read_bytes()).digest()).decode()
    (directory / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "": {"name": "first-party-web"}, "node_modules/@team/synthetic-web": {
            "version": "4.5.6", "resolved": "https://registry.npmjs.org/@team/synthetic-web/-/web.tgz",
            "integrity": integrity}}}))


@pytest.fixture
def evidence(tmp_path):
    (tmp_path / "wheels").mkdir()
    (tmp_path / "npm").mkdir()
    wheel(tmp_path / "wheels/synthetic.whl")
    npm(tmp_path / "npm/web.tgz")
    lock_files(tmp_path)
    for scope, component in [("python.cdx.json", {"name": "synthetic", "version": "1.2.3",
        "purl": "pkg:pypi/synthetic@1.2.3"}), ("web.cdx.json", {"name": "synthetic-web", "version": "4.5.6",
        "purl": "pkg:npm/%40team/synthetic-web@4.5.6"})]:
        (tmp_path / scope).write_text(json.dumps({"bomFormat": "CycloneDX", "components": [component]}))
    return tmp_path


def inputs(directory, **changes):
    return {"sbom_dir": directory, "scopes": ["python.cdx.json", "web.cdx.json"],
            "uv_lock": directory / "uv.lock", "npm_lock": directory / "package-lock.json",
            "platform": "linux/amd64", **changes}


def collect(collector, directory, **changes):
    return collector.collect(**inputs(directory, wheel_dir=directory / "wheels",
                                      npm_dir=directory / "npm", **changes))


def bundle(collector, directory, **changes):
    report, texts = collect(collector, directory, **changes)
    output = directory / "bundle"
    collector.write_bundle(output, report, texts)
    return output, report, texts


def test_actual_locked_text_and_pointers_are_deterministic_and_qualified(collector, evidence):
    output, report, texts = bundle(collector, evidence)
    assert (report, texts) == collect(collector, evidence)
    assert set(texts.values()) == {b"Synthetic copyright\nMIT terms\n", b"Web copyright\nApache terms\n"}
    assert report["selected_notice_coverage_complete"] and report["gaps"] == []
    assert report["legal_approved"] is False and report["notices_complete"] is False
    assert report["container_os_coverage"] == "unresolved"
    assert report["corresponding_source_obligations"] == "independent_review_required"
    for record in report["dependencies"]:
        archive = record["archives"][0]
        assert archive["integrity_verified"] is True
        assert record["notices"][0]["archive_sha256"] == archive["sha256"]
        assert record["source_pointers"][0]["archive_sha256"] == archive["sha256"]
        assert record["source_pointers"][0]["corresponding_source_verified"] is False
    receipt = collector.check_coverage(output, **inputs(evidence), require_source_pointers=True)
    assert receipt["dependencies"] == 2 and receipt["text_files"] == 2
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.rglob("*") if path.is_file())


def test_declared_unusually_named_license_file_is_collected(collector, evidence):
    wheel(evidence / "wheels/synthetic.whl", declared="legal/terms.txt")
    lock_files(evidence)
    report, texts = collect(collector, evidence)
    assert report["gaps"] == []
    assert any(notice["member"].endswith("/legal/terms.txt") for record in report["dependencies"] for notice in record["notices"])
    assert b"Synthetic copyright\nMIT terms\n" in texts.values()


def test_archive_provenance_uses_private_snapshot_if_input_changes(collector, evidence, monkeypatch):
    original = collector._wheel
    archive = evidence / "wheels/synthetic.whl"
    locked_digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    def change_original_then_parse(handle, deadline):
        wheel(archive, extra=[("synthetic-1.2.3.dist-info/NOTICE", b"Unverified replacement text")])
        return original(handle, deadline)

    monkeypatch.setattr(collector, "_wheel", change_original_then_parse)
    report, texts = collect(collector, evidence)
    python = next(item for item in report["dependencies"] if item["ecosystem"] == "pypi")
    assert python["archives"][0]["sha256"] == locked_digest
    assert hashlib.sha256(archive.read_bytes()).hexdigest() != locked_digest
    assert b"Unverified replacement text" not in texts.values()
    assert python["notices"][0]["sha256"] == hashlib.sha256(b"Synthetic copyright\nMIT terms\n").hexdigest()


def test_private_snapshot_enforces_copy_budget_and_deadline(collector, evidence, monkeypatch):
    archive = evidence / "wheels/synthetic.whl"
    monkeypatch.setattr(collector, "MAX_ARCHIVE", 1)
    with pytest.raises(ValueError, match="Archive byte budget"), collector._snapshot(archive, collector.time.monotonic() + 10):
        pytest.fail("Oversized archive was snapshotted")
    monkeypatch.setattr(collector, "MAX_ARCHIVE", archive.stat().st_size)
    with pytest.raises(ValueError, match="deadline"), collector._snapshot(archive, collector.time.monotonic() - 1):
        pytest.fail("Expired archive was snapshotted")
    with collector._snapshot(archive, collector.time.monotonic() + 10) as frozen:
        assert frozen.read() == archive.read_bytes()
    assert frozen.closed


@pytest.mark.parametrize("kind,reason", [("archive", "locked_archive_missing"), ("notice", "license_notice_text_missing"),
                                          ("declared", "declared_license_text_missing")])
def test_missing_evidence_is_explicit_and_fails_selected_coverage(collector, evidence, kind, reason):
    if kind == "archive":
        (evidence / "wheels/synthetic.whl").unlink()
    else:
        wheel(evidence / "wheels/synthetic.whl", notice=False,
              extra=[("synthetic-1.2.3.dist-info/NOTICE", b"An independent notice")] if kind == "declared" else [])
        lock_files(evidence)
    output, report, _ = bundle(collector, evidence)
    assert report["gaps"] == [{"purl": "pkg:pypi/synthetic@1.2.3", "scopes": ["python.cdx.json"], "reason": reason}]
    with pytest.raises(ValueError, match="coverage is missing"):
        collector.check_coverage(output, **inputs(evidence))


def test_unlocked_or_wrong_package_archives_cannot_supply_notices(collector, evidence):
    wheel(evidence / "wheels/synthetic.whl", name="impostor")
    with pytest.raises(ValueError, match="selected locked evidence"):
        collect(collector, evidence)
    lock_files(evidence)
    with pytest.raises(ValueError, match="package identity disagrees"):
        collect(collector, evidence)


@pytest.mark.parametrize("ecosystem", ["wheel", "npm"])
@pytest.mark.parametrize("attack", ["traversal", "duplicate", "link"])
def test_archive_members_refuse_traversal_duplicates_and_links(collector, evidence, ecosystem, attack):
    if ecosystem == "wheel":
        member = "../LICENSE" if attack == "traversal" else "synthetic.py"
        if attack == "link":
            member = zipfile.ZipInfo("linked-LICENSE")
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
        wheel(evidence / "wheels/synthetic.whl", extra=[(member, b"untrusted")])
    else:
        member = "../LICENSE" if attack == "traversal" else "package/index.js"
        if attack == "link":
            member = tarfile.TarInfo("package/linked-LICENSE")
            member.type, member.linkname = tarfile.SYMTYPE, "../outside"
        npm(evidence / "npm/web.tgz", extra=[(member, b"untrusted")])
    lock_files(evidence)
    with pytest.raises(ValueError):
        collect(collector, evidence)


def test_normal_pax_long_paths_are_compatible_but_oversized_extensions_refuse(collector, evidence, monkeypatch):
    long_path = "package/" + "subdir/" * 20 + "NOTICE"
    npm(evidence / "npm/web.tgz", extra=[(long_path, b"Long path notice")], format=tarfile.PAX_FORMAT)
    lock_files(evidence)
    report, texts = collect(collector, evidence)
    assert report["gaps"] == [] and b"Long path notice" in texts.values()
    header = tarfile.TarInfo("pax-header")
    header.type, header.size = tarfile.XHDTYPE, collector.MAX_TEXT + 1
    (evidence / "npm/web.tgz").write_bytes(gzip.compress(header.tobuf(format=tarfile.USTAR_FORMAT)))
    lock_files(evidence)
    with pytest.raises(ValueError, match="extension budget"):
        collect(collector, evidence)


def test_named_npm_package_root_is_compatible_but_mixed_roots_refuse(collector, evidence):
    path = evidence / "npm/web.tgz"
    raw = gzip.decompress(path.read_bytes())
    source, output = io.BytesIO(raw), io.BytesIO()
    with tarfile.open(fileobj=source, mode="r:") as archive, tarfile.open(fileobj=output, mode="w") as renamed:
        for member in archive:
            member.name = member.name.replace("package/", "synthetic-web/", 1)
            renamed.addfile(member, archive.extractfile(member))
    path.write_bytes(gzip.compress(output.getvalue(), mtime=0))
    lock_files(evidence)
    assert collect(collector, evidence)[0]["gaps"] == []
    npm(path, extra=[("another-root/NOTICE", b"An unrelated package notice")])
    lock_files(evidence)
    with pytest.raises(ValueError, match="exact npm package root"):
        collect(collector, evidence)


@pytest.mark.parametrize("attack", ["truncated", "trailer", "sparse"])
def test_hostile_tar_structure_is_not_accepted(collector, evidence, attack):
    path = evidence / "npm/web.tgz"
    raw = gzip.decompress(path.read_bytes())
    if attack == "truncated":
        raw = raw[:600]
    elif attack == "trailer":
        raw += b"untrusted suffix"
    else:
        header = tarfile.TarInfo("package/sparse")
        header.type = tarfile.GNUTYPE_SPARSE
        raw = header.tobuf(format=tarfile.GNU_FORMAT) + bytes(1024)
    path.write_bytes(gzip.compress(raw))
    lock_files(evidence)
    with pytest.raises((ValueError, tarfile.TarError)):
        collect(collector, evidence)


@pytest.mark.parametrize("change", ["scope", "lock", "platform", "dependency", "exclusion", "legal", "source", "text", "extra"])
def test_independent_inputs_and_text_inventory_prevent_forged_coverage(collector, evidence, change):
    output, report, _ = bundle(collector, evidence)
    arguments = inputs(evidence)
    if change == "scope":
        (evidence / "python.cdx.json").write_text((evidence / "python.cdx.json").read_text() + "\n")
    elif change == "lock":
        (evidence / "uv.lock").write_text((evidence / "uv.lock").read_text() + "\n")
    elif change == "platform":
        arguments["platform"] = "linux/arm64"
    elif change == "dependency":
        report["dependencies"].pop()
        report["gaps"] = []
    elif change == "exclusion":
        report["excluded_components"] = [{"purl": "pkg:pypi/synthetic@1.2.3", "reason": "first_party_project"}]
    elif change == "legal":
        report["legal_approved"] = True
    elif change == "source":
        report["dependencies"][0]["archives"][0]["locked_url"] = "https://example.test/forged.tgz"
    elif change == "text":
        (output / report["dependencies"][0]["notices"][0]["path"]).write_bytes(b"forged")
    else:
        (output / "extra.txt").write_text("not in the manifest")
    (output / collector.MANIFEST).write_text(json.dumps(report))
    with pytest.raises(ValueError):
        collector.check_coverage(output, **arguments)


def test_coverage_requires_independent_inputs_and_refuses_symlink_evidence(collector, evidence):
    output, report, _ = bundle(collector, evidence)
    with pytest.raises(ValueError, match="Independent selected"):
        collector.check_coverage(output)
    path = output / report["dependencies"][0]["notices"][0]["path"]
    original = evidence / "original-notice"
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(ValueError, match="symlink"):
        collector.check_coverage(output, **inputs(evidence))


def test_archive_links_and_byte_deadline_budgets_fail_closed(collector, evidence, monkeypatch):
    path = evidence / "wheels/synthetic.whl"
    os.link(path, evidence / "archive-hardlink")
    with pytest.raises(ValueError, match="evidence file"):
        collect(collector, evidence)
    (evidence / "archive-hardlink").unlink()
    monkeypatch.setattr(collector, "MAX_ARCHIVE", 1)
    with pytest.raises(ValueError, match="Archive byte budget"):
        collect(collector, evidence)
    monkeypatch.setattr(collector, "MAX_ARCHIVE", 256 * 1024**2)
    monkeypatch.setattr(collector, "MAX_SECONDS", -1)
    with pytest.raises(ValueError, match="deadline"):
        collect(collector, evidence)


def test_credentials_are_not_source_pointers_and_source_obligations_stay_unverified(collector, evidence):
    npm(evidence / "npm/web.tgz", metadata={"repository": "https://user:PRIVATE_CANARY@example.test/source",
                                          "homepage": "https://example.test/?token=PRIVATE_CANARY"})
    lock_files(evidence)
    output, report, _ = bundle(collector, evidence)
    assert "PRIVATE_CANARY" not in json.dumps(report)
    assert report["source_pointer_gaps"] == ["pkg:npm/%40team/synthetic-web@4.5.6"]
    assert collector.check_coverage(output, **inputs(evidence))["corresponding_source_verified"] is False
    with pytest.raises(ValueError, match="coverage is missing"):
        collector.check_coverage(output, **inputs(evidence), require_source_pointers=True)


def test_container_components_remain_unresolved_and_file_components_are_explicit(collector, evidence):
    path = evidence / "python.cdx.json"
    data = json.loads(path.read_text())
    data["components"] += [{"type": "operating-system", "name": "libc", "version": "1", "purl": "pkg:deb/libc@1"},
                            {"type": "file", "name": "copyright.txt"}]
    path.write_text(json.dumps(data))
    report, _ = collect(collector, evidence)
    assert report["file_components_excluded"] == 1
    assert report["gaps"] == [{"purl": "pkg:deb/libc@1", "scopes": ["python.cdx.json"],
                                "reason": "container_os_or_other_ecosystem_unresolved"}]


def test_first_party_exclusion_requires_exact_editable_lock_identity(collector, evidence):
    (evidence / "uv.lock").write_text((evidence / "uv.lock").read_text() + '''
[[package]]
name = "knowledge-layer"
version = "0.1.0"
source = { editable = "." }
''')
    path = evidence / "python.cdx.json"
    data = json.loads(path.read_text())
    data["components"].append({"name": "knowledge-layer", "version": "0.1.0", "purl": "pkg:pypi/knowledge-layer@0.1.0"})
    path.write_text(json.dumps(data))
    output, report, _ = bundle(collector, evidence)
    assert report["excluded_components"] == [{"purl": "pkg:pypi/knowledge-layer@0.1.0", "scopes": ["python.cdx.json"],
                                               "reason": "first_party_project"}]
    assert collector.check_coverage(output, **inputs(evidence))["dependencies"] == 2


def test_platform_exclusion_uses_frozen_markers_not_missing_archives_and_is_rechecked(collector, evidence):
    path = evidence / "python.cdx.json"
    data = json.loads(path.read_text())
    data["components"].append({"name": "windows-only", "version": "2.0", "purl": "pkg:pypi/windows-only@2.0"})
    path.write_text(json.dumps(data))
    requirements = evidence / "requirements.txt"
    requirements.write_text("synthetic==1.2.3\nwindows-only==2.0 ; sys_platform == 'win32'\n")
    report, _ = collect(collector, evidence)
    assert report["gaps"][0]["reason"] == "locked_archive_missing"
    output, report, _ = bundle(collector, evidence, requirements=requirements)
    assert report["requirements_input"]["sha256"] == hashlib.sha256(requirements.read_bytes()).hexdigest()
    assert report["requirements_input"]["environment"]["sys_platform"] == "linux"
    assert report["excluded_components"] == [{"purl": "pkg:pypi/windows-only@2.0", "scopes": ["python.cdx.json"],
                                               "reason": "platform_marker_false"}]
    assert collector.check_coverage(output, **inputs(evidence), requirements=requirements)["dependencies"] == 2
    requirements.write_text("synthetic==1.2.3\nwindows-only==2.0\n")
    with pytest.raises(ValueError, match="scope or lock evidence changed"):
        collector.check_coverage(output, **inputs(evidence), requirements=requirements)


def test_cli_failure_does_not_echo_archive_metadata_and_existing_bundle_is_preserved(collector, evidence, capsys):
    output, report, texts = bundle(collector, evidence)
    with pytest.raises(ValueError, match="New nonsymlink"):
        collector.write_bundle(output, report, texts)
    wheel(evidence / "wheels/synthetic.whl", name="PRIVATE_METADATA_CANARY")
    lock_files(evidence)
    result = collector.main(["--sbom-dir", str(evidence), "--scope", "python.cdx.json", "--scope", "web.cdx.json",
        "--uv-lock", str(evidence / "uv.lock"), "--npm-lock", str(evidence / "package-lock.json"),
        "--wheel-dir", str(evidence / "wheels"), "--npm-dir", str(evidence / "npm"),
        "--platform", "linux/amd64", "--output-dir", str(evidence / "unused-output")])
    assert result == 1 and "PRIVATE_METADATA_CANARY" not in capsys.readouterr().out
    assert not (evidence / "unused-output").exists()


def test_output_and_coverage_have_global_byte_and_time_budgets(collector, evidence, monkeypatch):
    output, report, texts = bundle(collector, evidence)
    monkeypatch.setattr(collector, "MAX_OUTPUT", 1)
    with pytest.raises(ValueError, match="output budget"):
        collector.write_bundle(evidence / "oversized-output", report, texts)
    assert not (evidence / "oversized-output").exists()
    with pytest.raises(ValueError, match="output byte budget"):
        collector.check_coverage(output, **inputs(evidence))
    monkeypatch.setattr(collector, "MAX_OUTPUT", 64 * 1024**2)
    monkeypatch.setattr(collector, "MAX_SECONDS", -1)
    with pytest.raises(ValueError, match="deadline"):
        collector.check_coverage(output, **inputs(evidence))


def sdist_evidence(directory, *, name="synthetic", extra=(), notice=True):
    path = directory / "sdists/synthetic-1.2.3.tar.gz"
    path.parent.mkdir(exist_ok=True)
    members = [("synthetic-1.2.3/PKG-INFO", f"Metadata-Version: 2.4\nName: {name}\nVersion: 1.2.3\n\n".encode()),
               ("synthetic-1.2.3/setup.py", b"raise RuntimeError('PACKAGE_CODE_MUST_NOT_RUN')")]
    if notice:
        members.append(("synthetic-1.2.3/LICENSE", b"Original source copyright and license\n"))
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for member, raw in [*members, *extra]:
            if isinstance(member, str):
                member = tarfile.TarInfo(member)
                member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw) if member.isfile() else None)
    path.write_bytes(gzip.compress(stream.getvalue(), mtime=0))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    lock = directory / "uv.lock"
    lock.write_text(lock.read_text() + 'sdist = { url = "https://files.pythonhosted.org/synthetic-1.2.3.tar.gz", hash = "sha256:'
                    + digest + '" }\n')
    return path


def test_locked_source_text_is_distinguished_from_wheel_and_package_code_is_not_run(collector, evidence):
    wheel(evidence / "wheels/synthetic.whl", notice=False, declared=None)
    lock_files(evidence)
    sdist_evidence(evidence)
    output, report, texts = bundle(collector, evidence, sdist_dir=evidence / "sdists")
    dependency = next(item for item in report["dependencies"] if item["ecosystem"] == "pypi")
    assert {item["kind"] for item in dependency["archives"]} == {"wheel", "sdist"}
    assert dependency["notices"][0]["member"] == "synthetic-1.2.3/LICENSE"
    assert b"Original source copyright and license\n" in texts.values()
    assert report["gaps"] == [] and report["legal_approved"] is False
    assert collector.check_coverage(output, **inputs(evidence))["dependencies"] == 2


@pytest.mark.parametrize("attack", ["identity", "digest", "traversal", "link", "mixed_root"])
def test_locked_source_identity_archive_and_member_attacks_refuse(collector, evidence, attack):
    extra = []
    if attack == "traversal":
        extra = [("../LICENSE", b"wrong")]
    elif attack == "link":
        member = tarfile.TarInfo("synthetic-1.2.3/link")
        member.type, member.linkname = tarfile.SYMTYPE, "../outside"
        extra = [(member, b"")]
    elif attack == "mixed_root":
        extra = [("other/LICENSE", b"wrong")]
    path = sdist_evidence(evidence, name="impostor" if attack == "identity" else "synthetic", extra=extra)
    if attack == "digest":
        path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError):
        collect(collector, evidence, sdist_dir=evidence / "sdists")


def test_source_archive_cannot_hide_missing_license_declared_in_binary_wheel(collector, evidence):
    wheel(evidence / "wheels/synthetic.whl", notice=False)
    lock_files(evidence)
    sdist_evidence(evidence)
    output, report, _ = bundle(collector, evidence, sdist_dir=evidence / "sdists")
    assert report["gaps"][0]["reason"] == "declared_license_text_missing"
    with pytest.raises(ValueError):
        collector.check_coverage(output, **inputs(evidence))


def publisher_evidence(directory, monkeypatch):
    publisher = importlib.import_module("fetch_publisher_notices")
    archive = json.loads((directory / "package-lock.json").read_text())["packages"]["node_modules/@team/synthetic-web"]
    commit = "1" * 40
    text = b"Synthetic original publisher copyright and terms\n"
    registry_url = "https://registry.npmjs.org/%40team%2Fsynthetic-web/4.5.6"
    source_url = f"https://raw.githubusercontent.com/example/publisher/{commit}/LICENSE"
    registry = json.dumps({"name": "@team/synthetic-web", "version": "4.5.6", "gitHead": commit,
        "repository": {"url": "git+https://github.com/example/publisher.git"},
        "dist": {"tarball": archive["resolved"], "integrity": archive["integrity"]}}).encode()
    entry = {"id": "synthetic-publisher-license", "ecosystem": "npm", "name": "@team/synthetic-web", "version": "4.5.6",
        "locked_archive_url": archive["resolved"],
        "locked_archive_digest": "sha512:" + base64.b64decode(archive["integrity"].split("-", 1)[1]).hex(),
        "source_url": source_url, "source_commit": commit, "sha256": hashlib.sha256(text).hexdigest(),
        "binding": {"kind": "registry_git_head", "repository": "example/publisher", "proofs": [{
            "role": "registry", "url": registry_url, "sha256": hashlib.sha256(registry).hexdigest()}]}}
    catalog = directory / "publisher-catalog.json"
    catalog.write_text(json.dumps({"schema_version": "publisher-notices-v1", "legal_approved": False,
                                  "entries": [entry], "archive_members": [], "unresolved": []}))
    documents = {registry_url: registry, source_url: text}
    monkeypatch.setattr(publisher, "download", lambda url, path, limit, timeout: path.write_bytes(documents[url]))
    destination = directory / "publishers"
    publisher.fetch(catalog, directory / "uv.lock", directory / "package-lock.json", destination)
    return catalog, destination, entry


def test_publisher_text_and_binding_proofs_are_distinct_and_independently_revalidated(collector, evidence, monkeypatch):
    npm(evidence / "npm/web.tgz", notice=False)
    lock_files(evidence)
    catalog, publishers, entry = publisher_evidence(evidence, monkeypatch)
    output, report, texts = bundle(collector, evidence, publisher_catalog=catalog, publisher_dir=publishers)
    web = next(item for item in report["dependencies"] if item["ecosystem"] == "npm")
    assert web["notices"] == [] and web["publisher_notices"][0]["source_commit"] == entry["source_commit"]
    assert report["schema_version"] == "release-notices-v2" and report["gaps"] == []
    assert any(path.startswith("publisher-proofs/") for path in texts)
    checked = collector.check_coverage(output, **inputs(evidence), publisher_catalog=catalog)
    assert checked["text_files"] == 2 and checked["publisher_proof_files"] == 1
    assert checked["legal_approved"] is False and checked["notices_complete"] is False


@pytest.mark.parametrize("change", ["catalogue", "binding", "proof", "dropped_proof", "dropped_notice", "notice", "extra_proof"])
def test_changed_or_dropped_publisher_evidence_cannot_forge_selected_coverage(collector, evidence, monkeypatch, change):
    npm(evidence / "npm/web.tgz", notice=False)
    lock_files(evidence)
    catalog, publishers, _ = publisher_evidence(evidence, monkeypatch)
    output, report, _ = bundle(collector, evidence, publisher_catalog=catalog, publisher_dir=publishers)
    web = next(item for item in report["dependencies"] if item["ecosystem"] == "npm")
    if change == "catalogue":
        catalog.write_text(catalog.read_text() + "\n")
    elif change == "binding":
        web["publisher_notices"][0]["binding"]["repository"] = "impostor/source"
    elif change in {"proof", "dropped_proof"}:
        proof = next((output / "publisher-proofs").iterdir())
        if change == "proof":
            proof.write_bytes(b"forged proof")
        else:
            proof.unlink()
    elif change == "dropped_notice":
        web["publisher_notices"] = []
        report["gaps"] = []
        report["selected_notice_coverage_complete"] = True
    elif change == "notice":
        (output / web["publisher_notices"][0]["path"]).write_bytes(b"forged publisher terms")
    else:
        (output / "publisher-proofs/extra").write_bytes(b"extra")
    (output / collector.MANIFEST).write_text(json.dumps(report))
    with pytest.raises((ValueError, FileNotFoundError)):
        collector.check_coverage(output, **inputs(evidence), publisher_catalog=catalog)


def test_exact_readme_license_member_selection_is_rechecked_against_catalogue(collector, evidence):
    raw = b"Package README with its original license section and copyright\n"
    npm(evidence / "npm/web.tgz", notice=False, extra=[("package/README.md", raw)])
    lock_files(evidence)
    archive = json.loads((evidence / "package-lock.json").read_text())["packages"]["node_modules/@team/synthetic-web"]
    catalog = evidence / "archive-member-catalog.json"
    catalog.write_text(json.dumps({"schema_version": "publisher-notices-v1", "legal_approved": False,
        "entries": [], "archive_members": [{"id": "synthetic-readme", "ecosystem": "npm",
            "name": "@team/synthetic-web", "version": "4.5.6", "locked_archive_url": archive["resolved"],
            "locked_archive_digest": "sha512:" + base64.b64decode(archive["integrity"].split("-", 1)[1]).hex(),
            "member": "package/README.md", "sha256": hashlib.sha256(raw).hexdigest()}]}))
    output, report, texts = bundle(collector, evidence, publisher_catalog=catalog)
    assert report["gaps"] == [] and raw in texts.values()
    assert collector.check_coverage(output, **inputs(evidence), publisher_catalog=catalog)["text_files"] == 2
    web = next(item for item in report["dependencies"] if item["ecosystem"] == "npm")
    web["notices"][0]["member"] = "package/forged-README.md"
    (output / collector.MANIFEST).write_text(json.dumps(report))
    with pytest.raises(ValueError, match="archive notice catalogue"):
        collector.check_coverage(output, **inputs(evidence), publisher_catalog=catalog)
