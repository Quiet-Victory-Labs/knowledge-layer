"""Fail closed on release version, signed tag, and exact-commit GitHub CI gates."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_JOBS = {
    "python", "package", "local-integration", "oidc-integration", "autonomy-integration",
    "self-host-integration",
}
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
CHANNELS = ("prerelease", "stable")
STABLE_CLASSIFIER = "Development Status :: 5 - Production/Stable"


def command(args: list[str], *, cwd: Path = ROOT, env: dict | None = None) -> str:
    return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True,
                          text=True, timeout=60).stdout.strip()


def versions(root: Path = ROOT) -> str:
    python = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    web = json.loads((root / "web/package.json").read_text())["version"]
    lock = json.loads((root / "web/package-lock.json").read_text())
    chart = (root / "charts/knowledge-layer/Chart.yaml").read_text()
    values = [python, web, lock["version"], lock["packages"][""]["version"]]
    for field in ("version", "appVersion"):
        found = re.findall(rf"^{field}:\s*[\"']?([^\s\"']+)[\"']?\s*$", chart, re.MULTILINE)
        if len(found) != 1:
            raise ValueError("Invalid chart version")
        values.append(found[0])
    if not re.fullmatch(VERSION, python) or set(values) != {python}:
        raise ValueError("Release versions disagree")
    return python


def validate_channel_metadata(version: str, classifiers: list[str], channel: str) -> dict:
    if (channel not in CHANNELS or not isinstance(version, str)
            or not re.fullmatch(VERSION, version) or not isinstance(classifiers, list)
            or any(not isinstance(value, str) for value in classifiers)):
        raise ValueError("Invalid release channel metadata")
    statuses = sorted(value for value in classifiers if value.startswith("Development Status ::"))
    if channel == "stable" and (int(version.split(".")[0]) < 1 or statuses != [STABLE_CLASSIFIER]):
        raise ValueError("Stable release requires version >=1.0.0 and Production/Stable metadata")
    return {"channel": channel, "version": version, "development_statuses": statuses}


def channel_metadata(channel: str, root: Path = ROOT) -> dict:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    return validate_channel_metadata(versions(root), project.get("classifiers", []), channel)


def validate_ga_review(review: dict, commit: str) -> dict:
    if (not isinstance(review, dict) or set(review) != {"review_id", "commit", "acknowledged"}
            or not isinstance(review.get("review_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", review["review_id"])
            or not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit)
            or review.get("commit") != commit or review.get("acknowledged") is not True):
        raise ValueError("Independent GA review is missing or mismatched")
    return review


def verify_ga_review(commit: str, review_id: str | None) -> dict:
    review = validate_ga_review({"review_id": review_id, "commit": commit, "acknowledged": True}, commit)
    if (os.environ.get("RELEASE_GA_ENVIRONMENT_READY") != "true"
            or review_id != os.environ.get("RELEASE_GA_REVIEW_ID")
            or commit != os.environ.get("RELEASE_GA_REVIEW_COMMIT")):
        raise ValueError("Protected environment has not acknowledged this exact GA review")
    return review


def verify_tag(tag: str, expected_commit: str, public_key: str, fingerprint: str) -> dict:
    if (not re.fullmatch("v" + VERSION, tag)
            or not re.fullmatch(r"[a-f0-9]{40}", expected_commit)
            or not re.fullmatch(r"(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})", fingerprint)):
        raise ValueError("Invalid release identity")
    ref = "refs/tags/" + tag
    if command(["git", "cat-file", "-t", ref]) != "tag":
        raise ValueError("Release tag must be annotated")
    commit = command(["git", "rev-parse", ref + "^{commit}"])
    if commit != expected_commit or command(["git", "rev-parse", "HEAD"]) != commit:
        raise ValueError("Tag and checkout disagree")
    with tempfile.TemporaryDirectory(prefix="knowledge-release-gpg-") as directory:
        environment = os.environ | {"GNUPGHOME": directory}
        key = Path(directory) / "maintainer.asc"
        key.write_text(public_key)
        key.chmod(0o600)
        command(["gpg", "--batch", "--import", str(key)], env=environment)
        result = subprocess.run(["git", "-c", "gpg.program=gpg", "verify-tag", "--raw", ref],
                                cwd=ROOT, env=environment, capture_output=True, text=True,
                                check=True, timeout=60)
        valid = re.findall(r"^\[GNUPG:\] VALIDSIG (.+)$", result.stderr, re.MULTILINE)
        # VALIDSIG ends with the primary fingerprint when a signing subkey is used.
        allowed = fingerprint.upper()
        invalid = re.search(r"^\[GNUPG:\] (?:EXPSIG|EXPKEYSIG|REVKEYSIG|BADSIG|ERRSIG)\b",
                            result.stderr, re.MULTILINE)
        if invalid or len(valid) != 1 or allowed not in {valid[0].split()[0], valid[0].split()[-1]}:
            raise ValueError("Unapproved tag signing key")
    return {"tag": tag, "commit": commit, "signing_fingerprint": allowed,
            "signed_annotated_tag": "verified"}


def verify_ci(repository: str, commit: str, *, api=command) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository identity")
    prefix = f"repos/{repository}/actions"
    response = json.loads(api(["gh", "api", f"{prefix}/workflows/verify.yaml/runs?head_sha={commit}&per_page=100"]))
    runs = [run for run in response["workflow_runs"] if run["head_sha"] == commit
            and run.get("event") in {"push", "workflow_dispatch"}
            and run.get("path") == ".github/workflows/verify.yaml"]
    if not runs:
        raise ValueError("No exact-commit Verify workflow")
    run = max(runs, key=lambda item: (item["run_number"], item.get("run_attempt", 1)))
    if run["status"] != "completed" or run["conclusion"] != "success":
        raise ValueError("Latest exact-commit Verify workflow has not succeeded")
    jobs = json.loads(api(["gh", "api", f"{prefix}/runs/{run['id']}/jobs?filter=latest&per_page=100"]))
    indexed = {}
    for job in jobs["jobs"]:
        indexed.setdefault(job["name"], []).append(job)
    if any(len(indexed.get(name, [])) != 1
           or indexed[name][0]["conclusion"] != "success"
           or indexed[name][0]["status"] != "completed" for name in REQUIRED_JOBS):
        raise ValueError("Required exact-commit CI jobs missing or unsuccessful")
    return {"workflow_run_id": run["id"], "workflow_run_attempt": run.get("run_attempt", 1),
            "required_jobs": sorted(REQUIRED_JOBS), "exact_commit_ci": "verified"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--versions-only", action="store_true")
    parser.add_argument("--channel", choices=CHANNELS, default="prerelease")
    parser.add_argument("--ga-review-id")
    parser.add_argument("--tag")
    parser.add_argument("--commit")
    parser.add_argument("--repository")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        receipt = {"schema_version": "release-gate-v1", **channel_metadata(args.channel),
                   "ga_review": None,
                   "scope": "versions_only" if args.versions_only else "signed_tag_and_exact_commit_ci"}
        if not args.versions_only:
            if args.tag != "v" + receipt["version"]:
                raise ValueError("Tag version disagrees")
            if os.environ.get("GITHUB_REF") != "refs/tags/" + args.tag:
                raise ValueError("Dispatch must select the release tag")
            if os.environ.get("GITHUB_REPOSITORY") != args.repository:
                raise ValueError("Repository mismatch")
            if args.channel == "stable":
                receipt["ga_review"] = verify_ga_review(args.commit, args.ga_review_id)
            receipt.update(verify_tag(args.tag, args.commit,
                                      os.environ["RELEASE_GPG_PUBLIC_KEY"],
                                      os.environ["RELEASE_GPG_FINGERPRINT"]))
            receipt.update(verify_ci(args.repository, args.commit))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print("Release gate failed; diagnostics withheld. Review tag, versions, trusted key and exact-commit CI.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
