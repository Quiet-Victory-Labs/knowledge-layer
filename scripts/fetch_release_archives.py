"""Fetch exact locked notice inputs, without installing or executing package code."""

from __future__ import annotations

import argparse
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
from pathlib import Path
from urllib.parse import urlparse

from dependency_licenses import canonical
from fetch_license_metadata import NoRedirect

MAX_ARCHIVE = 128 * 1024 * 1024
MAX_TOTAL = 768 * 1024 * 1024
MAX_PACKAGES = 512
MAX_SECONDS = 300
MAX_REQUIREMENTS = 512 * 1024
MAX_INPUT = 16 * 1024 * 1024
MAX_INPUT_TOTAL = 32 * 1024 * 1024
MAX_SCOPES = 16


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in {
            "files.pythonhosted.org", "registry.npmjs.org"} or parsed.port
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("archive_origin_refused")


def download(url: str, output: Path, budget: int) -> None:
    validate_url(url)
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
        if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise ValueError("archive_encoding_refused")
        with output.open("xb") as stream:
            os.chmod(output, 0o600)
            size = 0
            while chunk := response.read(65536):
                size += len(chunk)
                if size > budget:
                    raise ValueError("archive_size_limit")
                stream.write(chunk)


def target_tags(target: str):
    from packaging.tags import compatible_tags, cpython_tags
    if target not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("unsupported_notice_platform")
    machine = "x86_64" if target == "linux/amd64" else "aarch64"
    # The shipped Debian Trixie runtime is glibc 2.41, ordinary CPython 3.14.
    platforms = [f"manylinux_2_{minor}_{machine}" for minor in range(41, 4, -1)]
    platforms += [f"manylinux2014_{machine}", f"linux_{machine}"]
    if machine == "x86_64":
        platforms += ["manylinux2010_x86_64", "manylinux1_x86_64"]
    tags = list(cpython_tags((3, 14), abis=["cp314"], platforms=platforms))
    tags += list(compatible_tags((3, 14), interpreter="cp314", platforms=platforms))
    return {tag: index for index, tag in enumerate(tags)}


def target_marker_environment(target: str) -> dict[str, str]:
    target_tags(target)
    return {"implementation_name": "cpython", "implementation_version": "3.14.8",
        "os_name": "posix", "platform_machine": "x86_64" if target == "linux/amd64" else "aarch64",
        "platform_release": "", "platform_system": "Linux", "platform_version": "",
        "python_full_version": "3.14.8", "platform_python_implementation": "CPython",
        "python_version": "3.14", "sys_platform": "linux", "extra": ""}


