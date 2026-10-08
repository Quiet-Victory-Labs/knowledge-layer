"""Fetch hash-verified core metadata for locked wheels without installing them."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import quote, urlparse

from dependency_licenses import canonical, collect, has_license_metadata

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_FETCH_SECONDS = 60
MAX_PACKAGES = 128


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ValueError("Metadata redirects are refused")


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"pypi.org", "files.pythonhosted.org"}
            or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment):
        raise ValueError("Unsupported metadata origin")


def download(url: str) -> bytes:
    """The parent process enforces the wall-clock budget even for dripping bodies."""
    validate_url(url)
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
        if response.headers.get("Content-Encoding", "identity").strip().lower() not in {"", "identity"}:
            raise ValueError("Compressed metadata is refused")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("Metadata response exceeds budget")
    return payload


def read_url(url: str, deadline: float) -> bytes:
    validate_url(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Metadata fetch deadline exceeded")
    # Socket inactivity timeouts do not bound a continuously dripping body.
    # A short-lived stdlib-only child is killed and reaped at the total deadline;
    # no abandoned network threads survive a failed collection.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--read-url", url],
                                capture_output=True, timeout=min(10, remaining), check=False)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Metadata fetch deadline exceeded") from exc
    if result.returncode:
        raise ValueError("Metadata response is unavailable or refused")
    if len(result.stdout) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
        raise ValueError("Metadata response exceeds budget")
    return result.stdout


def fetch_one(record: dict, artifacts: dict[str, str], deadline: float, *, reader=read_url) -> dict:
    name, version = canonical(record["name"]), record["version"]
    if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", name)
            or not isinstance(version, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}", version)):
        raise ValueError("Invalid package identity")
    index_url = f"https://pypi.org/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json"
    index_bytes = reader(index_url, deadline)
    release = json.loads(index_bytes)
    if (canonical(release["info"]["name"]), release["info"]["version"]) != (name, version):
        raise ValueError("Release metadata identity changed")
    for artifact in sorted(release["urls"], key=lambda item: item["filename"]):
        url = artifact["url"]
        core = artifact.get("core-metadata")
        expected = core.get("sha256", "") if isinstance(core, dict) else ""
        wheel_hash = artifact["digests"]["sha256"]
        if (artifact.get("packagetype") != "bdist_wheel" or artifacts.get(url) != wheel_hash
                or not re.fullmatch(r"[a-f0-9]{64}", expected)):
            continue
        if urlparse(url).hostname != "files.pythonhosted.org" or not urlparse(url).path.endswith(".whl"):
            raise ValueError("Unexpected locked wheel origin")
        metadata_bytes = reader(url + ".metadata", deadline)
        if hashlib.sha256(metadata_bytes).hexdigest() != expected:
            raise ValueError("Core metadata digest changed")
        metadata = BytesParser().parsebytes(metadata_bytes, headersonly=True)
        if (canonical(metadata.get("Name", "")), metadata.get("Version")) != (name, version):
            raise ValueError("Wheel metadata identity changed")
        return {
            "name": record["name"], "version": version, "purl": record["purl"],
            "license_expression": metadata.get("License-Expression"),
            "legacy_license": metadata.get("License"),
            "license_classifiers": [value for value in metadata.get_all("Classifier", [])
                                    if value.startswith("License ::")],
            "license_files": metadata.get_all("License-File", []),
            "metadata_url": url + ".metadata", "core_metadata_sha256": expected,
            "locked_wheel_sha256": wheel_hash, "index_sha256": hashlib.sha256(index_bytes).hexdigest(),
            "filename": artifact["filename"], "source": "hash_verified_locked_wheel_core_metadata",
            "platform_coverage": "one_locked_wheel_not_all_variants",
        }
    raise ValueError("No hashed metadata for a locked wheel")


def fetch(directory: Path, lock: Path) -> dict:
    packages = tomllib.loads(lock.read_text())["package"]
    artifacts = {(canonical(package["name"]), package["version"]): {
        wheel["url"]: wheel["hash"].removeprefix("sha256:")
        for wheel in package.get("wheels", []) if wheel.get("hash", "").startswith("sha256:")
    } for package in packages}
    candidates = [record for record in collect(directory)["components"]
                  if record["purl"].startswith("pkg:pypi/") and not has_license_metadata(record)]
    deadline = time.monotonic() + MAX_FETCH_SECONDS
    entries, missing = [], []
    for index, record in enumerate(candidates):
        try:
            if index >= MAX_PACKAGES or time.monotonic() >= deadline:
                raise ValueError("Package budget exhausted")
            entries.append(fetch_one(record, artifacts.get(
                (canonical(record["name"]), record["version"]), {}), deadline))
        except (OSError, ValueError, TypeError, KeyError, urllib.error.URLError):
            missing.append({"purl": record["purl"], "reason": "locked_artifact_metadata_unavailable_or_refused"})
    undeclared = [entry["purl"] for entry in entries if not any(
        entry.get(key) for key in ("license_expression", "legacy_license", "license_classifiers"))]
    return {"schema_version": "locked-license-metadata-v1", "entries": entries, "missing": missing,
            "metadata_without_license_declaration": undeclared,
            "fetched_at": datetime.now(UTC).isoformat(), "legal_approved": False,
            "notices_complete": False, "limitations": [
                "Metadata is verified against the index digest of a wheel whose archive digest is locked.",
                "No package code is installed or executed; required license texts are not collected.",
                "One wheel per package does not qualify every platform variant or license obligations.",
                "Unavailable evidence remains a gap and never grants legal approval."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sbom-dir", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=Path(__file__).resolve().parents[1] / "uv.lock")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = fetch(args.sbom_dir, args.lock)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"verified_metadata": len(result["entries"]),
                          "unavailable_package_metadata": len(result["missing"]),
                          "metadata_without_license_declaration": len(result["metadata_without_license_declaration"]),
                          "legal_approved": False, "notices_complete": False}))
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print("Locked license metadata collection failed")
        return 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--read-url":
        try:
            sys.stdout.buffer.write(download(sys.argv[2]))
        except (OSError, ValueError, urllib.error.URLError):
            raise SystemExit(1) from None
        raise SystemExit(0)
    raise SystemExit(main())
