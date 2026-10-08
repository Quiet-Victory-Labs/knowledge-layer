from __future__ import annotations

import copy
import importlib
import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def controls(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("check_release_controls")


def protected(controls):
    return {"required_status_checks": {"strict": True, "checks": [
        {"context": name, "app_id": 15368} for name in controls.REQUIRED_JOBS]},
        "enforce_admins": {"enabled": True}, "required_pull_request_reviews": {
            "required_approving_review_count": 1, "dismiss_stale_reviews": True,
            "require_code_owner_reviews": True, "require_last_push_approval": True}}


def environment():
    return {"protection_rules": [{"type": "required_reviewers", "prevent_self_review": True,
        "reviewers": [{"type": "User", "reviewer": {"login": "independent", "type": "User"}}]}],
        "can_admins_bypass": False, "deployment_branch_policy": {
            "protected_branches": False, "custom_branch_policies": True}}


def policies():
    return {"total_count": 1, "branch_policies": [{"type": "tag", "name": "v*"}]}


def reader(controls):
    def read(path):
        if path.endswith("/protection"):
            return protected(controls)
        if "deployment-branch-policies" in path:
            return policies()
        if "/environments/" in path:
            return environment()
        return {"default_branch": "main"}
    return read


def test_actual_controls_pass_without_inventing_approval(controls):
    result = controls.inspect("synthetic/repo", actor="maintainer", tag="v1.0.0", reader=reader(controls))
    assert result["result"] == "passed" and len(result["checks"]) == 3
    assert result["settings_changed"] is False and result["human_approval_inferred"] is False


@pytest.mark.parametrize("change", ["admin", "strict", "wrong_app", "missing_job", "stale", "owner", "last_push", "bypass", "no_review"])
def test_branch_protection_refuses_bypass_and_incomplete_checks(controls, change):
    value = protected(controls)
    if change == "admin": value["enforce_admins"]["enabled"] = False
    elif change == "strict": value["required_status_checks"]["strict"] = False
    elif change == "wrong_app": value["required_status_checks"]["checks"][0]["app_id"] = None
    elif change == "missing_job": value["required_status_checks"]["checks"].pop()
    elif change == "bypass": value["required_pull_request_reviews"]["bypass_pull_request_allowances"] = {"users": [{"login": "maintainer"}]}
    else:
        key = {"stale": "dismiss_stale_reviews", "owner": "require_code_owner_reviews",
               "last_push": "require_last_push_approval", "no_review": "required_approving_review_count"}[change]
        value["required_pull_request_reviews"][key] = False
    with pytest.raises(ValueError): controls.branch_controls(value)


@pytest.mark.parametrize("change", ["self", "bot", "team", "no_rule", "no_prevent", "admin", "unknown_admin", "branches", "all_tags", "partial_pagination", "unknown_pattern"])
def test_environments_refuse_self_review_and_unverified_tag_rules(controls, change):
    value, branches = environment(), policies()
    if change in {"self", "bot", "team"}:
        entry = value["protection_rules"][0]["reviewers"][0]
        if change == "self": entry["reviewer"]["login"] = "MAINTAINER"
        elif change == "bot": entry["reviewer"]["type"] = "Bot"
        else: entry["type"] = "Team"
    elif change == "no_rule": value["protection_rules"] = []
    elif change == "no_prevent": value["protection_rules"][0]["prevent_self_review"] = False
    elif change == "admin": value["can_admins_bypass"] = True
    elif change == "unknown_admin": value.pop("can_admins_bypass")
    elif change == "branches": branches["branch_policies"][0]["type"] = "branch"
    elif change == "all_tags": branches["branch_policies"][0]["name"] = "*"
    elif change == "unknown_pattern": branches["branch_policies"][0]["name"] = "refs/tags/v*"
    else: branches["total_count"] = 101
    with pytest.raises(ValueError): controls.environment_controls(value, branches, actor="maintainer", tag="v1.0.0")


def test_api_denial_refuses_and_never_prints_private_diagnostics(controls, tmp_path, monkeypatch, capsys):
    def deny(_path):
        raise subprocess.CalledProcessError(1, "gh", stderr="PRIVATE_TOKEN_MARKER")
    monkeypatch.setattr(controls, "api", deny)
    # Default reader is captured by inspect; inject the denied reader explicitly.
    original = controls.inspect
    monkeypatch.setattr(controls, "inspect", lambda repository, **kwargs: original(repository, **kwargs, reader=deny))
    output = tmp_path / "receipt.json"
    monkeypatch.setattr("sys.argv", ["controls", "--repository", "synthetic/repo", "--actor", "maintainer",
                                   "--tag", "v1.0.0", "--output", str(output)])
    assert controls.main() == 1
    assert "PRIVATE_TOKEN_MARKER" not in capsys.readouterr().out
    result = json.loads(output.read_text())
    assert all(item["result"] == "refused" for item in result["checks"])


def test_one_missing_environment_cannot_be_hidden_by_other_success(controls):
    read = reader(controls)
    def missing(path):
        if path.endswith("oss-release"):
            raise ValueError("missing")
        return copy.deepcopy(read(path))
    result = controls.inspect("synthetic/repo", actor="maintainer", tag="v1.0.0", reader=missing)
    assert result["result"] == "refused" and [item["result"] for item in result["checks"]] == ["passed", "passed", "refused"]


@pytest.mark.parametrize("section", ["enforce_admins", "bypass_pull_request_allowances"])
def test_null_unknown_protection_fields_refuse_without_crashing(controls, section):
    read = reader(controls)
    def unknown(path):
        result = read(path)
        if path.endswith("/protection"):
            if section == "enforce_admins":
                result[section] = None
            else:
                result["required_pull_request_reviews"][section] = None
        return result
    result = controls.inspect("synthetic/repo", actor="maintainer", tag="v1.0.0", reader=unknown)
    assert result["result"] == "refused" and result["checks"][0]["result"] == "refused"


def test_boolean_policy_count_is_not_verified_pagination(controls):
    value = policies()
    value["total_count"] = True
    with pytest.raises(ValueError):
        controls.environment_controls(environment(), value, actor="maintainer", tag="v1.0.0")