def read_input(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("archive_input_not_regular")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("archive_input_size_limit")
    return raw


def snapshot_inputs(sbom: Path, scopes: list[str], lock: Path, npm_lock: Path,
                    requirements: Path | None) -> dict[Path, bytes]:
    if len(scopes) > MAX_SCOPES or any(not re.fullmatch(r"[A-Za-z0-9_-]+\.cdx\.json", scope)
                                     for scope in scopes):
        raise ValueError("invalid_notice_scope")
    paths = [lock, npm_lock, *(sbom / scope for scope in scopes)]
    if requirements is not None:
        paths.append(requirements)
    result, total = {}, 0
    for path in dict.fromkeys(paths):
        remaining = MAX_INPUT_TOTAL - total
        if remaining <= 0:
            raise ValueError("archive_input_size_limit")
        raw = read_input(path, min(MAX_REQUIREMENTS if path == requirements else MAX_INPUT, remaining))
        result[path] = raw
        total += len(raw)
    return result


def markers_from_bytes(raw: bytes, target: str) -> dict[tuple[str, str], bool]:
    from packaging.requirements import Requirement
    environment = target_marker_environment(target)
    result = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        if requirement.url or len(specifiers) != 1 or specifiers[0].operator != "==":
            raise ValueError("requirements_not_exact_locked_versions")
        key = (canonical(requirement.name), specifiers[0].version)
        applicable = requirement.marker is None or requirement.marker.evaluate(environment)
        result[key] = result.get(key, False) or applicable
    return result


def requirement_markers(path: Path | None, target: str) -> dict[tuple[str, str], bool]:
    return {} if path is None else markers_from_bytes(read_input(path, MAX_REQUIREMENTS), target)


def selection(sbom: Path, scopes: list[str], lock: Path, npm_lock: Path, target: str,
              requirements: Path | None = None, *, inputs: dict[Path, bytes] | None = None,
              markers: dict[tuple[str, str], bool] | None = None,
              source_packages: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    from packaging.utils import parse_wheel_filename
    tags = target_tags(target)
    if inputs is None:
        inputs = snapshot_inputs(sbom, scopes, lock, npm_lock, requirements)
    if markers is None:
        markers = {} if requirements is None else markers_from_bytes(inputs[requirements], target)
    python = {(canonical(item["name"]), item["version"]): item
              for item in tomllib.loads(inputs[lock].decode("utf-8"))["package"]}
    npm = json.loads(inputs[npm_lock])["packages"]
    sources = source_packages or []
    if (len(sources) != len(set(sources)) or len(sources) > MAX_PACKAGES
            or any(not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) for name in sources)):
        raise ValueError("source_package_selection_invalid")
    entries, missing, seen, selected_sources = [], [], set(), set()
    for scope in scopes:
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.cdx\.json", scope):
            raise ValueError("invalid_notice_scope")
        data = json.loads(inputs[sbom / scope])
        if data.get("bomFormat") != "CycloneDX":
            raise ValueError("invalid_sbom")
        for item in data.get("components", []):
            purl = item.get("purl", "")
            if purl in seen:
                continue
            seen.add(purl)
            if purl.startswith("pkg:pypi/"):
                if markers.get((canonical(item["name"]), item.get("version"))) is False:
                    continue
                package = python.get((canonical(item["name"]), item.get("version")), {})
                choices = []
                for wheel in package.get("wheels", []):
                    name = Path(urlparse(wheel["url"]).path).name
                    parsed_name, version, _, wheel_tags = parse_wheel_filename(name)
                    rank = min((tags[tag] for tag in wheel_tags if tag in tags), default=None)
                    if (rank is not None and canonical(parsed_name) == canonical(item["name"])
                            and str(version) == item["version"]):
                        choices.append((rank, name, wheel))
                if choices:
                    _, name, wheel = min(choices, key=lambda value: (value[0], value[1]))
                    entries.append({"purl": purl, "url": wheel["url"], "hash": wheel["hash"],
                                    "directory": "wheels", "filename": name})
                else:
                    missing.append({"purl": purl, "reason": "compatible_locked_wheel_missing"})
                if canonical(item["name"]) in sources:
                    selected_sources.add(canonical(item["name"]))
                    source = package.get("sdist", {})
                    url, digest = source.get("url", ""), source.get("hash", "")
                    name = Path(urlparse(url).path).name
                    if (not name.endswith(".tar.gz") or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)):
                        missing.append({"purl": purl, "reason": "locked_source_archive_missing"})
                    else:
                        entries.append({"purl": purl, "url": url, "hash": digest,
                                        "directory": "sdists", "filename": name})
            elif purl.startswith("pkg:npm/"):
                name = (item.get("group", "") + "/" if item.get("group") else "") + item["name"]
                candidates = [value for path, value in npm.items() if path and
                              path.split("node_modules/")[-1] == name and
                              value.get("version") == item.get("version")]
                identities = {(value.get("resolved"), value.get("integrity")) for value in candidates}
                if len(identities) == 1:
                    url, integrity = next(iter(identities))
                    if not isinstance(url, str) or not isinstance(integrity, str):
                        raise ValueError("npm_lock_identity_invalid")
                    entries.append({"purl": purl, "url": url, "hash": integrity,
                                    "directory": "npm", "filename": hashlib.sha256(url.encode()).hexdigest() + ".tgz"})
                else:
                    missing.append({"purl": purl, "reason": "unique_locked_npm_archive_missing"})
    if set(sources) != selected_sources:
        raise ValueError("source_package_not_in_selected_scope")
    if len(entries) > MAX_PACKAGES:
        raise ValueError("archive_package_limit")
    return sorted(entries, key=lambda item: item["purl"]), missing


def matches(path: Path, expected: str) -> bool:
    if expected.startswith("sha256:") and re.fullmatch(r"sha256:[a-f0-9]{64}", expected):
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest() == expected[7:]
    parts = expected.split("-", 1)
    if len(parts) != 2 or parts[0] not in {"sha256", "sha384", "sha512"}:
        raise ValueError("archive_digest_invalid")
    digest = base64.b64decode(parts[1], validate=True)
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, parts[0]).digest() == digest


