"""Review preparation must preserve every scanner match and never approve VEX."""

from __future__ import annotations

import json

import pytest

from scripts.container_finding_review import prepare


def scan(path, *, advisory="CVE-2026-85091", ignored=None):
    path.write_text(json.dumps({"matches": [{
        "vulnerability": {"id": advisory, "namespace": "nvd:cpe", "severity": "High",
                          "dataSource": "https://example.invalid/advisory", "fix": {"state": "", "versions": []}},
        "artifact": {"name": "zlib", "version": "1.3.2-r0", "purl": "pkg:apk/alpine/zlib@1.3.2-r0"},
    }], "ignoredMatches": ignored or [], "db": {"status": {"built": "2026-10-05T00:00:00Z"}}}))
    return path


def test_exact_app_profiles_share_review_rows_but_postgres_stays_separate(tmp_path):
    base = scan(tmp_path / "base.json")
    extras = scan(tmp_path / "extras.json")
    postgres = scan(tmp_path / "postgres.json")
    result = prepare(base, extras, postgres)
    assert result["raw_matches"] == {"application_base": 1, "application_all_extras": 1,
                                     "postgresql_derivative": 1}
    assert result["review_rows"] == 2
    assert result["release_gate"] == "blocked_by_raw_matches"
    assert result["vex_approved"] is False
    assert result["findings"][0]["profiles"] == ["base", "all-extras"]
    assert {item["image"] for item in result["findings"]} == {"application", "postgresql"}
    assert all(item["verdict"] == "needs_review" and item["vex_status"] is None
               and item["reviewer"] is None for item in result["findings"])


def test_different_app_profiles_refuse_combined_review(tmp_path):
    base = scan(tmp_path / "base.json")
    extras = scan(tmp_path / "extras.json", advisory="CVE-2026-102010")
    postgres = scan(tmp_path / "postgres.json")
    with pytest.raises(ValueError, match="profiles differ"):
        prepare(base, extras, postgres)


def test_ignored_findings_refuse_review_queue(tmp_path):
    base = scan(tmp_path / "base.json", ignored=[{"reason": "suppressed"}])
    extras = scan(tmp_path / "extras.json")
    postgres = scan(tmp_path / "postgres.json")
    with pytest.raises(ValueError, match="suppressed"):
        prepare(base, extras, postgres)
