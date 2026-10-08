"""Evaluate raw container scans against explicit, expiring finding decisions.

Raw scanner matches are never removed. A decision applies only to one image,
advisory namespace, advisory ID and installed package URL. The signed release
retains the raw scans, decisions and this derived evaluation together.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SCANS = {
    "application": "container-vulnerabilities.json",
    "postgresql": "postgres-vulnerabilities.json",
}
DECISIONS = "container-risk-decisions.json"
EVALUATION = "container-risk-evaluation.json"
SEVERITIES = {"Critical", "High", "Medium", "Low", "Negligible", "Unknown"}
BLOCKING_SEVERITIES = {"Critical", "High", "Unknown"}
DISPOSITIONS = {"not_affected", "mitigated", "risk_accepted"}


def _read_json(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise TypeError("Invalid container security document")
    return document, hashlib.sha256(raw).hexdigest()


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _identity(image: str, match: dict) -> tuple[str, str, str, str]:
    vulnerability, artifact = match.get("vulnerability"), match.get("artifact")
    if not isinstance(vulnerability, dict) or not isinstance(artifact, dict):
        raise TypeError("Incomplete scanner match")
    advisory, namespace, severity = (vulnerability.get("id"),
                                     vulnerability.get("namespace"),
                                     vulnerability.get("severity"))
    purl = artifact.get("purl")
    if (not all(_text(value) for value in (advisory, namespace, purl))
            or not isinstance(severity, str) or severity not in SEVERITIES
            or not all(_text(artifact.get(key)) for key in ("name", "version"))):
        raise ValueError("Incomplete scanner match identity")
    return image, namespace, advisory, purl


def _read_decisions(path: Path, today: date) -> tuple[dict, str]:
    document, digest = _read_json(path)
    if document.get("schema_version") != "container-risk-decisions-v1" or not isinstance(document.get("decisions"), list):
        raise ValueError("Invalid container risk decisions")
    indexed = {}
    for decision in document["decisions"]:
        if not isinstance(decision, dict) or set(decision) != {
                "image", "namespace", "advisory", "purl", "disposition",
                "rationale", "evidence", "reviewer", "reviewed_on", "expires_on"}:
            raise ValueError("Invalid container risk decision fields")
        image, namespace, advisory, purl = (decision.get(key) for key in
                                              ("image", "namespace", "advisory", "purl"))
        if (not isinstance(image, str) or image not in SCANS
                or not all(_text(value) for value in (namespace, advisory, purl))
                or not isinstance(decision["disposition"], str)
                or decision["disposition"] not in DISPOSITIONS
                or not _text(decision["reviewer"])
                or not isinstance(decision["rationale"], str)
                or len(decision["rationale"].strip()) < 20
                or not isinstance(decision["evidence"], list)
                or not decision["evidence"]
                or not all(isinstance(item, str) and
                           item.startswith(("https://", "docs/"))
                           for item in decision["evidence"])):
            raise ValueError("Incomplete container risk decision")
        try:
            reviewed = date.fromisoformat(decision["reviewed_on"])
            expires = date.fromisoformat(decision["expires_on"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid container risk decision dates") from exc
        if (reviewed > today or expires <= today or expires > reviewed + timedelta(days=180)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", decision["reviewed_on"])
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", decision["expires_on"])):
            raise ValueError("Container risk decision is stale or unbounded")
        key = image, namespace, advisory, purl
        if key in indexed:
            raise ValueError("Duplicate container risk decision")
        indexed[key] = decision
    return indexed, digest


def evaluate(directory: Path, *, today: date | None = None) -> dict:
    """Return a complete evaluation; callers block if its result is not passed."""
    today = today or datetime.now(UTC).date()
    decisions, decision_digest = _read_decisions(directory / DECISIONS, today)
    findings = []
    scan_digests = {}
    seen = set()
    for image, filename in SCANS.items():
        scan, digest = _read_json(directory / filename)
        ignored = scan.get("ignoredMatches", [])
        if (not isinstance(scan.get("matches"), list)
                or not isinstance(ignored, list)
                or ignored):
            raise ValueError("Container scan is invalid or has suppressed matches")
        scan_digests[filename] = digest
        for match in scan["matches"]:
            if not isinstance(match, dict):
                raise TypeError("Invalid container scanner match")
            key = _identity(image, match)
            if key in seen:
                raise ValueError("Duplicate container scanner match")
            seen.add(key)
            vulnerability = match["vulnerability"]
            fix = vulnerability.get("fix", {})
            if not isinstance(fix, dict) or not isinstance(fix.get("versions", []), list):
                raise TypeError("Invalid container scanner fix data")
            severity = vulnerability["severity"]
            decision = decisions.get(key)
            blocking = severity in BLOCKING_SEVERITIES and decision is None
            findings.append({
                "image": image, "namespace": key[1], "advisory": key[2], "purl": key[3],
                "severity": severity, "fix_state": fix.get("state", ""),
                "disposition": decision["disposition"] if decision else "unreviewed",
                "blocking": blocking,
            })
    if set(decisions) - seen:
        raise ValueError("Container risk decision does not match an installed finding")
    findings.sort(key=lambda item: (item["image"], item["advisory"], item["namespace"], item["purl"]))
    blockers = sum(item["blocking"] for item in findings)
    return {
        "schema_version": "container-risk-evaluation-v1",
        "result": "blocked" if blockers else "passed",
        "raw_matches": len(findings),
        "blocking_matches": blockers,
        "reviewed_matches": sum(item["disposition"] != "unreviewed" for item in findings),
        "tracked_matches": sum(item["disposition"] == "unreviewed" and not item["blocking"]
                               for item in findings),
        "scan_sha256": scan_digests,
        "decisions_sha256": decision_digest,
        "findings": findings,
    }


def check(directory: Path, *, today: date | None = None) -> dict:
    report = evaluate(directory, today=today)
    if report["blocking_matches"]:
        raise ValueError("Container risk evaluation has unresolved serious findings")
    if json.loads((directory / EVALUATION).read_text()) != report:
        raise ValueError("Container risk evaluation disagrees with raw scans or decisions")
    return report