def fetch(entries: list[dict], output: Path, *, runner=subprocess.run) -> dict:
    destinations = [(item["directory"], item["filename"]) for item in entries]
    if (len(entries) > MAX_PACKAGES or len(set(destinations)) != len(destinations)
            or any(directory not in {"wheels", "npm", "sdists"}
                   or not re.fullmatch(r"[A-Za-z0-9_.-]{1,240}", filename)
                   or filename in {".", ".."} for directory, filename in destinations)):
        raise ValueError("archive_destination_refused")
    output.mkdir(mode=0o700)
    for name in ("wheels", "npm", "sdists"):
        (output / name).mkdir(mode=0o700)
    deadline, total, saved, missing = time.monotonic() + MAX_SECONDS, 0, [], []
    for item in entries:
        path = output / item["directory"] / item["filename"]
        counted = False
        try:
            validate_url(item["url"])
            remaining = deadline - time.monotonic()
            if remaining <= 0 or total >= MAX_TOTAL:
                raise ValueError("archive_fetch_budget")
            runner([sys.executable, str(Path(__file__).resolve()), "--fetch-url", item["url"],
                    "--destination", str(path), "--byte-budget", str(min(MAX_ARCHIVE, MAX_TOTAL - total))],
                   timeout=min(20, remaining), capture_output=True, check=True)
            total += path.stat().st_size
            counted = True
            if not matches(path, item["hash"]):
                raise ValueError("archive_digest_mismatch")
            saved.append({"purl": item["purl"], "file": path.relative_to(output).as_posix(),
                          "bytes": path.stat().st_size, "locked_digest": item["hash"]})
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            # Only this invocation's exclusive child destination is removed.
            if path.is_file() and not path.is_symlink():
                if not counted:
                    total += path.stat().st_size
                path.unlink()
            missing.append({"purl": item["purl"], "reason": "locked_archive_unavailable_or_refused"})
    return {"schema_version": "locked-notice-archives-v1", "archives": saved, "missing": missing,
            "downloaded_bytes": total, "packages_installed": 0, "package_code_executed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sbom-dir", type=Path, required=True)
    parser.add_argument("--scope", action="append", required=True)
    parser.add_argument("--uv-lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--npm-lock", type=Path, default=Path("web/package-lock.json"))
    parser.add_argument("--platform", choices=["linux/amd64", "linux/arm64"], required=True)
    parser.add_argument("--requirements", type=Path, help="Frozen marker-bearing requirements for explicit platform exclusions")
    parser.add_argument("--source-package", action="append", help="Also fetch this selected package's exact locked sdist for notice evidence")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        inputs = snapshot_inputs(args.sbom_dir, args.scope, args.uv_lock, args.npm_lock, args.requirements)
        markers = {} if args.requirements is None else markers_from_bytes(inputs[args.requirements], args.platform)
        hashes = {path: hashlib.sha256(raw).hexdigest() for path, raw in inputs.items()}
        entries, missing = selection(args.sbom_dir, args.scope, args.uv_lock, args.npm_lock, args.platform,
                                     args.requirements, inputs=inputs, markers=markers, source_packages=args.source_package)
        result = fetch(entries, args.output_dir)
        result["missing"] += missing
        result.update(platform=args.platform, scopes=sorted(args.scope))
        result["platform_excluded_requirements"] = [{"name": name, "version": version} for (name, version),
            applicable in sorted(markers.items()) if not applicable]
        result["requirements_sha256"] = hashes.get(args.requirements)
        result["selection_inputs_sha256"] = {"uv_lock": hashes[args.uv_lock], "npm_lock": hashes[args.npm_lock],
            "sbom_scopes": {scope: hashes[args.sbom_dir / scope] for scope in sorted(args.scope)}}
        for path, raw in inputs.items():
            if read_input(path, MAX_REQUIREMENTS if path == args.requirements else MAX_INPUT) != raw:
                raise ValueError("archive_inputs_changed")
        (args.output_dir / "archives.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"archives": len(result["archives"]), "missing": len(result["missing"]),
                          "downloaded_bytes": result["downloaded_bytes"], "package_code_executed": False}))
        return 1 if result["missing"] else 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print("Locked notice archive fetch refused; no package code executed")
        return 1


if __name__ == "__main__":
    if len(sys.argv) == 7 and sys.argv[1] == "--fetch-url" and sys.argv[3] == "--destination" and sys.argv[5] == "--byte-budget":
        try:
            budget = int(sys.argv[6])
            if not 0 < budget <= MAX_ARCHIVE:
                raise ValueError("archive_size_limit")
            download(sys.argv[2], Path(sys.argv[4]), budget)
        except (OSError, ValueError):
            raise SystemExit(1) from None
        raise SystemExit(0)
    raise SystemExit(main())
