"""Prepare an exact-scan review queue; never grant VEX or release exceptions.

The base and all-extra application profiles must report the same OS/runtime
matches before they can share one review row. Every raw scanner match remains
unresolved. This tool is deliberately separate from the strict release gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

SEVERITIES = ("Critical", "High", "Medium", "Low", "Negligible", "Unknown")


def read_scan(path: Path) -> dict:
    raw = path.read_bytes()
    scan = json.loads(raw)
    if (not isinstance(scan, dict) or not isinstance(scan.get("matches"), list)
            or not isinstance(scan.get("ignoredMatches", []), list)
            or scan.get("ignoredMatches")):
        raise ValueError("Invalid or suppressed scanner matches")
    findings = []
    seen = set()
    for match in scan["matches"]:
        if not isinstance(match, dict):
            raise TypeError("Invalid scanner match")
        vulnerability, artifact = match.get("vulnerability"), match.get("artifact")
        if not isinstance(vulnerability, dict) or not isinstance(artifact, dict):
            raise TypeError("Missing vulnerability or package identity")
        advisory, severity = vulnerability.get("id"), vulnerability.get("severity")
        namespace = vulnerability.get("namespace")
        package, version, purl = (artifact.get("name"), artifact.get("version"),
                                  artifact.get("purl"))
        if (not isinstance(advisory, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{3,100}", advisory)
                or severity not in SEVERITIES or not isinstance(namespace, str) or not namespace
                or not all(isinstance(value, str) and value for value in (package, version, purl))):
            raise ValueError("Incomplete scanner match identity")
        identity = (namespace, advisory, purl)
        if identity in seen:
            raise ValueError("Duplicate scanner match identity")
        seen.add(identity)
        fix = vulnerability.get("fix", {})
        if not isinstance(fix, dict) or not isinstance(fix.get("versions", []), list):
            raise TypeError("Invalid scanner fix information")
        findings.append({
            "advisory": advisory, "namespace": namespace, "severity": severity,
            "package": package, "version": version, "purl": purl,
            "fix_state": fix.get("state", ""), "fix_versions": fix.get("versions", []),
            "advisory_source": vulnerability.get("dataSource", ""),
        })
    findings.sort(key=lambda item: (SEVERITIES.index(item["severity"]), item["advisory"],
                                    item["package"], item["purl"]))
    return {"sha256": hashlib.sha256(raw).hexdigest(), "findings": findings,
            "database_built": scan.get("db", {}).get("status", {}).get("built")}


def prepare(app_base: Path, app_extras: Path, postgres: Path) -> dict:
    base, extras, database = (read_scan(path) for path in (app_base, app_extras, postgres))
    if base["findings"] != extras["findings"]:
        raise ValueError("Application profiles differ; review each image separately")
    findings = []
    for image, profiles, scan in (
            ("application", ["base", "all-extras"], base),
            ("postgresql", ["shipped-derivative"], database)):
        for item in scan["findings"]:
            findings.append({"image": image, "profiles": profiles, **item,
                             "verdict": "needs_review", "vex_status": None,
                             "reviewer": None, "evidence": None})
    return {
        "schema_version": "container-finding-review-queue-v1",
        "scans": {"application_base": {"sha256": base["sha256"],
                                       "database_built": base["database_built"]},
                  "application_all_extras": {"sha256": extras["sha256"],
                                             "database_built": extras["database_built"]},
                  "postgresql_derivative": {"sha256": database["sha256"],
                                            "database_built": database["database_built"]}},
        "raw_matches": {"application_base": len(base["findings"]),
                        "application_all_extras": len(extras["findings"]),
                        "postgresql_derivative": len(database["findings"])},
        "review_rows": len(findings),
        "severity": dict(sorted(Counter(item["severity"] for item in findings).items())),
        "release_gate": "blocked_by_raw_matches" if findings else "scanner_review_still_required",
        "vex_approved": False,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-base", type=Path, required=True)
    parser.add_argument("--app-extras", type=Path, required=True)
    parser.add_argument("--postgres", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        queue = prepare(args.app_base, args.app_extras, args.postgres)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(queue, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(json.dumps({"review_rows": queue["review_rows"],
                          "raw_matches": queue["raw_matches"], "release_gate": queue["release_gate"]}))
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        print("Container finding review queue refused; verify exact unsuppressed scans and destination")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
