"""Fetch pinned publisher notice texts and small, independently checkable proofs.

No packages are installed or executed. Registry attestation contents are checked
for consistency; this helper does not verify Sigstore signatures, approve
licenses, or prove that a published binary was reproducibly built from source.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

MAX_CATALOG = 1024 * 1024
MAX_LOCK = 16 * 1024 * 1024
MAX_TEXT = 256 * 1024
MAX_PROOF = 512 * 1024
MAX_TOTAL = 8 * 1024 * 1024
MAX_ENTRIES = 128
MAX_FILES = 192
MAX_SECONDS = 90
RECEIPT = "publisher-notices.json"


def _canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate publisher evidence field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def _read(path: Path, limit: int) -> bytes:
    for parent in (path, *path.absolute().parents):
        if parent.is_symlink():
            raise ValueError("Publisher evidence symlinks refused")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Regular single-link publisher evidence required")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Publisher evidence byte budget exceeded")
    return raw


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Exact publisher SHA256 required")
    return value


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(ord(char) < 32 or char == "\\" for char in value)
            or PurePosixPath(value).is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("Unsafe publisher evidence path")
    return value


def _url(url):
    if not isinstance(url, str) or len(url) > 2048 or any(char.isspace() or ord(char) < 32 for char in url):
        raise ValueError("Publisher origin refused")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {
            "raw.githubusercontent.com", "api.github.com", "registry.npmjs.org"}
            or parsed.port or parsed.username or parsed.password or parsed.query
            or parsed.fragment or "\\" in url):
        raise ValueError("Publisher origin refused")
    _path(unquote(parsed.path).lstrip("/"))
    return parsed


def _repo(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("Exact publisher repository required")
    return value


def _raw_url(url, repository, commit):
    parsed = _url(url)
    if parsed.hostname != "raw.githubusercontent.com" or not parsed.path.startswith(f"/{repository}/{commit}/"):
        raise ValueError("Immutable publisher revision disagrees")


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:sha256:[a-f0-9]{64}|sha512:[a-f0-9]{128})", value):
        raise ValueError("Exact locked archive digest required")
    return value


def _integrity(value):
    match = re.fullmatch(r"(sha256|sha512)-([A-Za-z0-9+/]+={0,2})", value or "")
    if not match:
        raise ValueError("Exact npm integrity required")
    raw = base64.b64decode(match[2], validate=True)
    if len(raw) != hashlib.new(match[1]).digest_size:
        raise ValueError("Invalid npm integrity")
    return f"{match[1]}:{raw.hex()}"


def _lock_owners(uv_raw, npm_raw):
    owners = set()
    for package in tomllib.loads(uv_raw.decode())["package"]:
        for artifact in [*package.get("wheels", []), *([package["sdist"]] if package.get("sdist") else [])]:
            owners.add(("pypi", _canonical(package["name"]), package["version"], artifact["url"], _digest(artifact["hash"])))
    npm = _json(npm_raw)
    if npm.get("lockfileVersion") != 3 or not isinstance(npm.get("packages"), dict):
        raise ValueError("npm lockfile v3 required")
    for path, package in npm["packages"].items():
        if path and "node_modules/" in path:
            owners.add(("npm", package.get("name") or path.rsplit("node_modules/", 1)[1],
                        package["version"], package["resolved"], _integrity(package["integrity"])))
    return owners


def _owner(entry):
    ecosystem, name, version = (entry.get(key) for key in ("ecosystem", "name", "version"))
    if (ecosystem not in {"pypi", "npm"} or not isinstance(name, str) or not name
            or not isinstance(version, str) or not version or len(name) > 256 or len(version) > 128):
        raise ValueError("Exact publisher package identity required")
    return ecosystem, _canonical(name) if ecosystem == "pypi" else name, version


def _archive_url(entry):
    value = entry.get("locked_archive_url")
    if not isinstance(value, str) or len(value) > 2048 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Credential-free locked publisher archive required")
    parsed = urlsplit(value)
    expected = "registry.npmjs.org" if entry["ecosystem"] == "npm" else "files.pythonhosted.org"
    if (parsed.scheme != "https" or parsed.hostname != expected or parsed.port
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Credential-free locked publisher archive required")
    _path(unquote(parsed.path).lstrip("/"))


def _binding_shape(entry):
    commit = entry.get("source_commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Exact publisher source commit required")
    binding = entry.get("binding", {})
    repo = _repo(binding.get("repository"))
    _raw_url(entry["source_url"], repo, commit)
    kind = binding.get("kind")
    if kind == "registry_attestation" and binding.get("attestation_signatures_verified") is not False:
        raise ValueError("Independent attestation signature verification is not performed")
    expected = {"registry_git_head": {"registry"}, "registry_attestation": {"registry", "attestation"},
                "version_manifest": {"version_manifest", "release_tag"}}.get(kind)
    proofs = binding.get("proofs")
    if expected is None or not isinstance(proofs, list) or not 1 <= len(proofs) <= 5:
        raise ValueError("Explicit publisher binding proofs required")
    roles = [proof.get("role") for proof in proofs]
    allowed = expected | ({"peeled_tag", "package_manifest"} if kind == "version_manifest" else set())
    if len(roles) != len(set(roles)) or not expected <= set(roles) or not set(roles) <= allowed:
        raise ValueError("Publisher proof roles disagree")
    for proof in proofs:
        _sha(proof.get("sha256"))
        parsed = _url(proof.get("url"))
        role = proof["role"]
        if role in {"registry", "attestation"}:
            if entry["ecosystem"] != "npm" or parsed.hostname != "registry.npmjs.org":
                raise ValueError("Registry binding owner disagrees")
            path = f"/{entry['name']}/{entry['version']}" if role == "registry" else f"/-/npm/v1/attestations/{entry['name']}@{entry['version']}"
            if unquote(parsed.path) != path:
                raise ValueError("Registry binding version disagrees")
        elif role in {"version_manifest", "package_manifest"}:
            _raw_url(proof["url"], repo, commit)
        elif (parsed.hostname != "api.github.com" or not parsed.path.startswith(f"/repos/{repo}/git/")):
            raise ValueError("Publisher tag repository disagrees")


def _catalog_from_bytes(raw, uv_raw, npm_raw):
    data = _json(raw)
    if (data.get("schema_version") != "publisher-notices-v1" or data.get("legal_approved") is not False
            or data.get("attestation_signatures_verified", False) is not False
            or data.get("corresponding_source_verified", False) is not False):
        raise ValueError("Unapproved publisher catalogue required")
    owners = _lock_owners(uv_raw, npm_raw)
    entries, members = data.get("entries"), data.get("archive_members", [])
    if not isinstance(entries, list) or not isinstance(members, list) or len(entries) + len(members) > MAX_ENTRIES:
        raise ValueError("Publisher catalogue entry budget exceeded")
    ids = set()
    for entry in [*entries, *members]:
        if not isinstance(entry.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", entry["id"]) or entry["id"] in ids:
            raise ValueError("Unique publisher entry id required")
        ids.add(entry["id"])
        _archive_url(entry)
        identity = (*_owner(entry), entry.get("locked_archive_url"), _digest(entry.get("locked_archive_digest")))
        if identity not in owners:
            raise ValueError("Publisher notice owner disagrees with exact lock")
        _sha(entry.get("sha256"))
        if entry in entries:
            _binding_shape(entry)
        else:
            _path(entry.get("member"))
    if len(proof_specs(data)) + len({entry["sha256"] for entry in entries}) > MAX_FILES:
        raise ValueError("Publisher file budget exceeded")
    return data, hashlib.sha256(raw).hexdigest()


def read_catalog(catalog: Path, uv_lock: Path, npm_lock: Path):
    return _catalog_from_bytes(_read(catalog, MAX_CATALOG), _read(uv_lock, MAX_LOCK),
                               _read(npm_lock, MAX_LOCK))


def proof_specs(catalog):
    result = {}
    for entry in catalog["entries"]:
        for proof in entry["binding"]["proofs"]:
            spec = {"url": proof["url"], "sha256": proof["sha256"]}
            existing = result.setdefault(proof["sha256"], spec)
            if existing != spec:
                raise ValueError("Conflicting publisher proof origins")
    return result


def _field(data, path):
    if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,5}", path):
        raise ValueError("Explicit manifest field required")
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            raise ValueError("Publisher manifest field missing")
        data = data[part]
    return data


def _manifest(proof, raw):
    if proof.get("format") == "json":
        return _json(raw)
    if proof.get("format") == "toml":
        return tomllib.loads(raw.decode())
    if proof.get("format") == "python_literal":
        # Parse literal version assignments; never import or execute source.
        values = []
        try:
            nodes = ast.parse(raw.decode()).body
        except SyntaxError:
            raise ValueError("Malformed publisher literal source") from None
        for node in nodes:
            if (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)):
                if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                    raise ValueError("Literal publisher version required")
                values.append(node.value.value)
        if len(values) != 1:
            raise ValueError("Unique literal publisher version required")
        return {"__version__": values[0]}
    raise ValueError("Explicit publisher manifest format required")


def _repository(value):
    value = value.get("url") if isinstance(value, dict) else value
    if not isinstance(value, str):
        return None
    value = value.removeprefix("git+").removesuffix(".git")
    match = re.fullmatch(r"(?:https://github\.com/|git://github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", value)
    return match[1].lower() if match else None


def validate_bound_entry(entry, proof_bytes):
    _binding_shape(entry)
    binding = entry["binding"]
    proofs = {proof["role"]: proof for proof in binding["proofs"]}
    raw = {}
    for role, proof in proofs.items():
        value = proof_bytes.get(proof["sha256"])
        if not isinstance(value, bytes) or len(value) > MAX_PROOF or hashlib.sha256(value).hexdigest() != proof["sha256"]:
            raise ValueError("Publisher binding proof integrity disagrees")
        raw[role] = value
    kind, commit, repo = binding["kind"], entry["source_commit"], binding["repository"]
    if kind in {"registry_git_head", "registry_attestation"}:
        record = _json(raw["registry"])
        if (record.get("name") != entry["name"] or record.get("version") != entry["version"]
                or record.get("dist", {}).get("tarball") != entry["locked_archive_url"]
                or _integrity(record.get("dist", {}).get("integrity")) != entry["locked_archive_digest"]
                or _repository(record.get("repository")) != repo.lower()):
            raise ValueError("Registry proof disagrees with locked owner")
        if kind == "registry_git_head":
            if record.get("gitHead") != commit:
                raise ValueError("Registry gitHead disagrees")
            return
        if unquote(record.get("dist", {}).get("attestations", {}).get("url", "")) != unquote(proofs["attestation"]["url"]):
            raise ValueError("Registry attestation origin disagrees")
        statements = []
        for item in _json(raw["attestation"]).get("attestations", []):
            if item.get("predicateType") == "https://slsa.dev/provenance/v1":
                envelope = item.get("bundle", {}).get("dsseEnvelope", {})
                payload = base64.b64decode(envelope.get("payload", ""), validate=True)
                if len(payload) > MAX_PROOF:
                    raise ValueError("Attestation payload budget exceeded")
                statements.append(_json(payload))
        if len(statements) != 1:
            raise ValueError("Unique registry provenance statement required")
        statement = statements[0]
        digest_kind, digest = entry["locked_archive_digest"].split(":", 1)
        purl = f"pkg:npm/{quote(entry['name'], safe='/')}@{entry['version']}"
        definition = statement.get("predicate", {}).get("buildDefinition", {})
        dependencies = definition.get("resolvedDependencies", [])
        if (statement.get("predicateType") != "https://slsa.dev/provenance/v1"
                or statement.get("subject") != [{"name": purl, "digest": {digest_kind: digest}}]
                or definition.get("externalParameters", {}).get("workflow", {}).get("repository") != f"https://github.com/{repo}"
                or not any(item.get("uri", "").startswith(f"git+https://github.com/{repo}@")
                           and item.get("digest", {}).get("gitCommit") == commit for item in dependencies)):
            raise ValueError("Registry provenance source or subject disagrees")
        return
    manifest_proof = proofs["version_manifest"]
    manifest = _manifest(manifest_proof, raw["version_manifest"])
    if _field(manifest, manifest_proof["version_field"]) != entry["version"]:
        raise ValueError("Publisher manifest version disagrees")
    if "name_field" in manifest_proof:
        name = _field(manifest, manifest_proof["name_field"])
        if _canonical(name) != _canonical(entry["name"]):
            raise ValueError("Publisher manifest name disagrees")
    elif "package_manifest" in proofs:
        package_proof = proofs["package_manifest"]
        package = _manifest(package_proof, raw["package_manifest"])
        if (_canonical(_field(package, package_proof["name_field"])) != _canonical(entry["name"])
                or _field(package, "package.version") != {"workspace": True}):
            raise ValueError("Publisher workspace package identity disagrees")
    elif (manifest_proof.get("format") != "python_literal"
          or PurePosixPath(urlsplit(manifest_proof["url"]).path).parent.name != entry["name"]):
        raise ValueError("Publisher module identity disagrees")
    tag = _json(raw["release_tag"])
    tags = tag if isinstance(tag, list) else [tag]
    ref = proofs["release_tag"].get("ref")
    matching = [item for item in tags if item.get("ref") == ref]
    if len(matching) != 1 or not isinstance(ref, str) or ref.rsplit("/", 1)[-1] not in {entry["version"], "v" + entry["version"], entry["name"] + "@" + entry["version"]}:
        raise ValueError("Exact publisher release tag required")
    obj = matching[0].get("object", {})
    if obj.get("type") == "tag":
        if "peeled_tag" not in proofs or proofs["peeled_tag"]["url"] != f"https://api.github.com/repos/{repo}/git/tags/{obj.get('sha')}":
            raise ValueError("Annotated publisher tag proof required")
        obj = _json(raw["peeled_tag"]).get("object", {})
    if obj.get("type") != "commit" or obj.get("sha") != commit:
        raise ValueError("Publisher release commit disagrees")


def _inventory(directory: Path):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Publisher evidence directory refused")
    files, directories = set(), set()
    for parent, dirs, names in os.walk(directory, followlinks=False):
        for name in [*dirs, *names]:
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError("Publisher evidence symlinks refused")
        directories.update((Path(parent) / name).relative_to(directory).as_posix() for name in dirs)
        files.update((Path(parent) / name).relative_to(directory).as_posix() for name in names)
        if len(files) + len(directories) > MAX_FILES + 3:
            raise ValueError("Publisher evidence file budget exceeded")
    return files, directories


def _load_proof_directory(directory, catalog):
    specs = proof_specs(catalog)
    files, directories = _inventory(directory)
    if files != set(specs) or directories:
        raise ValueError("Publisher proof inventory disagrees")
    result, total = {}, 0
    for digest in specs:
        value = _read(directory / digest, MAX_PROOF)
        total += len(value)
        if hashlib.sha256(value).hexdigest() != digest or total > MAX_TOTAL:
            raise ValueError("Publisher proof integrity or budget disagrees")
        result[digest] = value
    for entry in catalog["entries"]:
        validate_bound_entry(entry, result)
    return result


def load_proofs(publisher_dir: Path, catalog):
    return _load_proof_directory(publisher_dir / "proofs", catalog)


def validate_bundle_proofs(bundle_dir: Path, catalog):
    return _load_proof_directory(bundle_dir / "publisher-proofs", catalog)


def validate_receipt(publisher_dir: Path, catalog, catalog_sha256):
    receipt = _json(_read(publisher_dir / RECEIPT, MAX_CATALOG))
    expected = [{"id": entry["id"], "path": f"texts/{entry['sha256']}", "sha256": entry["sha256"]} for entry in catalog["entries"]]
    if (receipt.get("schema_version") != "publisher-notices-receipt-v1"
            or receipt.get("catalog_sha256") != catalog_sha256
            or receipt.get("legal_approved") is not False
            or receipt.get("attestation_signatures_verified") is not False
            or receipt.get("entries") != expected):
        raise ValueError("Publisher receipt catalogue binding disagrees")
    files, directories = _inventory(publisher_dir)
    wanted = {RECEIPT, *(item["path"] for item in expected), *(f"proofs/{sha}" for sha in proof_specs(catalog))}
    if files != wanted or directories != {"texts", "proofs"}:
        raise ValueError("Publisher receipt inventory disagrees")
    proofs = load_proofs(publisher_dir, catalog)
    result, total, seen = [], sum(map(len, proofs.values())), set()
    for entry in catalog["entries"]:
        path = f"texts/{entry['sha256']}"
        raw = _read(publisher_dir / path, MAX_TEXT)
        raw.decode("utf-8")
        if path not in seen:
            total += len(raw)
            seen.add(path)
        if not raw.strip() or hashlib.sha256(raw).hexdigest() != entry["sha256"] or total > MAX_TOTAL:
            raise ValueError("Publisher notice integrity or budget disagrees")
        result.append({**entry, "path": path, "bytes": len(raw)})
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Publisher redirect refused")


def _download_once(url, path, limit):
    _url(url)
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "KnowledgeLayer-publisher-evidence/1"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
        if response.geturl() != url or response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise ValueError("Publisher encoding or redirect refused")
        with path.open("xb") as output:
            os.chmod(path, 0o600)
            received = 0
            while chunk := response.read(65536):
                received += len(chunk)
                if received > limit:
                    raise ValueError("Publisher download byte budget exceeded")
                output.write(chunk)


def download(url, path, limit, timeout):
    _url(url)
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "_download", url,
                    str(path), str(limit)], check=True, timeout=min(10, timeout),
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def fetch(catalog: Path, uv_lock: Path, npm_lock: Path, output: Path):
    inputs = {path: _read(path, MAX_CATALOG if path == catalog else MAX_LOCK) for path in (catalog, uv_lock, npm_lock)}
    data, digest = _catalog_from_bytes(inputs[catalog], inputs[uv_lock], inputs[npm_lock])
    for parent in (output, *output.absolute().parents):
        if parent.is_symlink():
            raise ValueError("Publisher destination symlink refused")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    (output / "texts").mkdir(mode=0o700)
    (output / "proofs").mkdir(mode=0o700)
    deadline, total = time.monotonic() + MAX_SECONDS, 0
    files = [("proofs", spec, MAX_PROOF) for spec in proof_specs(data).values()]
    notices = {}
    for entry in data["entries"]:
        notices.setdefault((entry["sha256"], entry["source_url"]), {"url": entry["source_url"], "sha256": entry["sha256"]})
    files += [("texts", spec, MAX_TEXT) for spec in notices.values()]
    for index, (directory, spec, limit) in enumerate(files):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or total >= MAX_TOTAL:
            raise ValueError("Publisher fetch deadline or total budget exceeded")
        path = output / directory / spec["sha256"]
        existing = _read(path, limit) if path.exists() else None
        destination = output / f".pending-{index}" if existing is not None else path
        budget = min(limit, MAX_TOTAL - total)
        download(spec["url"], destination, budget, remaining)
        raw = _read(destination, budget)
        raw.decode("utf-8")
        total += len(raw)
        if (hashlib.sha256(raw).hexdigest() != spec["sha256"]
                or directory == "texts" and not raw.strip()):
            raise ValueError("Publisher fetched evidence digest disagrees")
        if existing is not None:
            if raw != existing:
                raise ValueError("Shared publisher text bytes disagree")
            destination.unlink()
    load_proofs(output, data)
    for path, before in inputs.items():
        if _read(path, MAX_CATALOG if path == catalog else MAX_LOCK) != before:
            raise ValueError("Publisher inputs changed during fetch")
    if time.monotonic() > deadline:
        raise ValueError("Publisher fetch deadline exceeded")
    receipt = {"schema_version": "publisher-notices-receipt-v1", "catalog_sha256": digest,
               "legal_approved": False, "attestation_signatures_verified": False,
               "entries": [{"id": entry["id"], "path": f"texts/{entry['sha256']}", "sha256": entry["sha256"]} for entry in data["entries"]]}
    with (output / RECEIPT).open("x", encoding="utf-8") as stream:
        os.chmod(output / RECEIPT, 0o600)
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    validate_receipt(output, data, digest)
    return receipt


def main():
    if len(sys.argv) == 5 and sys.argv[1] == "_download":
        _download_once(sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("docs/publisher-notices.json"))
    parser.add_argument("--uv-lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--npm-lock", type=Path, default=Path("web/package-lock.json"))
    parser.add_argument("--output-dir", "--out", dest="out", type=Path, required=True,
                        help="New private output directory; existing directories are refused")
    args = parser.parse_args()
    fetch(args.catalog, args.uv_lock, args.npm_lock, args.out)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, UnicodeError, RecursionError,
            SyntaxError, AttributeError, subprocess.SubprocessError):
        print("Publisher notice fetch refused; no validated receipt was issued.", file=sys.stderr)
        raise SystemExit(1) from None
