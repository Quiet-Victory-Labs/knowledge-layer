"""Scan all reachable Git history, redacting values and customer-like filenames.

Run with: uv run --no-project --with detect-secrets==1.5.0 python
scripts/check_release_history.py --output .local/release/history-scan.json
This is a candidate gate, not a proof of credential absence or fixture rights.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_BLOB_BYTES = 32 * 1024 * 1024
SYNTHETIC_DATA_PREFIXES = (
    "data/connectable/northstar/", "data/connectable/juniper/",
    "tests/fixtures/enterprise_benchmark/",
)


def reviewed_history(path: Path) -> set[tuple[str, str, str, str]]:
    """Historical exemptions bind exact bytes/path/rule, never a value globally."""
    if not path.exists():
        return set()
    from check_secrets import _read_baseline, _unique_keys
    data = json.loads(_read_baseline(path), object_pairs_hook=_unique_keys)
    if data.get("schema_version") != "reviewed-history-candidates-v1":
        raise ValueError("Invalid historical candidate baseline")
    result = set()
    for candidate in data["reviewed_candidates"]:
        if (candidate.get("review_basis") not in {
                "synthetic_environment_variable_reference", "synthetic_refusal_or_redaction_test_canary"}
                or not re.fullmatch(r"[a-f0-9]{40}", candidate["object"])
                or not re.fullmatch(r"[a-f0-9]{64}", candidate["path_sha256"])
                or not re.fullmatch(r"[a-f0-9]{40}", candidate["candidate_sha1"])
                or not isinstance(candidate["kind"], str) or not candidate["kind"]):
            raise ValueError("Invalid historical candidate identity")
        identity = tuple(candidate[key] for key in ("object", "path_sha256", "candidate_sha1", "kind"))
        if identity in result:
            raise ValueError("Duplicate historical candidate identity")
        result.add(identity)
    return result


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True,
                          timeout=120).stdout


def path_reason(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    base = PurePosixPath(path).name.lower()
    if not parts or path.startswith("/") or ".." in parts:
        return "unsafe_path"
    if base == ".env" or base.startswith(".env.") and base != ".env.example":
        return "environment_file"
    if {".local", ".venv", "node_modules", "downloads", "customer-data", "raw"} & set(parts):
        return "private_or_downloaded_data_path"
    if base.endswith((".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db")):
        return "credential_or_database_file"
    if base.endswith((".csv", ".parquet", ".arrow", ".xlsx")) and not path.startswith(SYNTHETIC_DATA_PREFIXES):
        return "unreviewed_data_fixture"
    return None


def scan_history() -> tuple[dict, list[dict]]:
    from check_secrets import _candidate_identities, _read_baseline, _unique_keys
    from detect_secrets.core.scan import scan_file
    from detect_secrets.settings import transient_settings

    if git("rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("A complete Git clone is required")
    raw_baseline = _read_baseline(ROOT / ".secrets.baseline")
    allowed = _candidate_identities(raw_baseline)
    baseline = json.loads(raw_baseline, object_pairs_hook=_unique_keys)
    historical = reviewed_history(ROOT / ".release-history-baseline.json")
    commits = git("rev-list", "--branches", "--tags", "HEAD").decode().splitlines()
    if not commits:
        raise ValueError("No reachable Git history")
    blobs = set()
    issues = []
    paths = set()
    for commit in commits:
        for entry in git("ls-tree", "-r", "-z", commit).split(b"\0"):
            if not entry:
                continue
            header, encoded_path = entry.split(b"\t", 1)
            mode, kind, oid = header.decode().split()
            path = encoded_path.decode("utf-8")
            paths.add(path)
            if kind != "blob" or mode == "120000":
                issues.append({"object": oid, "path_sha256": hashlib.sha256(encoded_path).hexdigest(),
                               "reason": "symlink_or_submodule"})
                continue
            blobs.add((oid, path))
    for path in sorted(paths):
        if reason := path_reason(path):
            issues.append({"path_sha256": hashlib.sha256(path.encode()).hexdigest(), "reason": reason})
    binary_objects = set()
    scanned = 0
    reviewed = 0
    refs = git("for-each-ref", "--format=%(objectname) %(objecttype)").decode().splitlines()
    messages = {(commit, "commit") for commit in commits}
    messages.update(tuple(line.split()) for line in refs if line.endswith(" tag"))
    # Copy the detector's reviewed plugin/filter configuration; never load local
    # plugin paths, and never verify candidate credentials over the network.
    settings = {key: baseline[key] for key in ("plugins_used", "filters_used")}
    if any(item.get("path", "").startswith("file:")
           for group in settings.values() for item in group):
        raise ValueError("Local scanner plugins are forbidden")
    with tempfile.TemporaryDirectory(prefix="knowledge-history-") as temporary:
        temporary_root = Path(temporary)
        with open(os.devnull, "w") as discarded, contextlib.redirect_stdout(discarded), \
                contextlib.redirect_stderr(discarded), transient_settings(settings):
            for oid, path in sorted(blobs | {(oid, f"git-message/{kind}/{oid}.txt") for oid, kind in messages}):
                if path == ".secrets.baseline":
                    continue
                size = int(git("cat-file", "-s", oid))
                if size > MAX_BLOB_BYTES:
                    issues.append({"object": oid, "reason": "object_exceeds_scan_budget"})
                    continue
                content = git("cat-file", "-p", oid)
                if path.startswith("git-message/"):
                    content = content.partition(b"\n\n")[2]
                if b"\0" in content:
                    binary_objects.add(oid)
                    issues.append({"object": oid, "reason": "binary_requires_manual_review"})
                    continue
                try:
                    content.decode("utf-8")
                except UnicodeError:
                    binary_objects.add(oid)
                    issues.append({"object": oid, "reason": "non_utf8_requires_manual_review"})
                    continue
                # Keep the original basename so detector lock/schema filters
                # behave as they do in the tracked-file gate.
                target = temporary_root / PurePosixPath(path).name
                target.write_bytes(content)
                target.chmod(0o600)
                for candidate in scan_file(str(target)):
                    if (path, candidate.secret_hash, candidate.type) not in allowed:
                        path_hash = hashlib.sha256(path.encode()).hexdigest()
                        identity = (oid, path_hash, candidate.secret_hash, candidate.type)
                        if identity in historical:
                            reviewed += 1
                        else:
                            issues.append({"object": oid, "path_sha256": path_hash,
                                           "candidate_sha1": candidate.secret_hash, "kind": candidate.type,
                                           "reason": "unreviewed_secret_candidate"})
                target.unlink()
                scanned += 1
    report = {"schema_version": "release-history-scan-v1", "result": "passed" if not issues else "review_required",
              "reachable_commits": len(commits), "unique_blob_path_pairs": len(blobs),
              "scanned_text_objects": scanned, "binary_objects": len(binary_objects),
              "reviewed_historical_candidates": reviewed,
              "issue_count": len(issues), "candidate_network_verification": False,
              "customer_data_rights": "independent_review_required", "full_history": True}
    return report, issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-candidates", type=Path,
                        help="Optional mode-0600 local hashed identities; do not upload as release assets")
    args = parser.parse_args()
    try:
        report, issues = scan_history()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        if args.private_candidates:
            args.private_candidates.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(args.private_candidates, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                json.dump(issues, handle, indent=2)
                handle.write("\n")
            args.private_candidates.chmod(0o600)
        print(json.dumps(report, sort_keys=True))
        return 0 if not issues else 1
    except Exception:  # noqa: BLE001 - plugin exceptions must never expose candidate content
        print("Full-history release scan failed; diagnostics withheld")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
