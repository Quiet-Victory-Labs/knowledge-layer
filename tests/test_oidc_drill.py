"""Offline guards for the optional real Keycloak/PostgreSQL qualification drill."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

OFFLINE_VALUE = "opaque fixture"


@pytest.fixture
def drill():
    path = Path(__file__).resolve().parents[1] / "scripts/drill_oidc_keycloak.py"
    spec = importlib.util.spec_from_file_location("oidc_qualification_drill", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_separates_service_credentials_from_browser_and_user_grants(drill, tmp_path, monkeypatch):
    def fake_openssl(arguments, **_kwargs):
        (tmp_path / "tls.key").write_text("offline fixture key", encoding="utf-8")
        (tmp_path / "tls.crt").write_text("offline fixture certificate", encoding="utf-8")
        assert arguments[0] == "openssl"
    monkeypatch.setattr(drill.subprocess, "run", fake_openssl)
    values = drill._fixture(tmp_path)
    realm = json.loads((tmp_path / "knowledge-drill-realm.json").read_text(encoding="utf-8"))
    clients = {client["clientId"]: client for client in realm["clients"]}
    service = clients[drill.SERVICE_CLIENT]
    assert service["serviceAccountsEnabled"] is True
    assert service["directAccessGrantsEnabled"] is False and service["standardFlowEnabled"] is False
    assert service["redirectUris"] == [] and service["webOrigins"] == []
    assert service["secret"] == values["service_secret"] != values["web_secret"]
    assert clients["knowledge-web"]["serviceAccountsEnabled"] is False
    assert clients["knowledge-web"]["standardFlowEnabled"] is True
    audience = next(mapper for mapper in service["protocolMappers"] if mapper["name"] == "knowledge-audience")
    assert audience["config"]["included.custom.audience"] == "knowledge-agent"
    assert {user["username"] for user in realm["users"]} == {"owner", "reader"}
    for name in (*values, "knowledge-drill-realm.json", "tls.key", "admin.env", "pg.env"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600


def test_service_token_uses_client_credentials_and_requested_scopes_only(drill):
    observed = []
    def handle(request):
        observed.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "offline opaque fixture"})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        token = drill._service_token(http, "https://id.example.test/realms/fixture", {"service_secret": OFFLINE_VALUE})
        drill._service_token(http, "https://id.example.test/realms/fixture", {"service_secret": OFFLINE_VALUE},
                             scope="knowledge.agent knowledge.sources.manage")
    assert token == "offline opaque fixture"
    assert set(observed[0]) == {"grant_type", "client_id", "client_secret", "scope"}
    assert observed[0]["grant_type"] == ["client_credentials"]
    assert observed[0]["client_id"] == [drill.SERVICE_CLIENT]
    assert observed[0]["scope"] == [drill.DELEGATION_SCOPES]
    assert "knowledge.autonomy" not in observed[1]["scope"][0].split()


def test_service_token_refuses_idp_failure_instead_of_inventing_token(drill):
    with (httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(401))) as http,
          pytest.raises(httpx.HTTPStatusError)):
        drill._service_token(http, "https://id.example.test/realms/fixture", {"service_secret": OFFLINE_VALUE})


@pytest.mark.parametrize("service_name", ["service-account-knowledge-delegation-service", "unrelated-account"])
def test_service_subject_is_pinned_to_exact_admin_service_account(drill, service_name):
    def handle(request):
        if request.url.path.endswith("/service-account-user"):
            return httpx.Response(200, json={"id": "exact-fixture-subject", "username": service_name})
        return httpx.Response(200, json=[{"id": "fixture-client", "clientId": drill.SERVICE_CLIENT}])
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        if service_name.startswith("service-account-"):
            assert drill._service_subject(http, "https://id.example.test/admin/realms/fixture", "Bearer fixture") == "exact-fixture-subject"
        else:
            with pytest.raises(AssertionError):
                drill._service_subject(http, "https://id.example.test/admin/realms/fixture", "Bearer fixture")


def test_mcp_harness_sends_modern_reserved_metadata_and_matching_name_header(drill):
    observed = []
    def handle(request):
        observed.append(request)
        return httpx.Response(200, json={"result": {"structuredContent": {"result": "already_applied"}}})
    arguments = {"connection_id": "fixture-source", "request": {"revision": 1}}
    with httpx.Client(base_url="https://knowledge.example.test", transport=httpx.MockTransport(handle)) as client:
        result = drill._mcp_call(client, "opaque fixture", "/agent/mcp/", "apply_autonomy_policy", arguments)
    request = observed[0]
    body = json.loads(request.content)
    assert result["structuredContent"]["result"] == "already_applied"
    assert request.headers["MCP-Protocol-Version"] == body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert request.headers["Mcp-Method"] == body["method"] == "tools/call"
    assert request.headers["Mcp-Name"] == body["params"]["name"] == "apply_autonomy_policy"
    assert body["params"]["arguments"] == arguments
    assert body["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] == {}


@pytest.mark.parametrize("status", [401, 413, 500])
def test_mcp_harness_never_counts_http_refusal_as_tool_success(drill, status):
    with httpx.Client(base_url="https://knowledge.example.test", transport=httpx.MockTransport(
        lambda _request: httpx.Response(status, json={"detail": "fixture refusal"}),
    )) as client, pytest.raises(AssertionError):
        drill._mcp_call(client, "opaque fixture", "/agent/mcp/", "apply_autonomy_policy", {})


def test_denial_receipt_requires_all_three_real_surface_results(drill, monkeypatch):
    calls = []
    def refused(_client, _token, route, name, arguments):
        calls.append((route, name, arguments))
        return {"isError": True}
    monkeypatch.setattr(drill, "_mcp_call", refused)
    with httpx.Client(base_url="https://knowledge.example.test", transport=httpx.MockTransport(
        lambda _request: httpx.Response(403),
    )) as client:
        result = drill._assert_delegation_refused(client, "opaque fixture", "fixture-source", {"revision": 1}, http_status=403)
    assert result == {"http_status": 403, "compact_mcp_error": True, "advanced_mcp_error": True}
    assert [call[0] for call in calls] == list(drill.MCP_ROUTES)
    assert all(call[2] == {"connection_id": "fixture-source", "request": {"revision": 1}} for call in calls)


@pytest.mark.parametrize("result", [{}, {"isError": False}])
def test_expected_denial_fails_when_any_mcp_surface_accepts(drill, monkeypatch, result):
    monkeypatch.setattr(drill, "_mcp_call", lambda *_args: result)
    with httpx.Client(base_url="https://knowledge.example.test", transport=httpx.MockTransport(
        lambda _request: httpx.Response(403),
    )) as client, pytest.raises(AssertionError):
        drill._assert_delegation_refused(client, "opaque fixture", "fixture-source", {}, http_status=403)


def test_timed_out_launch_still_cleans_exact_owned_container(drill, tmp_path, monkeypatch):
    name = "knowledge-oidc-pg-aaaaaaaaaaaaaaaa"
    existing, calls = {"present": False}, []
    def run(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1] == "run":
            assert arguments[arguments.index("--name") + 1] == name
            assert arguments[arguments.index("--label") + 1] == drill.OWNERSHIP_LABEL + "=" + name
            existing["present"] = True
            raise subprocess.TimeoutExpired(arguments, 45, output=b"private fixture diagnostic")
        if arguments[1:3] == ["container", "ls"]:
            return subprocess.CompletedProcess(arguments, 0, name if existing["present"] else "", "")
        if arguments[1] == "inspect":
            return subprocess.CompletedProcess(arguments, 0, json.dumps({drill.OWNERSHIP_LABEL: name}), "")
        assert arguments == ["docker", "rm", "--force", name]
        existing["present"] = False
        return subprocess.CompletedProcess(arguments, 0, name, "")
    monkeypatch.setattr(drill.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="did not complete") as error:
        drill._run_disposable(tmp_path, "https://localhost:18443/realms/fixture", 15544, 18443,
                              name, "knowledge-oidc-keycloak-aaaaaaaaaaaaaaaa", {})
    assert "private fixture diagnostic" not in str(error.value)
    assert existing["present"] is False
    assert [arguments for arguments in calls if arguments[1] == "rm"] == [["docker", "rm", "--force", name]]
    assert len([arguments for arguments in calls if arguments[1] == "run"]) == 1


def test_cleanup_wrong_label_never_removes_borrowed_container(drill, monkeypatch):
    name, calls = "knowledge-oidc-pg-aaaaaaaaaaaaaaaa", []
    def run(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["container", "ls"]:
            return subprocess.CompletedProcess(arguments, 0, name, "")
        return subprocess.CompletedProcess(arguments, 0, json.dumps({drill.OWNERSHIP_LABEL: "other-owner"}), "")
    monkeypatch.setattr(drill.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="ownership changed"):
        drill._cleanup_container(name)
    assert not any(arguments[1] in {"rm", "stop"} for arguments in calls)


@pytest.mark.parametrize("failure", ["remove", "absence_query", "still_present"])
def test_failed_cleanup_is_not_reported_as_success(drill, monkeypatch, failure):
    name = "knowledge-oidc-pg-aaaaaaaaaaaaaaaa"
    queries = []
    def run(arguments, **_kwargs):
        if arguments[1:3] == ["container", "ls"]:
            queries.append(arguments)
            if failure == "absence_query":
                return subprocess.CompletedProcess(arguments, 1, "", "private fixture diagnostic")
            return subprocess.CompletedProcess(arguments, 0,
                                               name if len(queries) == 1 or failure == "still_present" else "", "")
        if arguments[1] == "inspect":
            return subprocess.CompletedProcess(arguments, 0, json.dumps({drill.OWNERSHIP_LABEL: name}), "")
        return subprocess.CompletedProcess(arguments, 1 if failure == "remove" else 0, "", "private fixture diagnostic")
    monkeypatch.setattr(drill.subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        drill._cleanup_container(name)
    assert "private fixture diagnostic" not in str(error.value)


def test_verified_absence_does_not_try_to_remove_any_container(drill, monkeypatch):
    calls = []
    def run(arguments, **_kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")
    monkeypatch.setattr(drill.subprocess, "run", run)
    drill._cleanup_container("knowledge-oidc-pg-aaaaaaaaaaaaaaaa")
    assert len(calls) == 1 and calls[0][1:3] == ["container", "ls"]


def test_cleanup_attempts_other_owned_target_even_when_first_refuses(drill, monkeypatch):
    calls = []
    def cleanup(name):
        calls.append(name)
        if name == "owned-idp":
            raise RuntimeError("ownership changed")
    monkeypatch.setattr(drill, "_cleanup_container", cleanup)
    with pytest.raises(RuntimeError, match="could not be verified"):
        drill._cleanup_targets(["owned-pg", "owned-idp"])
    assert calls == ["owned-idp", "owned-pg"]


def test_main_never_emits_passed_receipt_on_cleanup_failure(drill, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(drill, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["drill_oidc_keycloak.py"])
    monkeypatch.setattr(drill.logging, "disable", lambda _level: None)
    def fixture(path):
        (path / "data").mkdir()
        (path / "data" / "orders.csv").write_text("order_id\nfixture\n", encoding="utf-8")
        return {}
    monkeypatch.setattr(drill, "_fixture", fixture)
    monkeypatch.setattr(drill, "_docker", lambda *_args: "owned-fixture")
    monkeypatch.setattr(drill, "_wait_ready", lambda *_args: None)
    monkeypatch.setattr(drill, "_run_checks", lambda *_args: {"result": "passed", "schema_revision": "0013_autonomy"})
    def cleanup(_names):
        raise RuntimeError("private fixture diagnostic must never be printed")
    monkeypatch.setattr(drill, "_cleanup_targets", cleanup)
    assert drill.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"result": "failed", "error_code": "cleanup_failed", "stage": "cleanup"}
    assert captured.err == ""
    assert list((tmp_path / ".local").glob("recovery/*.json")) == []
    assert list((tmp_path / ".local").glob("oidc-drill-*")) == []


def test_run_checks_pins_disposable_target_before_platform_import_or_source_access(drill, tmp_path, monkeypatch):
    environment = {"DATABASE_URL_FILE": "/unrelated/operator-database", "KNOWLEDGE_SOURCE_POLICY_FILE": "/unrelated/policy",
                   "KNOWLEDGE_OIDC_CLIENT_SECRET_FILE": "/unrelated/credential"}
    monkeypatch.setattr(drill.os, "environ", environment)
    observed = []
    def store(**arguments):
        observed.append(arguments)
        raise RuntimeError("stop before any I/O")
    monkeypatch.setattr(drill, "KnowledgeStore", store)
    with pytest.raises(RuntimeError, match="stop before"):
        drill._run_checks(tmp_path, "https://localhost:18443/realms/fixture", 15544,
                          {"pg_password": OFFLINE_VALUE, "web_secret": OFFLINE_VALUE, "session_key": OFFLINE_VALUE})
    assert len(observed) == 1 and observed[0]["initialize_schema"] is False
    assert observed[0]["database_url"].endswith("@127.0.0.1:15544/knowledge_drill")
    assert environment["DATABASE_URL"] == observed[0]["database_url"]
    assert not {"DATABASE_URL_FILE", "KNOWLEDGE_SOURCE_POLICY_FILE", "KNOWLEDGE_OIDC_CLIENT_SECRET_FILE"} & environment.keys()
    assert environment["SSL_CERT_FILE"] == str(tmp_path / "tls.crt")


def test_equal_ports_refused_before_creating_disposable_fixture(drill, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["drill_oidc_keycloak.py", "--idp-port", "15544", "--postgres-port", "15544"])
    def forbidden(*_args):
        raise AssertionError("Invalid ports attempted fixture creation")
    monkeypatch.setattr(drill, "_fixture", forbidden)
    with pytest.raises(SystemExit) as error:
        drill.main()
    assert error.value.code == 2


@pytest.mark.parametrize("operation,image,expected_timeout,expected_stage", [
    ("pull", "postgres:18.6-alpine", 180, "image_pull_postgres"),
    ("pull", "quay.io/keycloak/keycloak:26.7.5", 180, "image_pull_keycloak"),
    ("run", "postgres:18.6-alpine", 45, "launch_postgres"),
    ("run", "quay.io/keycloak/keycloak:26.7.5", 45, "launch_keycloak"),
])
@pytest.mark.parametrize("failure", ["timeout", "failed"])
def test_cold_pull_and_launch_have_separate_bounded_safe_diagnostics(
    drill, monkeypatch, operation, image, expected_timeout, expected_stage, failure,
):
    def run(arguments, **options):
        assert options["timeout"] == expected_timeout
        if failure == "timeout":
            raise subprocess.TimeoutExpired(arguments, expected_timeout, stderr=b"private driver fixture diagnostic")
        return subprocess.CompletedProcess(arguments, 1, "", "private driver fixture diagnostic")
    monkeypatch.setattr(drill.subprocess, "run", run)
    with pytest.raises(drill.DrillFailure) as error:
        drill._docker(operation, image)
    prefix = "image_pull" if operation == "pull" else "docker_launch"
    assert error.value.code == prefix + "_" + failure
    assert error.value.stage == expected_stage
    assert "private driver fixture diagnostic" not in str(error.value)


def test_pull_helper_uses_only_exact_two_known_images(drill, monkeypatch):
    calls = []
    monkeypatch.setattr(drill, "_docker", lambda *arguments: calls.append(arguments))
    drill._pull_known_images()
    assert calls == [("pull", drill.POSTGRES_IMAGE), ("pull", drill.KEYCLOAK_IMAGE)]


def test_arbitrary_pull_target_refused_before_docker_invocation(drill, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Unregistered image was passed to Docker")
    monkeypatch.setattr(drill.subprocess, "run", forbidden)
    with pytest.raises(drill.DrillFailure) as error:
        drill._docker("pull", "private.example.test/customer-image")
    assert error.value.code == "docker_operation_unsupported"


def test_launch_never_spends_its_budget_on_an_implicit_image_pull(drill, tmp_path, monkeypatch):
    calls = []
    (tmp_path / "data").mkdir()
    (tmp_path / "data/orders.csv").write_text("id\nfixture\n", encoding="utf-8")
    monkeypatch.setattr(drill, "_docker", lambda *arguments: calls.append(arguments))
    monkeypatch.setattr(drill, "_wait_ready", lambda *_args: None)
    monkeypatch.setattr(drill, "_run_checks", lambda *_args: {"result": "passed"})
    monkeypatch.setattr(drill, "_cleanup_targets", lambda *_args: None)
    drill._run_disposable(tmp_path, "https://localhost:18443/realms/fixture", 15544, 18443,
                          "owned-pg", "owned-idp", {})
    assert len(calls) == 2
    assert all(arguments[0] == "run" and "--pull=never" in arguments for arguments in calls)


def test_unknown_driver_message_is_redacted_to_constant_phase_only(drill):
    failure = RuntimeError("private driver message with token or database URL")
    assert drill._safe_failure(failure, "registry_checks") == {
        "result": "failed", "error_code": "unclassified_error", "stage": "registry_checks",
    }
    assert drill._safe_failure(failure, "private driver message") == {
        "result": "failed", "error_code": "unclassified_error", "stage": "bootstrap",
    }


def test_failure_class_rejects_nonconstant_output_codes_and_phases(drill):
    with pytest.raises(ValueError, match="Unsupported safe"):
        drill.DrillFailure("private error text", "bootstrap")
    with pytest.raises(ValueError, match="Unsupported safe"):
        drill.DrillFailure("image_pull_failed", "private stage text")


def test_cold_pull_failure_prints_safe_code_without_launching_or_creating_credentials(
    drill, tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(drill, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["drill_oidc_keycloak.py"])
    monkeypatch.setattr(drill.logging, "disable", lambda _level: None)
    def fail_pull():
        raise drill.DrillFailure("image_pull_timeout", "image_pull_keycloak")
    monkeypatch.setattr(drill, "_pull_known_images", fail_pull)
    def forbidden(*_args):
        raise AssertionError("Failed cold pull attempted fixture or launch")
    monkeypatch.setattr(drill, "_fixture", forbidden)
    monkeypatch.setattr(drill, "_run_disposable", forbidden)
    assert drill.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"result": "failed", "error_code": "image_pull_timeout", "stage": "image_pull_keycloak"}
    assert captured.err == ""
    assert list((tmp_path / ".local").glob("recovery/*.json")) == []


def test_cleanup_failure_preserves_safe_original_launch_cause(drill, tmp_path, monkeypatch):
    def launch(*_args):
        raise drill.DrillFailure("docker_launch_timeout", "launch_postgres")
    def cleanup(_names):
        raise drill.DrillFailure("cleanup_ownership_mismatch", "cleanup_postgres")
    monkeypatch.setattr(drill, "_docker", launch)
    monkeypatch.setattr(drill, "_cleanup_targets", cleanup)
    with pytest.raises(drill.DrillFailure) as error:
        drill._run_disposable(tmp_path, "https://localhost:18443/realms/fixture", 15544, 18443,
                              "owned-pg", "owned-idp", {})
    assert drill._safe_failure(error.value, "bootstrap") == {
        "result": "failed", "error_code": "cleanup_ownership_mismatch", "stage": "cleanup_postgres",
        "previous_failure": {"error_code": "docker_launch_timeout", "stage": "launch_postgres"},
    }


def test_readiness_exhaustion_has_its_own_safe_code(drill, tmp_path):
    with pytest.raises(drill.DrillFailure) as error:
        drill._wait_ready("https://localhost:18443/realms/fixture", tmp_path / "tls.crt", "owned-pg", seconds=0)
    assert error.value.code == "readiness_timeout" and error.value.stage == "readiness"
