"""Collect actual notices from locked archives and pinned publishers; never approve licenses.

No network, installation, package code execution or archive extraction is used.
Selected SBOM scopes and the operator's platform label remain explicit. Image/OS,
other platform variants and corresponding-source obligations remain unresolved.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import stat
import struct
import tarfile
import tempfile
import time
import tomllib
import zipfile
import zlib
from contextlib import contextmanager
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

MAX_ARCHIVE = 256 * 1024**2
MAX_EXPANDED = 512 * 1024**2
MAX_TEXT = 4 * 1024**2
MAX_METADATA = 16 * 1024**2
MAX_OUTPUT = 64 * 1024**2
MAX_ITEMS = 25_000
MAX_DEPENDENCIES = 1024
MAX_ARCHIVES = 512
MAX_SECONDS = 300
MANIFEST = "third-party-notices.json"


def canonical(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate evidence field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def _path(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value.encode()) > 512
            or any(ord(char) < 32 or char == "\\" for char in value)):
        raise ValueError("Unsafe evidence path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} for part in value.split("/")) or str(path) != value:
        raise ValueError("Unsafe evidence path")
    return value


def _open(path: Path):
    for parent in (path, *path.absolute().parents):
        if parent.is_symlink():
            raise ValueError("Evidence symlinks are refused")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    handle = os.fdopen(descriptor, "rb")
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        handle.close()
        raise ValueError("Regular unlinked evidence file required")
    return handle


def _read(path: Path, limit=MAX_METADATA) -> bytes:
    with _open(path) as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Evidence byte budget exceeded")
    return raw


def _check_time(deadline):
    if time.monotonic() > deadline:
        raise ValueError("Notice collection deadline exceeded")


@contextmanager
def _snapshot(path: Path, deadline):
    # Parse the same anonymous private snapshot whose digest is lock-verified.
    # An operator may replace/change an input while collection runs; later reads
    # of that original file cannot substitute different text for verified bytes.
    with _open(path) as source, tempfile.TemporaryFile(mode="w+b") as frozen:
        copied = 0
        while True:
            _check_time(deadline)
            chunk = source.read(65536)
            if not chunk:
                break
            copied += len(chunk)
            if copied > MAX_ARCHIVE:
                raise ValueError("Archive byte budget exceeded")
            frozen.write(chunk)
        frozen.seek(0)
        yield frozen


def _digests(handle, deadline):
    digests = {kind: hashlib.new(kind) for kind in ("sha256", "sha512")}
    count = 0
    while True:
        _check_time(deadline)
        chunk = handle.read(65536)
        if not chunk:
            handle.seek(0)
            return {kind: digest.hexdigest() for kind, digest in digests.items()}
        count += len(chunk)
        if count > MAX_ARCHIVE:
            raise ValueError("Archive byte budget exceeded")
        for digest in digests.values():
            digest.update(chunk)


def _url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    value = value.removeprefix("git+")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        return None
    return value


def _notice(path):
    return bool(re.match(r"^(?:licen[cs]e|copying|notice|copyright|authors)(?:$|[._-])",
                         PurePosixPath(path).name, re.IGNORECASE))


def _identity(component):
    purl = component.get("purl", "")
    if not isinstance(purl, str) or len(purl) > 2048:
        raise ValueError("Invalid component identity")
    ecosystem = "pypi" if purl.startswith("pkg:pypi/") else "npm" if purl.startswith("pkg:npm/") else "unresolved"
    if ecosystem == "unresolved":
        return ecosystem, component["name"], component.get("version")
    name, separator, version = unquote(purl.split("/", 1)[1].split("?", 1)[0].split("#", 1)[0]).rpartition("@")
    if not separator or version != component.get("version") or not name:
        raise ValueError("Conflicting component identity")
    if ecosystem == "pypi" and canonical(name) != canonical(component["name"]):
        raise ValueError("Conflicting component identity")
    if ecosystem == "npm" and component["name"] not in {name, name.rsplit("/", 1)[-1]}:
        raise ValueError("Conflicting component identity")
    return ecosystem, canonical(name) if ecosystem == "pypi" else name, version


def _scopes(directory, selected):
    if not selected or len(selected) != len(set(selected)) or len(selected) > 32:
        raise ValueError("Explicit unique SBOM scopes required")
    records, inputs, file_count = {}, [], 0
    for name in sorted(selected):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.cdx\.json", name):
            raise ValueError("Invalid SBOM scope")
        raw = _read(directory / name)
        data = _json(raw)
        if data.get("bomFormat") != "CycloneDX" or not isinstance(data.get("components"), list):
            raise ValueError("Invalid SBOM scope")
        if len(data["components"]) > MAX_ITEMS:
            raise ValueError("SBOM item budget exceeded")
        inputs.append({"scope": name, "sha256": hashlib.sha256(raw).hexdigest()})
        for component in data["components"]:
            if component.get("type") == "file":
                file_count += 1
                continue
            ecosystem, package, version = _identity(component)
            key = component.get("purl") or f"{component.get('type', 'unknown')}:{package}@{version}"
            record = records.setdefault(key, {"purl": key, "name": package, "version": version,
                "ecosystem": ecosystem, "scopes": [], "archives": [], "notices": [],
                "source_pointers": [], "missing_declared_license_files": []})
            if (record["ecosystem"], record["name"], record["version"]) != (ecosystem, package, version):
                raise ValueError("Conflicting component identity")
            if name not in record["scopes"]:
                record["scopes"].append(name)
    if len(records) > MAX_DEPENDENCIES:
        raise ValueError("Dependency budget exceeded")
    return records, inputs, file_count


def _locks(uv_raw, npm_raw, records):
    wanted = {(item["ecosystem"], item["name"], item["version"]) for item in records.values()}
    approved, first_party = {}, set()
    for package in tomllib.loads(uv_raw.decode())["package"]:
        identity = ("pypi", canonical(package["name"]), package["version"])
        if package.get("source") == {"editable": "."} or package.get("source") == {"virtual": "."}:
            first_party.add(identity)
        if identity not in wanted:
            continue
        artifacts = [(wheel, "wheel") for wheel in package.get("wheels", [])]
        if package.get("sdist"):
            artifacts.append((package["sdist"], "sdist"))
        for artifact, kind in artifacts:
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", artifact.get("hash", "")):
                raise ValueError("Invalid locked Python archive digest")
            if not _url(artifact["url"]):
                raise ValueError("Credential-free locked source URL required")
            approved.setdefault(("sha256", artifact["hash"][7:]), []).append((identity, artifact["url"], kind))
    npm = _json(npm_raw)
    if npm.get("lockfileVersion") != 3 or not isinstance(npm.get("packages"), dict):
        raise ValueError("npm lockfile v3 required")
    for name, package in npm["packages"].items():
        if not name or "node_modules/" not in name:
            continue
        identity = ("npm", package.get("name") or name.rsplit("node_modules/", 1)[1], package.get("version"))
        if identity not in wanted:
            continue
        integrity = package.get("integrity", "")
        match = re.fullmatch(r"(sha256|sha512)-([A-Za-z0-9+/]+={0,2})", integrity)
        if not match:
            raise ValueError("Exact npm archive integrity required")
        digest = base64.b64decode(match[2], validate=True)
        if len(digest) != hashlib.new(match[1]).digest_size:
            raise ValueError("Invalid npm archive digest")
        if not _url(package["resolved"]):
            raise ValueError("Credential-free locked source URL required")
        approved.setdefault((match[1], digest.hex()), []).append((identity, package["resolved"], "npm"))
    return approved, first_party


def _applicability(requirements, platform):
    if requirements is None:
        return {}, None
    from fetch_release_archives import requirement_markers, target_marker_environment
    raw = _read(requirements, 512 * 1024)
    markers = requirement_markers(requirements, platform)
    # Check the helper's second read did not race the evidence digest.
    if _read(requirements, 512 * 1024) != raw:
        raise ValueError("Requirements evidence changed during evaluation")
    return markers, {"sha256": hashlib.sha256(raw).hexdigest(),
                     "environment": target_marker_environment(platform)}


def _exclude(records, first_party, markers):
    excluded = []
    for key, record in list(records.items()):
        identity = (record["ecosystem"], record["name"], record["version"])
        reason = ("first_party_project" if identity in first_party else
                  "platform_marker_false" if record["ecosystem"] == "pypi"
                  and markers.get(identity[1:]) is False else None)
        if reason:
            excluded.append({"purl": key, "scopes": record["scopes"], "reason": reason})
            del records[key]
    return sorted(excluded, key=lambda item: item["purl"])


def _wheel(handle, deadline):
    # Bound the central directory before ZipFile allocates its entry inventory.
    handle.seek(0, 2)
    size = handle.tell()
    handle.seek(max(0, size - 65557))
    tail = handle.read(65557)
    marker = tail.rfind(b"PK\x05\x06")
    if marker < 0 or len(tail) - marker < 22 or tail[marker - 20:marker - 16] == b"PK\x06\x07":
        raise ValueError("Bounded non-ZIP64 wheel required")
    _, disk, central_disk, entries_disk, entries, central_size, _, comment_size = struct.unpack(
        "<4s4H2LH", tail[marker:marker + 22])
    if (disk or central_disk or entries_disk != entries or entries > MAX_ITEMS
            or central_size > MAX_TEXT or marker + 22 + comment_size != len(tail)):
        raise ValueError("Wheel directory budget exceeded")
    handle.seek(0)
    with zipfile.ZipFile(handle) as archive:
        if len(archive.infolist()) > MAX_ITEMS:
            raise ValueError("Wheel member budget exceeded")
        texts, seen, total, metadata = {}, set(), 0, []
        for member in archive.infolist():
            _check_time(deadline)
            name = _path(member.filename.rstrip("/"))
            if (name in seen or stat.S_IFMT(member.external_attr >> 16) not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or member.flag_bits & 1):
                raise ValueError("Duplicate, linked or encrypted wheel member")
            seen.add(name)
            total += member.file_size
            if total > MAX_EXPANDED:
                raise ValueError("Wheel expanded byte budget exceeded")
            if name.endswith(".dist-info/METADATA") and len(PurePosixPath(name).parts) == 2:
                if member.file_size > MAX_TEXT:
                    raise ValueError("Wheel metadata byte budget exceeded")
                metadata.append((name, BytesParser().parsebytes(archive.read(member), headersonly=True)))
            if not member.is_dir() and _notice(name):
                if not 0 < member.file_size <= MAX_TEXT:
                    raise ValueError("Notice byte budget exceeded")
                texts[name] = archive.read(member)
                if sum(map(len, texts.values())) > MAX_OUTPUT:
                    raise ValueError("Notice output byte budget exceeded")
        if len(metadata) != 1:
            raise ValueError("One exact wheel package metadata record required")
        parent, info = metadata[0]
        declared = info.get_all("License-File", [])
        missing = []
        for relative in declared:
            relative = _path(relative)
            candidates = {str(PurePosixPath(parent).parent / relative),
                          str(PurePosixPath(parent).parent / "licenses" / relative)}
            available = candidates & seen
            if not available:
                missing.append(relative)
            for name in sorted(available - texts.keys()):
                member = archive.getinfo(name)
                if not 0 < member.file_size <= MAX_TEXT or member.is_dir():
                    raise ValueError("Declared notice byte budget exceeded")
                texts[name] = archive.read(member)
                if sum(map(len, texts.values())) > MAX_OUTPUT:
                    raise ValueError("Notice output byte budget exceeded")
        pointers = []
        for value in info.get_all("Project-URL", []):
            label, _, url = value.partition(",")
            if checked := _url(url.strip()):
                pointers.append({"label": label.strip(), "url": checked})
        if checked := _url(info.get("Home-page")):
            pointers.append({"label": "Homepage", "url": checked})
        return ("pypi", canonical(info.get("Name", "")), info.get("Version")), texts, pointers, missing


class _TarBudget:
    def __init__(self, handle, deadline):
        self.handle, self.deadline = handle, deadline

    def read(self, size=-1):
        _check_time(self.deadline)
        if not 0 <= size <= MAX_TEXT or self.handle.tell() + size > MAX_EXPANDED:
            raise ValueError("Tar read budget exceeded")
        return self.handle.read(size)

    def seek(self, offset, whence=0):
        _check_time(self.deadline)
        if whence != 0 or not 0 <= offset <= MAX_EXPANDED:
            raise ValueError("Tar seek budget exceeded")
        result = self.handle.seek(offset)
        if result != offset:
            raise ValueError("Tar payload truncated")
        return result

    def tell(self):
        return self.handle.tell()


def _tar(handle, deadline, *, ecosystem="npm", selected_members=None):
    selected_members = selected_members or {}
    with gzip.GzipFile(fileobj=handle, mode="rb") as expanded:
        reader = _TarBudget(expanded, deadline)
        # Check extension sizes before automatic parsing/backport differences.
        count = 0
        while True:
            header = reader.read(512)
            if len(header) != 512:
                raise ValueError("Tar header truncated")
            if header == bytes(512):
                if reader.read(512) != bytes(512):
                    raise ValueError("Tar trailer invalid")
                while chunk := reader.read(65536):
                    if any(chunk):
                        raise ValueError("Tar trailer invalid")
                break
            member = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
            count += 1
            if count > MAX_ITEMS or member.size < 0 or member.type == tarfile.GNUTYPE_SPARSE:
                raise ValueError("Tar header budget exceeded")
            if member.type in {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME,
                               tarfile.GNUTYPE_LONGLINK} and member.size > MAX_TEXT:
                raise ValueError("Tar extension budget exceeded")
            reader.seek(reader.tell() + (member.size + 511) // 512 * 512)
        reader.seek(0)
        with tarfile.open(fileobj=reader, mode="r:", encoding="utf-8", errors="strict") as archive:
            texts, seen, roots, metadata, info = {}, set(), set(), [], None
            for count, member in enumerate(archive, 1):
                _check_time(deadline)
                name = _path(member.name.rstrip("/"))
                if count > MAX_ITEMS or name in seen or not (member.isfile() or member.isdir()):
                    raise ValueError("Duplicate, linked or special npm member")
                seen.add(name)
                parts = PurePosixPath(name).parts
                roots.add(parts[0])
                metadata_name = "package.json" if ecosystem == "npm" else "PKG-INFO"
                is_metadata = len(parts) == 2 and parts[1] == metadata_name
                if member.isfile() and (is_metadata or _notice(name) or name in selected_members):
                    if not 0 < member.size <= MAX_TEXT:
                        raise ValueError("npm text byte budget exceeded")
                    with archive.extractfile(member) as stream:
                        raw = stream.read(MAX_TEXT + 1)
                    if len(raw) != member.size:
                        raise ValueError("npm member size changed")
                    if is_metadata:
                        metadata.append(name)
                        info = _json(raw) if ecosystem == "npm" else BytesParser().parsebytes(raw, headersonly=True)
                    else:
                        if name in selected_members and hashlib.sha256(raw).hexdigest() != selected_members[name]:
                            raise ValueError("Selected archive member digest disagrees")
                        texts[name] = raw
                        if sum(map(len, texts.values())) > MAX_OUTPUT:
                            raise ValueError("Notice output byte budget exceeded")
            if info is None or ecosystem == "npm" and not isinstance(info, dict):
                raise TypeError("Archive package identity required")
            if len(metadata) != 1 or roots != {PurePosixPath(metadata[0]).parts[0]}:
                raise ValueError(f"One exact {ecosystem} package root required")
            if set(selected_members) - texts.keys():
                raise ValueError("Selected archive member is missing")
            if ecosystem == "pypi":
                pointers = []
                for value in info.get_all("Project-URL", []):
                    label, _, url = value.partition(",")
                    if checked := _url(url.strip()):
                        pointers.append({"label": label.strip(), "url": checked})
                if checked := _url(info.get("Home-page")):
                    pointers.append({"label": "Homepage", "url": checked})
                root = PurePosixPath(metadata[0]).parent
                # PKG-INFO's License-File paths belong to this source root.
                # Preserve missing declarations even if another notice exists.
                missing = []
                for relative in info.get_all("License-File", []):
                    candidate = str(root / _path(relative))
                    if candidate not in seen:
                        missing.append(relative)
                    elif candidate not in texts:
                        member = archive.getmember(candidate)
                        if not member.isfile() or not 0 < member.size <= MAX_TEXT:
                            raise ValueError("Declared source notice byte budget exceeded")
                        with archive.extractfile(member) as stream:
                            texts[candidate] = stream.read(MAX_TEXT + 1)
                        if len(texts[candidate]) != member.size or sum(map(len, texts.values())) > MAX_OUTPUT:
                            raise ValueError("Source notice output byte budget exceeded")
                return ("pypi", canonical(info.get("Name", "")), info.get("Version")), texts, pointers, missing
            pointers = []
            for label in ("repository", "homepage"):
                value = info.get(label)
                if isinstance(value, dict):
                    value = value.get("url")
                if checked := _url(value):
                    pointers.append({"label": label, "url": checked})
            return ("npm", info.get("name"), info.get("version")), texts, pointers, []


def _npm(handle, deadline):
    return _tar(handle, deadline)


def collect(*, sbom_dir: Path, scopes: list[str], uv_lock: Path, npm_lock: Path,
            wheel_dir: Path | None, npm_dir: Path | None, platform: str,
            requirements: Path | None = None, sdist_dir: Path | None = None,
            publisher_catalog: Path | None = None, publisher_dir: Path | None = None) -> tuple[dict, dict[str, bytes]]:
    if not isinstance(platform, str):
        raise TypeError("Platform label must be a string")
    if not re.fullmatch(r"[A-Za-z0-9_./+-]{1,96}", platform):
        raise ValueError("Explicit platform label required")
    deadline = time.monotonic() + MAX_SECONDS
    records, inputs, file_count = _scopes(sbom_dir, scopes)
    uv_raw, npm_raw = _read(uv_lock), _read(npm_lock)
    approved, first_party = _locks(uv_raw, npm_raw, records)
    catalog, catalog_hash = {}, None
    if publisher_catalog is not None:
        from fetch_publisher_notices import read_catalog
        catalog, catalog_hash = read_catalog(publisher_catalog, uv_lock, npm_lock)
    if publisher_dir is not None and publisher_catalog is None:
        raise ValueError("Independent publisher catalogue required")
    markers, marker_input = _applicability(requirements, platform)
    texts, excluded = {}, _exclude(records, first_party, markers)
    processed, evidence_items = set(), 0
    for directory, ecosystem, kind, suffixes in ((wheel_dir, "pypi", "wheel", {".whl"}),
            (sdist_dir, "pypi", "sdist", {".gz"}), (npm_dir, "npm", "npm", {".tgz", ".gz"})):
        if directory is None:
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Local archive directory required")
        archives = sorted(path for path in directory.iterdir() if path.suffix in suffixes)
        if len(archives) > MAX_ARCHIVES:
            raise ValueError("Archive count budget exceeded")
        for path in archives:
            with _snapshot(path, deadline) as handle:
                digests = _digests(handle, deadline)
                owners = {identity: (url, algorithm + ":" + digest) for algorithm, digest in digests.items()
                          for identity, url, locked_kind in approved.get((algorithm, digest), [])
                          if identity[0] == ecosystem and locked_kind == kind}
                if not owners:
                    raise ValueError("Local archive does not match selected locked evidence")
                if digests["sha256"] in processed:
                    continue
                processed.add(digests["sha256"])
                selected = {item["member"]: item["sha256"] for item in catalog.get("archive_members", [])
                            if (item["ecosystem"], item["name"], item["version"]) in owners
                            and (item["locked_archive_url"], item["locked_archive_digest"]) ==
                            owners[(item["ecosystem"], item["name"], item["version"])]}
                if kind == "wheel":
                    if selected:
                        raise ValueError("Selected wheel members require declared license metadata")
                    identity, found, pointers, missing = _wheel(handle, deadline)
                else:
                    identity, found, pointers, missing = _tar(handle, deadline, ecosystem=ecosystem,
                                                            selected_members=selected)
                if identity not in owners:
                    raise ValueError("Locked archive package identity disagrees")
                for record in records.values():
                    if (record["ecosystem"], record["name"], record["version"]) != identity:
                        continue
                    evidence_items += len(found) + len(pointers) + len(missing)
                    if evidence_items > MAX_ITEMS:
                        raise ValueError("Notice evidence item budget exceeded")
                    evidence = {"sha256": digests["sha256"], "locked_url": owners[identity][0],
                                "locked_digest": owners[identity][1],
                                "filename": path.name, "kind": kind, "integrity_verified": True}
                    record["archives"].append(evidence)
                    record["missing_declared_license_files"].extend(missing)
                    for member, raw in sorted(found.items()):
                        checksum = hashlib.sha256(raw).hexdigest()
                        target = f"texts/{checksum}.txt"
                        texts[target] = raw
                        record["notices"].append({"path": target, "member": member, "bytes": len(raw),
                            "sha256": checksum, "archive_sha256": digests["sha256"]})
                    for pointer in pointers:
                        record["source_pointers"].append({**pointer, "archive_sha256": digests["sha256"],
                            "corresponding_source_verified": False})
            if sum(map(len, texts.values())) > MAX_OUTPUT:
                raise ValueError("Notice output byte budget exceeded")
    publisher_proofs = {}
    if publisher_dir is not None:
        from fetch_publisher_notices import load_proofs, validate_bound_entry, validate_receipt
        publisher_proofs = load_proofs(publisher_dir, catalog)
        for checksum, raw in publisher_proofs.items():
            texts[f"publisher-proofs/{checksum}"] = raw
        for evidence in validate_receipt(publisher_dir, catalog, catalog_hash):
            validate_bound_entry(evidence, publisher_proofs)
            identity = (evidence["ecosystem"], evidence["name"], evidence["version"])
            raw = _read(publisher_dir / evidence["path"], MAX_TEXT)
            checksum = hashlib.sha256(raw).hexdigest()
            if checksum != evidence["sha256"] or len(raw) != evidence["bytes"]:
                raise ValueError("Publisher notice evidence changed during collection")
            target = f"texts/{checksum}.txt"
            for record in records.values():
                if (record["ecosystem"], record["name"], record["version"]) == identity:
                    texts[target] = raw
                    record.setdefault("publisher_notices", []).append({**evidence, "path": target})
            if sum(map(len, texts.values())) > MAX_OUTPUT:
                raise ValueError("Notice output byte budget exceeded")
    # Re-read independently supplied inputs before emitting a bound receipt.
    if (_read(uv_lock) != uv_raw or _read(npm_lock) != npm_raw
            or any(hashlib.sha256(_read(sbom_dir / item["scope"])).hexdigest() != item["sha256"] for item in inputs)
            or publisher_catalog is not None and hashlib.sha256(_read(publisher_catalog)).hexdigest() != catalog_hash
            or marker_input is not None and hashlib.sha256(_read(requirements, 512 * 1024)).hexdigest() != marker_input["sha256"]):
        raise ValueError("Notice selection inputs changed during collection")
    dependencies = sorted(records.values(), key=lambda item: item["purl"])
    gaps = []
    for record in dependencies:
        reason = ("container_os_or_other_ecosystem_unresolved" if record["ecosystem"] == "unresolved"
                  else "locked_archive_missing" if not record["archives"]
                  else "license_notice_text_missing" if not record["notices"] and not record.get("publisher_notices")
                  else "declared_license_text_missing" if record["missing_declared_license_files"] else None)
        if reason:
            gaps.append({"purl": record["purl"], "scopes": record["scopes"], "reason": reason})
    report = {"schema_version": "release-notices-v2", "platform": platform, "scopes": sorted(scopes),
        "scope_inputs": inputs, "lock_inputs": {"uv_sha256": hashlib.sha256(uv_raw).hexdigest(),
            "npm_sha256": hashlib.sha256(npm_raw).hexdigest()}, "requirements_input": marker_input,
        "publisher_catalog_sha256": catalog_hash,
        "publisher_proofs_sha256": sorted(publisher_proofs),
        "dependencies": dependencies,
        "excluded_components": excluded, "file_components_excluded": file_count,
        "gaps": gaps, "selected_notice_coverage_complete": not gaps,
        "source_pointer_gaps": [record["purl"] for record in dependencies
                                if not record["source_pointers"] and not record.get("publisher_notices")],
        "legal_approved": False, "notices_complete": False, "container_os_coverage": "unresolved",
        "corresponding_source_obligations": "independent_review_required",
        "limitations": ["Only provided exact locked archives are inspected; other platform variants are not inferred.",
            "The platform is an explicit operator label, not an archive compatibility test.",
            "Only explicit false frozen-requirement markers exclude a dependency; unavailable wheels never do.",
            "Provided archives for explicitly excluded components are inspected but not included in selected notice coverage.",
            "Publisher evidence retains exact immutable source and binding claims; attestation signatures are not inferred.",
            "Publisher source URLs are provenance pointers, not verified corresponding-source offers.",
            "Image/OS notices and binary/source obligations require separate explicit evidence and review."]}
    return report, texts


def write_bundle(directory: Path, report: dict, texts: dict[str, bytes]) -> None:
    if directory.exists() or directory.is_symlink() or any(parent.is_symlink() for parent in directory.absolute().parents):
        raise ValueError("New nonsymlink notice output directory required")
    serialized = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    if len(serialized) > MAX_METADATA or len(texts) > MAX_ITEMS or sum(map(len, texts.values())) > MAX_OUTPUT:
        raise ValueError("Notice output budget exceeded")
    for name, raw in texts.items():
        checksum = hashlib.sha256(raw).hexdigest()
        if name not in {f"texts/{checksum}.txt", f"publisher-proofs/{checksum}"}:
            raise ValueError("Notice output identity disagrees")
    directory.mkdir(parents=True, mode=0o700)
    (directory / "texts").mkdir(mode=0o700)
    if any(name.startswith("publisher-proofs/") for name in texts):
        (directory / "publisher-proofs").mkdir(mode=0o700)
    for name, raw in sorted(texts.items()):
        checksum = hashlib.sha256(raw).hexdigest()
        if name not in {f"texts/{checksum}.txt", f"publisher-proofs/{checksum}"}:
            raise ValueError("Notice output identity disagrees")
        target = directory / name
        with target.open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
    target = directory / MANIFEST
    with target.open("xb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(serialized)


def check_coverage(directory: Path, *, sbom_dir: Path | None = None, uv_lock: Path | None = None,
                   npm_lock: Path | None = None, scopes: list[str] | None = None, platform: str | None = None,
                   requirements: Path | None = None,
                   publisher_catalog: Path | None = None,
                   require_source_pointers: bool = False) -> dict:
    if not sbom_dir or not uv_lock or not npm_lock or not scopes or not platform:
        raise ValueError("Independent selected SBOM and lock inputs required for coverage")
    deadline, text_bytes, evidence_items = time.monotonic() + MAX_SECONDS, 0, 0
    report = _json(_read(directory / MANIFEST))
    if (report.get("schema_version") != "release-notices-v2" or report.get("legal_approved") is not False
            or report.get("notices_complete") is not False or report.get("container_os_coverage") != "unresolved"
            or report.get("corresponding_source_obligations") != "independent_review_required"
            or scopes is not None and report.get("scopes") != sorted(scopes)
            or platform is not None and report.get("platform") != platform):
        raise ValueError("Notice evidence scope or qualification disagrees")
    records, inputs, _ = _scopes(sbom_dir, scopes)
    uv_raw, npm_raw = _read(uv_lock), _read(npm_lock)
    approved, first_party = _locks(uv_raw, npm_raw, records)
    markers, marker_input = _applicability(requirements, platform)
    excluded = _exclude(records, first_party, markers)
    catalog, catalog_hash = {}, None
    if publisher_catalog is not None:
        from fetch_publisher_notices import read_catalog
        catalog, catalog_hash = read_catalog(publisher_catalog, uv_lock, npm_lock)
    if (report.get("scope_inputs") != inputs or report.get("lock_inputs") != {
            "uv_sha256": hashlib.sha256(uv_raw).hexdigest(), "npm_sha256": hashlib.sha256(npm_raw).hexdigest()}
            or report.get("requirements_input") != marker_input or report.get("excluded_components") != excluded
            or report.get("publisher_catalog_sha256") != catalog_hash
            or not isinstance(report.get("dependencies"), list)
            or len(report["dependencies"]) != len(records)
            or {item.get("purl") for item in report["dependencies"]} != records.keys()):
        raise ValueError("Independent dependency scope or lock evidence changed")
    expected = {MANIFEST}
    proofs = {}
    if publisher_catalog is not None:
        from fetch_publisher_notices import proof_specs, validate_bundle_proofs
        if proof_specs(catalog):
            proofs = validate_bundle_proofs(directory, catalog)
        for checksum, raw in proofs.items():
            expected.add(f"publisher-proofs/{checksum}")
            text_bytes += len(raw)
        if text_bytes > MAX_OUTPUT:
            raise ValueError("Notice output byte budget exceeded")
    if report.get("publisher_proofs_sha256", []) != sorted(proofs):
        raise ValueError("Publisher binding proof inventory disagrees")
    for record in report["dependencies"]:
        _check_time(deadline)
        actual = records[record["purl"]]
        if any(record.get(key) != actual[key] for key in ("name", "version", "ecosystem", "scopes")):
            raise ValueError("Notice dependency identity changed")
        if (record.get("ecosystem") not in {"pypi", "npm"} or not record.get("archives")
                or not record.get("notices") and not record.get("publisher_notices")
                or record.get("missing_declared_license_files")
                or require_source_pointers and not record.get("source_pointers") and not record.get("publisher_notices")):
            raise ValueError("Selected notice dependency coverage is missing")
        archives = {item["sha256"] for item in record["archives"] if item.get("integrity_verified") is True}
        if len(archives) != len(record["archives"]):
            raise ValueError("Locked archive evidence is missing")
        identity = (record["ecosystem"], record["name"], record["version"])
        for evidence in record["archives"]:
            algorithm, _, digest = evidence["locked_digest"].partition(":")
            if (not re.fullmatch(r"[a-f0-9]{64}", evidence["sha256"])
                    or (identity, evidence["locked_url"], evidence.get("kind")) not in approved.get((algorithm, digest), [])):
                raise ValueError("Archive provenance is not in the exact selected lock")
        for pointer in record["source_pointers"]:
            if (pointer["archive_sha256"] not in archives or _url(pointer["url"]) != pointer["url"]
                    or pointer.get("corresponding_source_verified") is not False):
                raise ValueError("Source pointer provenance disagrees")
        for notice in record["notices"]:
            _check_time(deadline)
            evidence_items += 1
            if evidence_items > MAX_ITEMS:
                raise ValueError("Notice evidence item budget exceeded")
            if (not re.fullmatch(r"texts/[a-f0-9]{64}\.txt", notice["path"])
                    or notice["archive_sha256"] not in archives):
                raise ValueError("Notice artifact provenance disagrees")
            raw = _read(directory / notice["path"], MAX_TEXT)
            if notice["path"] not in expected:
                text_bytes += len(raw)
                if text_bytes > MAX_OUTPUT:
                    raise ValueError("Notice output byte budget exceeded")
            if (len(raw) != notice["bytes"] or hashlib.sha256(raw).hexdigest() != notice["sha256"]
                    or notice["path"] != f"texts/{notice['sha256']}.txt"):
                raise ValueError("Notice text digest changed")
            expected.add(notice["path"])
        for selection in catalog.get("archive_members", []):
            if (selection["ecosystem"], selection["name"], selection["version"]) != identity:
                continue
            archive_hashes = {item["sha256"] for item in record["archives"]
                              if (item["locked_url"], item["locked_digest"]) ==
                              (selection["locked_archive_url"], selection["locked_archive_digest"])}
            if not any(item["member"] == selection["member"] and item["sha256"] == selection["sha256"]
                       and item["archive_sha256"] in archive_hashes for item in record["notices"]):
                raise ValueError("Selected archive notice catalogue coverage disagrees")
        publisher_entries = [item for item in catalog.get("entries", [])
                             if (item["ecosystem"], item["name"], item["version"]) == identity]
        actual_entries = [{key: value for key, value in item.items() if key not in {"path", "bytes"}}
                          for item in record.get("publisher_notices", [])]
        if sorted(actual_entries, key=lambda item: json.dumps(item, sort_keys=True)) != sorted(
                publisher_entries, key=lambda item: json.dumps(item, sort_keys=True)):
            raise ValueError("Publisher notice catalogue coverage disagrees")
        for notice in record.get("publisher_notices", []):
            from fetch_publisher_notices import validate_bound_entry
            validate_bound_entry(notice, proofs)
            _check_time(deadline)
            evidence_items += 1
            if evidence_items > MAX_ITEMS:
                raise ValueError("Notice evidence item budget exceeded")
            if (not re.fullmatch(r"texts/[a-f0-9]{64}\.txt", notice["path"])
                    or not any(evidence["locked_url"] == notice["locked_archive_url"]
                               and evidence["locked_digest"] == notice["locked_archive_digest"]
                               for evidence in record["archives"])):
                raise ValueError("Publisher notice archive provenance disagrees")
            raw = _read(directory / notice["path"], MAX_TEXT)
            if notice["path"] not in expected:
                text_bytes += len(raw)
                if text_bytes > MAX_OUTPUT:
                    raise ValueError("Notice output byte budget exceeded")
            if (len(raw) != notice["bytes"] or hashlib.sha256(raw).hexdigest() != notice["sha256"]
                    or notice["path"] != f"texts/{notice['sha256']}.txt"):
                raise ValueError("Publisher notice text digest changed")
            expected.add(notice["path"])
    actual = set()
    for path in directory.rglob("*"):
        _check_time(deadline)
        if path.is_symlink():
            raise ValueError("Notice output symlink refused")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
            if len(actual) > MAX_ITEMS + 1:
                raise ValueError("Notice output inventory budget exceeded")
    if actual != expected or report.get("gaps") or not report.get("selected_notice_coverage_complete"):
        raise ValueError("Notice output inventory or selected coverage disagrees")
    return {"scope": "selected_locked_archive_notice_texts", "scopes": report["scopes"],
        "platform": report["platform"], "dependencies": len(report["dependencies"]),
        "text_files": sum(name.startswith("texts/") for name in expected),
        "publisher_proof_files": len(proofs), "legal_approved": False, "notices_complete": False,
        "container_os_coverage": "unresolved", "corresponding_source_verified": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sbom-dir", type=Path)
    parser.add_argument("--scope", action="append", dest="scopes")
    parser.add_argument("--uv-lock", type=Path)
    parser.add_argument("--npm-lock", type=Path)
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--wheel-dir", type=Path)
    parser.add_argument("--npm-dir", type=Path)
    parser.add_argument("--sdist-dir", type=Path)
    parser.add_argument("--publisher-catalog", type=Path)
    parser.add_argument("--publisher-dir", type=Path)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--require-source-pointers", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check:
            result = check_coverage(args.output_dir, sbom_dir=args.sbom_dir, uv_lock=args.uv_lock,
                                    npm_lock=args.npm_lock, scopes=args.scopes, platform=args.platform,
                                    requirements=args.requirements,
                                    publisher_catalog=args.publisher_catalog,
                                    require_source_pointers=args.require_source_pointers)
        else:
            if not args.sbom_dir or not args.scopes or not args.uv_lock or not args.npm_lock:
                raise ValueError("Explicit SBOM scopes and both lock files required")
            report, texts = collect(sbom_dir=args.sbom_dir, scopes=args.scopes, uv_lock=args.uv_lock,
                npm_lock=args.npm_lock, wheel_dir=args.wheel_dir, npm_dir=args.npm_dir, platform=args.platform,
                requirements=args.requirements, sdist_dir=args.sdist_dir,
                publisher_catalog=args.publisher_catalog, publisher_dir=args.publisher_dir)
            write_bundle(args.output_dir, report, texts)
            result = {"dependencies": len(report["dependencies"]),
                "text_files": sum(name.startswith("texts/") for name in texts),
                "publisher_proof_files": len(report["publisher_proofs_sha256"]),
                "missing_notice_dependencies": len(report["gaps"]), "legal_approved": False,
                "notices_complete": False, "container_os_coverage": "unresolved"}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, SyntaxError,
            tarfile.TarError, zipfile.BadZipFile, zlib.error):
        print("Release notice evidence collection or verification failed; inspect selected local archives and scope")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
