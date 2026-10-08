from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def publisher():
    spec = importlib.util.spec_from_file_location(
        "publisher_notices", Path(__file__).parents[1] / "scripts/fetch_publisher_notices.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def evidence(tmp_path, publisher, monkeypatch):
    commit, digest = "1" * 40, "a" * 128
    archive_url = "https://registry.npmjs.org/synthetic/-/synthetic-1.2.3.tgz"
    registry_url = "https://registry.npmjs.org/synthetic/1.2.3"
    integrity = "sha512-" + base64.b64encode(bytes.fromhex(digest)).decode()
    registry = {"name": "synthetic", "version": "1.2.3", "gitHead": commit,
                "repository": {"url": "git+https://github.com/example/publisher.git"},
                "dist": {"tarball": archive_url, "integrity": integrity}}
    notice = b"Package-specific publisher notice fixture.\n"
    source_url = f"https://raw.githubusercontent.com/example/publisher/{commit}/LICENSE"
    entry = {"id": "synthetic-license", "ecosystem": "npm", "name": "synthetic", "version": "1.2.3",
             "source_url": source_url, "source_commit": commit, "sha256": sha(notice),
             "locked_archive_url": archive_url, "locked_archive_digest": "sha512:" + digest,
             "binding": {"kind": "registry_git_head", "repository": "example/publisher",
                         "proofs": [{"role": "registry", "url": registry_url, "sha256": sha(encoded(registry))}]}}
    catalog = {"schema_version": "publisher-notices-v1", "legal_approved": False,
               "entries": [entry], "archive_members": [], "unresolved": []}
    paths = {"catalog": tmp_path / "catalog.json", "uv": tmp_path / "uv.lock",
             "npm": tmp_path / "package-lock.json", "out": tmp_path / "output"}
    paths["catalog"].write_bytes(encoded(catalog))
    paths["uv"].write_text('version = 1\npackage = []\n')
    paths["npm"].write_bytes(encoded({"lockfileVersion": 3, "packages": {
        "": {"name": "app"}, "node_modules/synthetic": {
            "version": "1.2.3", "resolved": archive_url, "integrity": integrity}}}))
    documents = {registry_url: encoded(registry), source_url: notice}

    def download(url, path, limit, timeout):
        assert timeout > 0
        path.write_bytes(documents[url])
    monkeypatch.setattr(publisher, "download", download)
    return {"paths": paths, "catalog": catalog, "entry": entry, "registry": registry,
            "documents": documents, "download": download}


def read(publisher, evidence):
    p = evidence["paths"]
    return publisher.read_catalog(p["catalog"], p["uv"], p["npm"])


def write(evidence):
    evidence["paths"]["catalog"].write_bytes(encoded(evidence["catalog"]))


def fetch(publisher, evidence):
    p = evidence["paths"]
    return publisher.fetch(p["catalog"], p["uv"], p["npm"], p["out"])


def test_fetched_texts_and_offline_bundle_revalidate_exact_proof_bytes(publisher, evidence, tmp_path):
    receipt = fetch(publisher, evidence)
    catalog, digest = read(publisher, evidence)
    rows = publisher.validate_receipt(evidence["paths"]["out"], catalog, digest)
    assert rows == [{**evidence["entry"], "path": "texts/" + evidence["entry"]["sha256"],
                     "bytes": len(evidence["documents"][evidence["entry"]["source_url"]])}]
    assert receipt["legal_approved"] is False
    assert receipt["attestation_signatures_verified"] is False
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    shutil.copytree(evidence["paths"]["out"] / "proofs", bundle / "publisher-proofs")
    assert publisher.validate_bundle_proofs(bundle, catalog) == publisher.load_proofs(evidence["paths"]["out"], catalog)


@pytest.mark.parametrize("change", ["name", "version", "digest", "archive_url", "source_commit", "source_branch", "source_origin", "duplicate_id", "duplicate_proof", "legal", "archive_member"])
def test_catalogue_refuses_wrong_owner_unpinned_sources_and_ambiguous_entries(publisher, evidence, change):
    entry = evidence["entry"]
    if change in {"name", "version"}:
        entry[change] = "impostor"
    elif change == "digest":
        entry["locked_archive_digest"] = "sha512:" + "b" * 128
    elif change == "archive_url":
        entry["locked_archive_url"] += "?token=secret"
    elif change == "source_commit":
        entry["source_commit"] = "2" * 40
    elif change == "source_branch":
        entry["source_url"] = entry["source_url"].replace(entry["source_commit"], "main")
    elif change == "source_origin":
        entry["source_url"] = entry["source_url"].replace("raw.githubusercontent.com", "example.com")
    elif change == "duplicate_id":
        evidence["catalog"]["entries"].append(copy.deepcopy(entry))
    elif change == "duplicate_proof":
        entry["binding"]["proofs"].append(copy.deepcopy(entry["binding"]["proofs"][0]))
    elif change == "legal":
        evidence["catalog"]["legal_approved"] = True
    else:
        evidence["catalog"]["archive_members"] = [{**entry, "id": "readme", "member": "../README.md"}]
    write(evidence)
    with pytest.raises(ValueError):
        read(publisher, evidence)


@pytest.mark.parametrize("field", ["name", "version", "gitHead", "dist_digest", "dist_url", "repository"])
def test_registry_proof_claims_are_validated_after_hash_verification(publisher, evidence, field):
    record = evidence["registry"]
    if field == "dist_digest":
        record["dist"]["integrity"] = "sha512-" + base64.b64encode(b"b" * 64).decode()
    elif field == "dist_url":
        record["dist"]["tarball"] += ".different"
    elif field == "repository":
        record["repository"]["url"] = "https://github.com/impostor/publisher"
    else:
        record[field] = "impostor"
    proof = evidence["entry"]["binding"]["proofs"][0]
    proof["sha256"] = sha(encoded(record))
    evidence["documents"][proof["url"]] = encoded(record)
    write(evidence)
    with pytest.raises(ValueError):
        fetch(publisher, evidence)
    assert not (evidence["paths"]["out"] / publisher.RECEIPT).exists()


@pytest.mark.parametrize("changed", ["catalog", "uv", "npm"])
def test_inputs_changed_mid_fetch_cannot_produce_a_receipt(publisher, evidence, monkeypatch, changed):
    def mutation(url, path, limit, timeout):
        evidence["download"](url, path, limit, timeout)
        target = evidence["paths"][changed]
        target.write_bytes(target.read_bytes() + b"\n")
    monkeypatch.setattr(publisher, "download", mutation)
    with pytest.raises(ValueError, match="inputs changed"):
        fetch(publisher, evidence)
    assert not (evidence["paths"]["out"] / publisher.RECEIPT).exists()


@pytest.mark.parametrize("attack", ["notice", "proof", "receipt", "extra", "extra_dir", "symlink", "hardlink", "fifo"])
def test_receipt_refuses_tampered_and_unsafe_inventory(publisher, evidence, tmp_path, attack):
    fetch(publisher, evidence)
    out = evidence["paths"]["out"]
    catalog, digest = read(publisher, evidence)
    proof = next((out / "proofs").iterdir())
    if attack == "notice":
        (out / "texts" / evidence["entry"]["sha256"]).write_bytes(b"replacement")
    elif attack == "proof":
        proof.write_bytes(b"replacement")
    elif attack == "receipt":
        p = out / publisher.RECEIPT
        d = json.loads(p.read_text()); d["catalog_sha256"] = "0" * 64; p.write_bytes(encoded(d))
    elif attack == "extra":
        (out / "extra").write_text("extra")
    elif attack == "extra_dir":
        (out / "extra-dir").mkdir()
    else:
        original = proof.read_bytes(); proof.unlink()
        other = tmp_path / "outside"; other.write_bytes(original)
        if attack == "symlink":
            proof.symlink_to(other)
        elif attack == "hardlink":
            os.link(other, proof)
        else:
            os.mkfifo(proof)
    with pytest.raises(ValueError):
        publisher.validate_receipt(out, catalog, digest)


@pytest.mark.parametrize("kind", ["file", "proof", "total", "deadline", "digest", "empty"])
def test_finite_fetch_budgets_and_integrity_refuse_before_receipt(publisher, evidence, monkeypatch, kind):
    if kind == "file":
        monkeypatch.setattr(publisher, "MAX_TEXT", 4)
    elif kind == "proof":
        monkeypatch.setattr(publisher, "MAX_PROOF", 4)
    elif kind == "total":
        monkeypatch.setattr(publisher, "MAX_TOTAL", 4)
    elif kind == "deadline":
        monkeypatch.setattr(publisher, "MAX_SECONDS", 0)
    elif kind == "digest":
        evidence["documents"][evidence["entry"]["source_url"]] = b"changed"
    else:
        evidence["documents"][evidence["entry"]["source_url"]] = b""
        evidence["entry"]["sha256"] = sha(b""); write(evidence)
    with pytest.raises(ValueError):
        fetch(publisher, evidence)
    assert not (evidence["paths"]["out"] / publisher.RECEIPT).exists()


def test_existing_and_symlinked_destinations_are_not_modified(publisher, evidence, tmp_path):
    out = evidence["paths"]["out"]; out.mkdir(); (out / "preserved").write_text("keep")
    with pytest.raises(FileExistsError):
        fetch(publisher, evidence)
    assert (out / "preserved").read_text() == "keep"
    evidence["paths"]["out"] = tmp_path / "link"
    evidence["paths"]["out"].symlink_to(out, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        fetch(publisher, evidence)


@pytest.mark.parametrize("url", ["http://registry.npmjs.org/synthetic/1.2.3", "https://user:secret@registry.npmjs.org/synthetic/1.2.3", "https://registry.npmjs.org:443/synthetic/1.2.3", "https://registry.npmjs.org/synthetic/1.2.3?token=secret", "https://registry.npmjs.org/%2e%2e/secret", "https://localhost/notice"])
def test_origin_controls_refuse_credentials_redirect_workarounds_and_traversal(publisher, url):
    with pytest.raises(ValueError):
        publisher._url(url)


def test_download_has_a_hard_process_timeout_and_refuses_redirects(publisher, tmp_path, monkeypatch):
    captured = {}
    def run(args, **kwargs):
        captured.update(kwargs)
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
    monkeypatch.setattr(publisher.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        publisher.download("https://registry.npmjs.org/synthetic/1.2.3", tmp_path / "download", 64, 1000)
    assert captured["timeout"] == 10
    with pytest.raises(ValueError, match="redirect"):
        publisher.NoRedirect().redirect_request(None, None, 302, "Moved", {}, "https://example.com")


def attestation(evidence, change=None):
    entry = evidence["entry"]
    registry = evidence["registry"]
    url = "https://registry.npmjs.org/-/npm/v1/attestations/synthetic@1.2.3"
    registry["dist"]["attestations"] = {"url": url}
    statement = {"predicateType": "https://slsa.dev/provenance/v1",
                 "subject": [{"name": "pkg:npm/synthetic@1.2.3", "digest": {"sha512": "a" * 128}}],
                 "predicate": {"buildDefinition": {
                     "externalParameters": {"workflow": {"repository": "https://github.com/example/publisher"}},
                     "resolvedDependencies": [{"uri": "git+https://github.com/example/publisher@refs/heads/main", "digest": {"gitCommit": "1" * 40}}]}}}
    if change == "digest":
        statement["subject"][0]["digest"]["sha512"] = "b" * 128
    elif change == "package":
        statement["subject"][0]["name"] = "pkg:npm/impostor@1.2.3"
    elif change == "commit":
        statement["predicate"]["buildDefinition"]["resolvedDependencies"][0]["digest"]["gitCommit"] = "2" * 40
    elif change == "repository":
        statement["predicate"]["buildDefinition"]["externalParameters"]["workflow"]["repository"] = "https://github.com/impostor/publisher"
    bundle = {"attestations": [{"predicateType": statement["predicateType"],
                               "bundle": {"dsseEnvelope": {"payload": base64.b64encode(encoded(statement)).decode()}}}]}
    if change == "duplicate":
        bundle["attestations"].append(copy.deepcopy(bundle["attestations"][0]))
    registry_proof = entry["binding"]["proofs"][0]
    registry_proof["sha256"] = sha(encoded(registry))
    entry["binding"]["kind"] = "registry_attestation"
    entry["binding"]["attestation_signatures_verified"] = False
    entry["binding"]["proofs"].append({"role": "attestation", "url": url, "sha256": sha(encoded(bundle))})
    evidence["documents"][registry_proof["url"]] = encoded(registry)
    evidence["documents"][url] = encoded(bundle)
    write(evidence)


@pytest.mark.parametrize("change", [None, "digest", "package", "commit", "repository", "duplicate"])
def test_attestation_checks_exact_subject_and_source_without_signature_claim(publisher, evidence, change):
    attestation(evidence, change)
    if change:
        with pytest.raises(ValueError):
            fetch(publisher, evidence)
    else:
        assert fetch(publisher, evidence)["attestation_signatures_verified"] is False


@pytest.mark.parametrize("change", [None, "version", "name", "tag", "commit"])
def test_version_manifests_and_release_tags_are_independently_checked(publisher, evidence, change):
    entry = evidence["entry"]; commit = entry["source_commit"]
    manifest_url = f"https://raw.githubusercontent.com/example/publisher/{commit}/package.json"
    tag_url = "https://api.github.com/repos/example/publisher/git/matching-refs/tags/v1.2.3"
    manifest = {"name": "synthetic", "version": "1.2.3"}
    tag = [{"ref": "refs/tags/v1.2.3", "object": {"type": "commit", "sha": commit}}]
    if change in {"version", "name"}:
        manifest[change] = "impostor"
    elif change == "tag":
        tag[0]["ref"] = "refs/tags/v1.2.30"
    elif change == "commit":
        tag[0]["object"]["sha"] = "2" * 40
    entry["binding"] = {"kind": "version_manifest", "repository": "example/publisher", "proofs": [
        {"role": "version_manifest", "url": manifest_url, "sha256": sha(encoded(manifest)),
         "format": "json", "version_field": "version", "name_field": "name"},
        {"role": "release_tag", "url": tag_url, "sha256": sha(encoded(tag)), "ref": "refs/tags/v1.2.3"}]}
    evidence["documents"].update({manifest_url: encoded(manifest), tag_url: encoded(tag)})
    write(evidence)
    if change:
        with pytest.raises(ValueError):
            fetch(publisher, evidence)
    else:
        fetch(publisher, evidence)


def test_python_source_is_parsed_as_literals_and_never_executed(publisher, tmp_path):
    sentinel = tmp_path / "executed"
    raw = f'__version__ = "1.2.3"\nraise RuntimeError("do not execute")\nopen({str(sentinel)!r}, "w").write("executed")\n'.encode()
    assert publisher._manifest({"format": "python_literal"}, raw) == {"__version__": "1.2.3"}
    assert not sentinel.exists()
    with pytest.raises(ValueError, match="Literal"):
        publisher._manifest({"format": "python_literal"}, b'__version__ = compute_version()\n')


def test_checked_in_catalogue_validates_exact_current_lock_owners(publisher):
    root = Path(__file__).parents[1]
    catalog, _ = publisher.read_catalog(root / "docs/publisher-notices.json", root / "uv.lock", root / "web/package-lock.json")
    native_owners = {name.rsplit("node_modules/", 1)[-1] for name, item in json.loads((root / "web/package-lock.json").read_text())["packages"].items()
                     if name.rsplit("node_modules/", 1)[-1].startswith("@rolldown/binding-") and item["version"] == "1.2.12"}
    assert {entry["name"] for entry in catalog["entries"] if entry["name"].startswith("@rolldown/binding-")} == native_owners


def test_shared_notice_bytes_are_fetched_from_each_pinned_origin_then_deduplicated(publisher, evidence):
    other = copy.deepcopy(evidence["entry"])
    other["id"] = "other-license"
    other["source_url"] = other["source_url"].removesuffix("LICENSE") + "LICENSE-MIT"
    evidence["catalog"]["entries"].append(other)
    evidence["documents"][other["source_url"]] = evidence["documents"][evidence["entry"]["source_url"]]
    write(evidence)
    fetch(publisher, evidence)
    assert len(list((evidence["paths"]["out"] / "texts").iterdir())) == 1
    catalog, digest = read(publisher, evidence)
    assert len(publisher.validate_receipt(evidence["paths"]["out"], catalog, digest)) == 2


def test_cli_refusal_does_not_echo_external_input_or_errors(evidence):
    p = evidence["paths"]
    p["catalog"].write_text('external-input-must-not-be-echoed')
    script = Path(__file__).parents[1] / "scripts/fetch_publisher_notices.py"
    result = subprocess.run([sys.executable, str(script), "--catalog", str(p["catalog"]),
                             "--uv-lock", str(p["uv"]), "--npm-lock", str(p["npm"]),
                             "--output-dir", str(p["out"])], capture_output=True, text=True,
                            timeout=5, check=False)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Publisher notice fetch refused; no validated receipt was issued.\n"
    assert not p["out"].exists()


@pytest.mark.parametrize("claim", [True, None])
def test_unverified_attestation_signature_claims_cannot_be_self_approved(publisher, evidence, claim):
    attestation(evidence)
    evidence["entry"]["binding"]["attestation_signatures_verified"] = claim
    write(evidence)
    with pytest.raises(ValueError, match="signature verification"):
        read(publisher, evidence)


def test_malformed_literal_source_proof_refuses_without_source_diagnostics(publisher, evidence):
    entry = evidence["entry"]; commit = entry["source_commit"]
    manifest_url = f"https://raw.githubusercontent.com/example/publisher/{commit}/synthetic/_version.py"
    tag_url = "https://api.github.com/repos/example/publisher/git/matching-refs/tags/v1.2.3"
    malformed = b'__version__ = "external-source-must-not-be-echoed\n'
    tag = encoded([{"ref": "refs/tags/v1.2.3", "object": {"type": "commit", "sha": commit}}])
    entry["binding"] = {"kind": "version_manifest", "repository": "example/publisher", "proofs": [
        {"role": "version_manifest", "url": manifest_url, "sha256": sha(malformed),
         "format": "python_literal", "version_field": "__version__"},
        {"role": "release_tag", "url": tag_url, "sha256": sha(tag), "ref": "refs/tags/v1.2.3"}]}
    evidence["documents"].update({manifest_url: malformed, tag_url: tag})
    write(evidence)
    with pytest.raises(ValueError) as exc:
        fetch(publisher, evidence)
    assert str(exc.value) == "Malformed publisher literal source"
    assert not (evidence["paths"]["out"] / publisher.RECEIPT).exists()
