"""Read actual GitHub release protections; no settings or approvals are changed."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from release_gate import REQUIRED_JOBS


def api(endpoint: str):
    result = subprocess.run(["gh", "api", endpoint], check=True, capture_output=True,
                            text=True, timeout=30)
    return json.loads(result.stdout)


def branch_controls(protection: dict) -> None:
    if not isinstance(protection, dict):
        raise TypeError("main_protection_invalid")
    checks = protection["required_status_checks"]
    reviews = protection["required_pull_request_reviews"]
    admins = protection.get("enforce_admins")
    bypass = reviews.get("bypass_pull_request_allowances", {}) if isinstance(reviews, dict) else None
    if (not isinstance(checks, dict) or not isinstance(reviews, dict)
            or not isinstance(admins, dict) or not isinstance(bypass, dict)
            or not isinstance(checks.get("checks"), list)
            or any(not isinstance(item, dict) for item in checks["checks"])
            or type(reviews.get("required_approving_review_count")) is not int
            or checks.get("strict") is not True
            or not REQUIRED_JOBS <= {item["context"] for item in checks["checks"]
                                     if item.get("app_id") == 15368}
            or admins.get("enabled") is not True
            or reviews.get("required_approving_review_count", 0) < 1
            or reviews.get("dismiss_stale_reviews") is not True
            or reviews.get("require_code_owner_reviews") is not True
            or reviews.get("require_last_push_approval") is not True
            or bypass.get("users") or bypass.get("teams") or bypass.get("apps")):
        raise ValueError("main_protection_incomplete")


def environment_controls(environment: dict, policies: dict, *, actor: str, tag: str) -> None:
    if (not isinstance(environment, dict) or not isinstance(policies, dict)
            or not isinstance(environment.get("protection_rules"), list)
            or any(not isinstance(item, dict) for item in environment["protection_rules"])):
        raise ValueError("environment_control_invalid")
    rules = [item for item in environment["protection_rules"]
             if item.get("type") == "required_reviewers"]
    if len(rules) != 1 or rules[0].get("prevent_self_review") is not True:
        raise ValueError("environment_independent_review_missing")
    # Only named individual human accounts are supported here; a Team may have
    # only the dispatcher as its member. Unknown/group identities refuse.
    reviewers = rules[0].get("reviewers", [])
    if not isinstance(reviewers, list) or not reviewers or any(not isinstance(item, dict)
                            or item.get("type") != "User"
                            or not isinstance(item.get("reviewer"), dict)
                            or item.get("reviewer", {}).get("type") != "User"
                            or not isinstance(item.get("reviewer", {}).get("login"), str)
                            or not item["reviewer"]["login"]
                            or item["reviewer"]["login"].casefold() == actor.casefold()
                            for item in reviewers):
        raise ValueError("environment_independent_review_missing")
    if environment.get("can_admins_bypass") is not False:
        raise ValueError("environment_admin_bypass_unverified")
    if environment.get("deployment_branch_policy") != {
            "protected_branches": False, "custom_branch_policies": True}:
        raise ValueError("environment_tag_policy_missing")
    entries = policies["branch_policies"]
    if (not isinstance(entries, list) or type(policies.get("total_count")) is not int
            or policies.get("total_count") != len(entries) or not entries
            or any(not isinstance(item, dict) or item.get("type") != "tag" or item.get("name") not in {tag, "v*"}
                   for item in entries)):
        raise ValueError("environment_tag_policy_unverified")


def inspect(repository: str, *, actor: str, tag: str, reader=api) -> dict:
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", actor)
            or not re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", tag)):
        raise ValueError("release_control_identity_invalid")
    prefix = f"repos/{repository}"
    result = {"schema_version": "release-controls-v1", "repository": repository,
              "tag": tag, "checks": [], "settings_changed": False,
              "human_approval_inferred": False}
    try:
        repo = reader(prefix)
        if not isinstance(repo, dict) or repo.get("default_branch") != "main":
            raise ValueError("default_branch_unverified")
        branch_controls(reader(prefix + "/branches/main/protection"))
        result["checks"].append({"name": "main_protection", "result": "passed"})
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        result["checks"].append({"name": "main_protection", "result": "refused"})
    for name in ("oss-release-candidate", "oss-release"):
        try:
            path = prefix + "/environments/" + name
            environment_controls(reader(path), reader(path + "/deployment-branch-policies?per_page=100"),
                                 actor=actor, tag=tag)
            result["checks"].append({"name": name, "result": "passed"})
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            result["checks"].append({"name": name, "result": "refused"})
    result["result"] = "passed" if all(item["result"] == "passed" for item in result["checks"]) else "refused"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = inspect(args.repository, actor=args.actor, tag=args.tag)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        return 0 if result["result"] == "passed" else 1
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print("Release control verification refused; no settings changed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
