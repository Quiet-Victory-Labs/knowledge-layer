"""Inventory release files and verify checksums without implying publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from container_scan_policy import DECISIONS, EVALUATION, SCANS
from container_scan_policy import check as check_container_risk
from container_scan_policy import evaluate as evaluate_container_risk
from release_gate import (
    CHANNELS,
    REQUIRED_JOBS,
    channel_metadata,
    validate_channel_metadata,
    validate_ga_review,
    verify_ga_review,
)

EXCLUDED = {"release-manifest.json", "SHA256SUMS"}
CONTAINER_SCANS = set(SCANS.values())


def check_container_scans(directory: Path) -> None:
    """Keep raw scans and require a matching, passed risk evaluation."""
    check_container_risk(directory)


def check_postgres_identity(directory: Path) -> None:
    result = json.loads((directory / "postgres-build.json").read_text())
    if (not isinstance(result, dict) or not isinstance(result.get("containerimage.digest"), str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", result["containerimage.digest"])):
        raise ValueError("PostgreSQL build identity is invalid")


def check_controls(directory: Path, *, version: str, repository: str | None) -> None:
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected release repository is required")
    result = json.loads((directory / "release-controls.json").read_text())
    if (not isinstance(result, dict) or result.get("schema_version") != "release-controls-v1"
            or result.get("result") != "passed" or result.get("tag") != "v" + version
            or result.get("repository") != repository
            or result.get("checks") != [{"name": name, "result": "passed"} for name in
                ("main_protection", "oss-release-candidate", "oss-release")]
            or result.get("settings_changed") is not False
            or result.get("human_approval_inferred") is not False):
        raise ValueError("Actual release protection receipt is missing or incomplete")


def check_notices(directory: Path) -> None:
    from collect_release_notices import check_coverage
    check_coverage(directory / "notices", sbom_dir=directory / "sbom",
                   uv_lock=Path("uv.lock"), npm_lock=Path("web/package-lock.json"),
                   scopes=["python-all-runtime-extras.cdx.json", "web.cdx.json"],
                   platform="linux/amd64", requirements=directory / "notice-requirements.txt",
                   publisher_catalog=Path("docs/publisher-notices.json"))


def inventory(directory: Path) -> list[dict]:
    result = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Release artifacts cannot be symlinks")
        if not path.is_file() or path.name in EXCLUDED:
            continue
        relative = path.relative_to(directory).as_posix()
        if not re.fullmatch(r"[A-Za-z0-9_./+-]+", relative):
            raise ValueError("Unsafe artifact name")
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        result.append({"path": relative, "bytes": path.stat().st_size, "sha256": digest})
    return result


def check_gate(directory: Path, *, version: str, commit: str, channel: str) -> dict:
    gate = json.loads((directory / "release-gate.json").read_text())
    if (not isinstance(gate, dict) or gate.get("schema_version") != "release-gate-v1"
            or gate.get("scope") != "signed_tag_and_exact_commit_ci"
            or gate.get("version") != version or gate.get("commit") != commit
            or gate.get("channel") != channel or gate.get("tag") != "v" + version
            or "ga_review" not in gate
            or gate.get("signed_annotated_tag") != "verified" or gate.get("exact_commit_ci") != "verified"
            or gate.get("required_jobs") != sorted(REQUIRED_JOBS)):
        raise ValueError("Release gate identity or channel disagrees")
    validate_channel_metadata(version, gate.get("development_statuses"), channel)
    if channel == "stable":
        validate_ga_review(gate.get("ga_review"), commit)
    elif gate.get("ga_review") is not None:
        raise ValueError("Prerelease artifacts cannot contain a GA approval")
    return gate


def create(directory: Path, *, allow_incomplete: bool = False, channel: str = "prerelease",
           repository: str | None = None) -> dict:
    repository = repository or os.environ.get("GITHUB_REPOSITORY")
    metadata = channel_metadata(channel)
    artifacts = inventory(directory)
    names = {item["path"] for item in artifacts}
    required = {
        "wheel": any(name.endswith(".whl") for name in names),
        "sdist": any(name.startswith("knowledge_layer-") and name.endswith(".tar.gz") for name in names),
        "web": "web-static.tar.gz" in names,
        "chart": any(name.startswith("knowledge-layer-") and name.endswith(".tgz") for name in names),
        "container": "container.oci.tar" in names,
        "container_identity": {"container-build.json", "container-reference.txt"} <= names,
        "postgres_identity": "postgres-build.json" in names,
        "sbom": {"sbom/python.cdx.json", "sbom/python-all-runtime-extras.cdx.json",
                 "sbom/web.cdx.json", "sbom/container.cdx.json", "sbom/postgres.cdx.json"} <= names,
        "license_inventory": "dependency-licenses.json" in names,
        "locked_license_metadata": "locked-license-metadata.json" in names,
        "sbom_bundle": "sbom-bundle.tar.gz" in names,
        "history_scan": "history-scan.json" in names,
        "gate": "release-gate.json" in names,
        "release_controls": "release-controls.json" in names,
        "notice_texts": {"third-party-notices.tar.gz", "notice-requirements.txt",
                         "notices/third-party-notices.json"} <= names,
        "distribution_smoke": "verification.json" in names,
        "vulnerability_scans": {"python-vulnerabilities.json", "web-vulnerabilities.json",
                                *CONTAINER_SCANS} <= names,
        "container_risk": {DECISIONS, EVALUATION} <= names,
    }
    if not artifacts or not allow_incomplete and not all(required.values()):
        raise ValueError("Required release artifacts are missing")
    if CONTAINER_SCANS <= names:
        check_container_scans(directory)
    if required["postgres_identity"]:
        check_postgres_identity(directory)
    if required["release_controls"]:
        check_controls(directory, version=metadata["version"], repository=repository)
    if required["notice_texts"]:
        check_notices(directory)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
    if dirty and not allow_incomplete:
        raise ValueError("Release requires a clean checkout")
    gate = check_gate(directory, version=metadata["version"], commit=commit, channel=channel) if required["gate"] else None
    if channel == "stable" and gate is None:
        raise ValueError("Stable release requires its exact-commit GA gate")
    if channel == "stable" and gate["ga_review"] != verify_ga_review(commit, gate["ga_review"]["review_id"]):
        raise ValueError("Protected GA review changed after preparation")
    if gate is not None and gate["development_statuses"] != metadata["development_statuses"]:
        raise ValueError("Release development metadata changed after preparation")
    return {"schema_version": "release-manifest-v1", **metadata, "commit": commit, "repository": repository,
            "ga_review": gate["ga_review"] if gate else None,
            "dirty_checkout": dirty, "complete": all(required.values()) and not dirty,
            "required_artifacts": required, "artifacts": artifacts,
            "published": False, "legal_approval": "not_inferred", "enterprise_qualified": False}


def verify(directory: Path, *, channel: str | None = None, ga_review_id: str | None = None,
           repository: str | None = None) -> dict:
    repository = repository or os.environ.get("GITHUB_REPOSITORY")
    manifest = json.loads((directory / "release-manifest.json").read_text())
    if (manifest.get("schema_version") != "release-manifest-v1"
            or manifest.get("artifacts") != inventory(directory)):
        raise ValueError("Release file inventory or digest changed")
    expected = "".join(f"{item['sha256']}  {item['path']}\n" for item in manifest["artifacts"])
    if (directory / "SHA256SUMS").read_text() != expected:
        raise ValueError("Checksum inventory disagrees")
    validate_channel_metadata(manifest.get("version"), manifest.get("development_statuses"), manifest.get("channel"))
    if channel is not None and channel != manifest["channel"]:
        raise ValueError("Prepared release channel cannot be changed")
    if manifest.get("complete"):
        check_container_scans(directory)
        check_postgres_identity(directory)
        if manifest.get("repository") != repository:
            raise ValueError("Expected release repository disagrees with manifest")
        check_controls(directory, version=manifest["version"], repository=repository)
        check_notices(directory)
        gate = check_gate(directory, version=manifest["version"], commit=manifest["commit"], channel=manifest["channel"])
        if (manifest.get("ga_review") != gate.get("ga_review")
                or manifest["development_statuses"] != gate["development_statuses"]):
            raise ValueError("Prepared release review or metadata changed")
    elif manifest.get("channel") == "stable":
        raise ValueError("Stable release requires a complete preparation")
    if (ga_review_id is not None and manifest["channel"] == "stable"
            and manifest.get("ga_review") != verify_ga_review(manifest["commit"], ga_review_id)):
        raise ValueError("Protected GA review does not match prepared artifacts")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--channel", choices=CHANNELS)
    parser.add_argument("--ga-review-id", help="Recheck the protected exact-commit GA review during publication")
    parser.add_argument("--repository", help="Expected release repository; defaults to GITHUB_REPOSITORY in CI")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="Generate a local unqualified receipt, never a publishable release")
    parser.add_argument("--check-container-scans", action="store_true",
                        help="Record raw matches; require review of serious findings and refuse suppressions")
    args = parser.parse_args()
    try:
        if args.check_container_scans:
            if args.verify or args.allow_incomplete or args.channel or args.ga_review_id is not None:
                raise ValueError("Ambiguous release check")
            report = evaluate_container_risk(args.directory)
            (args.directory / EVALUATION).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
            print(json.dumps({key: report[key] for key in
                              ("result", "raw_matches", "blocking_matches", "reviewed_matches", "tracked_matches")}))
            check_container_scans(args.directory)
            return 0
        if args.ga_review_id is not None and not args.verify:
            raise ValueError("GA publication review requires verification")
        manifest = verify(args.directory, channel=args.channel, ga_review_id=args.ga_review_id,
                          repository=args.repository) if args.verify else create(
            args.directory, allow_incomplete=args.allow_incomplete, channel=args.channel or "prerelease",
            repository=args.repository)
        if not args.verify:
            (args.directory / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            (args.directory / "SHA256SUMS").write_text("".join(
                f"{item['sha256']}  {item['path']}\n" for item in manifest["artifacts"]))
        print(json.dumps({"result": "verified" if args.verify else "generated",
                          "files": len(manifest["artifacts"]), "complete": manifest["complete"],
                          "channel": manifest["channel"],
                          "published": False}))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print("Release manifest failed; artifact identity is not verified")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
