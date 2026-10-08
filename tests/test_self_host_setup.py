"""Offline installer authority, secret persistence and Compose startup checks."""
from __future__ import annotations

import json
import stat
import subprocess
import sys

import pytest

from scripts import self_host


def protected(path, contents):
    path.write_text(contents)
    path.chmod(0o600)
    return path


def install(tmp_path, **kwargs):
    path = tmp_path / "installation"
    return path, self_host.initialize(path, **kwargs)


def test_init_uses_only_standard_library_and_never_starts_docker(tmp_path):
    path = tmp_path / "private"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(self_host.ROOT / "scripts/self_host.py"),
         "--install-dir", str(path), "init"], capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0
    assert "Installation prepared" in result.stdout
    assert (path / self_host.MANIFEST).exists()
    for child in (path / "secrets").iterdir():
        assert child.read_text() not in result.stdout + result.stderr


def test_private_files_durable_secrets_and_missing_owner_claim(tmp_path):
    path, config = install(tmp_path)
    assert config["auth"] == "local" and config["enabled_kinds"] == ["local_files"]
    assert config["uid"] > 0 and config["gid"] > 0
    for child in [path, *path.rglob("*")]:
        assert stat.S_IMODE(child.stat().st_mode) == (0o700 if child.is_dir() else 0o600)
    assert not (path / "state/auth/local-auth.json").exists()
    assert not list((path / "sources").iterdir())
    names = ["database_password", "bootstrap_database_password", "bootstrap_token", "session_key", "agent_token"]
    values = [(path / "secrets" / name).read_bytes() for name in names]
    assert len(set(values)) == len(names) and all(len(value) == 64 for value in values)
    assert values[0] in (path / "secrets/database_url").read_bytes()
    before = {child.name: child.read_bytes() for child in (path / "secrets").iterdir()}
    with pytest.raises(ValueError, match="already_exists"):
        self_host.initialize(path)
    assert before == {child.name: child.read_bytes() for child in (path / "secrets").iterdir()}
    assert self_host.load(path)[1] == config


def test_compose_runtime_authority_and_startup_order(tmp_path):
    path, config = install(tmp_path)
    compose = json.loads((path / "compose.yaml").read_text())
    services = compose["services"]
    assert set(services) == {"postgres", "migrate", "api", "worker"}
    assert services["api"]["build"]["context"] == str(self_host.ROOT)
    assert services["postgres"]["build"] == {"context": str(self_host.ROOT), "dockerfile": "Dockerfile.postgres"}
    assert services["postgres"]["image"] == "knowledge-layer-postgres:" + config["project"]
    assert services["postgres"]["pull_policy"] == "never"
    assert all("ghcr.io/quiet-victory" not in service["image"] for service in services.values())
    assert services["api"]["ports"] == ["127.0.0.1:4173:8000"]
    assert "ports" not in services["postgres"]
    assert services["migrate"]["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert services["api"]["depends_on"] == {"migrate": {"condition": "service_completed_successfully"}}
    assert services["worker"]["depends_on"] == {"api": {"condition": "service_healthy"}}
    assert "-h 127.0.0.1" in services["postgres"]["healthcheck"]["test"][-1]
    for service in services.values():
        assert service["user"] == f"{config['uid']}:{config['gid']}"
        assert service["read_only"] and service["cap_drop"] == ["ALL"]
        assert service["mem_limit"] and service["cpus"] and service["pids_limit"]
        assert service["logging"]["options"] == {"max-size": "10m", "max-file": "3"}
    for name in ("api", "migrate", "worker"):
        mounts = services[name]["volumes"]
        assert not any("bootstrap_database_password" in mount or "database_password:" in mount for mount in mounts)
        assert services[name]["environment"]["DATABASE_URL_FILE"] == "/run/secrets/database_url"
    assert services["api"]["environment"]["KNOWLEDGE_AGENT_SOURCE_MANAGEMENT"] == "0"
    assert services["api"]["environment"]["KNOWLEDGE_LOCAL_AUTH_FILE"] == "/state/auth/local-auth.json"
    assert "./state/auth:/state/auth" in services["api"]["volumes"]
    assert "./sources:/data/sources:ro" in services["api"]["volumes"]
    for mount in services["worker"]["volumes"]:
        assert mount.endswith(":ro")
        assert not any(secret in mount for secret in ("auth", "bootstrap_token", "session_key", "agent_token"))
    sql = (path / "init.sql").read_text()
    for attribute in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOREPLICATION", "NOBYPASSRLS"):
        assert attribute in sql
    assert "pg_read_file('/run/secrets/application_password')" in sql and "\\gexec" in sql
    assert "CREATE DATABASE knowledge_layer OWNER knowledge_layer" in sql
    assert "REVOKE ALL ON DATABASE knowledge_layer FROM PUBLIC" in sql
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in sql
    assert "ALTER SCHEMA public OWNER TO knowledge_layer" in sql


@pytest.mark.parametrize("kwargs", [
    {"public_url": "http://remote.example:4173"}, {"public_url": "https://user:secret@example.org"},
    {"public_url": "https://example.org/path"}, {"public_url": "https://example.org/?secret=abc"},
    {"public_url": "https://$HOST"}, {"port": 0}, {"port": 65536},
    {"public_url": "http://127.0.0.1:4444"}, {"enabled_kinds": []},
    {"enabled_kinds": ["local_files", "postgres"]}, {"enabled_kinds": ["unsupported"]},
    {"uid": 0}, {"gid": 0}, {"auth": "demo"},
])
def test_invalid_or_unapproved_configuration_has_no_filesystem_effect(tmp_path, kwargs):
    path = tmp_path / "installation"
    with pytest.raises(ValueError):
        self_host.initialize(path, **kwargs)
    assert not path.exists()


def test_repository_symlink_and_unprotected_input_refused(tmp_path):
    with pytest.raises(ValueError, match="external"):
        self_host.initialize(self_host.ROOT / ".self-host-test")
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="nonsymlink"):
        self_host.initialize(linked / "installation")
    policy = tmp_path / "policy"
    policy.write_text('{"version":1,"sources":[]}')
    policy.chmod(0o644)
    with pytest.raises(ValueError, match="protected_file"):
        self_host.initialize(tmp_path / "installation", source_policy=policy)
    policy.chmod(0o600)
    symlink = tmp_path / "policy-symlink"
    symlink.symlink_to(policy)
    with pytest.raises(OSError):
        self_host.initialize(tmp_path / "installation", source_policy=symlink)


def test_explicit_examples_are_only_bundled_synthetic_sources(tmp_path):
    path, _ = install(tmp_path, examples=True)
    assert {child.name for child in (path / "sources").iterdir()} == {"northstar", "juniper"}
    for child in (path / "sources").rglob("*"):
        assert not child.is_symlink()
        assert stat.S_IMODE(child.stat().st_mode) == (0o700 if child.is_dir() else 0o600)


def test_oidc_reuses_operator_settings_and_requires_protected_policy(tmp_path):
    settings = protected(tmp_path / "settings.json", json.dumps({
        "issuer": "https://identity.example/realms/company", "client_id": "knowledge-web",
        "audience": "knowledge-agents", "jwks_uri": "https://identity.example/keys",
        "allowed_group": "users", "steward_group": "stewards", "enabled_kinds": ["postgres"],
        "image": "ignored-existing-deployment-setting", "data_root": "/ignored-existing-install",
    }))
    secret = protected(tmp_path / "oidc-secret", "private-client-value")
    policy = protected(tmp_path / "policy.json", '{"version":1,"sources":[]}')
    path, config = install(tmp_path, auth="oidc", public_url="https://knowledge.example",
                           oidc_settings=settings, client_secret=secret, source_policy=policy)
    assert config["enabled_kinds"] == ["postgres"]
    services = json.loads((path / "compose.yaml").read_text())["services"]
    assert services["api"]["environment"]["KNOWLEDGE_AUTH_MODE"] == "oidc"
    assert services["api"]["environment"]["KNOWLEDGE_OIDC_ISSUER"] == "https://identity.example/realms/company"
    assert "private-client-value" not in (path / "compose.yaml").read_text()
    assert "private-client-value" not in (path / self_host.MANIFEST).read_text()
    assert (path / "secrets/oidc_client_secret").read_text() == "private-client-value"
    assert not (path / "secrets/agent_token").exists()
    assert not any("oidc_client_secret" in mount for mount in services["worker"]["volumes"])


def test_start_stop_restart_preserve_auth_secrets_and_scope_commands(tmp_path, monkeypatch, capsys):
    path, config = install(tmp_path)
    protected(path / "state/auth/local-auth.json", '{"version":1,"owner":{"subject":"local-owner"}}')
    before = {str(child.relative_to(path)): child.read_bytes() for child in path.rglob("*") if child.is_file()}
    calls = []
    def docker(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(self_host, "docker", docker)
    monkeypatch.setattr(self_host, "record_runtime_images", lambda *a: None)
    monkeypatch.setattr(self_host, "require_free_port", lambda port: None)
    for command in ("start", "stop", "start"):
        assert self_host.main(["--install-dir", str(path), command]) == 0
    assert before == {str(child.relative_to(path)): child.read_bytes() for child in path.rglob("*") if child.is_file()}
    for args, _ in calls:
        assert args[:3] == ["compose", "--project-name", config["project"]]
        assert "--file" in args and str(path / "compose.yaml") in args
        assert "--volumes" not in args and "--remove-orphans" not in args
    builds = [(args, kwargs) for args, kwargs in calls if args[-3:] == ["build", "api", "postgres"]]
    startups = [(args, kwargs) for args, kwargs in calls if "--wait-timeout" in args]
    assert len(builds) == 2
    assert len(startups) == 2 and startups[0][0][-5:] == ["up", "--detach", "--wait", "--wait-timeout", "180"]
    assert startups[0][1]["timeout"] == 210
    assert any(args[-1] == "down" for args, _ in calls)
    output = capsys.readouterr().out
    for child in (path / "secrets").iterdir():
        assert child.read_text() not in output


def test_existing_start_refuses_auth_change_without_docker(tmp_path, monkeypatch):
    path, _ = install(tmp_path)
    monkeypatch.setattr(self_host, "docker", lambda *a, **k: pytest.fail("Docker must not start"))
    assert self_host.main(["--install-dir", str(path), "start", "--auth", "oidc"]) == 2


def test_fresh_start_initializes_then_uses_bounded_readiness(tmp_path, monkeypatch):
    calls = []
    def compose(path, config, args, **kwargs):
        assert (path / self_host.MANIFEST).exists()
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(self_host, "compose", compose)
    monkeypatch.setattr(self_host, "project_container_ids", lambda *a, **k: set())
    monkeypatch.setattr(self_host, "require_free_port", lambda port: None)
    monkeypatch.setattr(self_host, "record_runtime_images", lambda *a: None)
    assert self_host.main(["--install-dir", str(tmp_path / "new"), "start", "--no-build", "--wait-seconds", "30"]) == 0
    assert calls == [(["up", "--detach", "--wait", "--wait-timeout", "30"], {"timeout": 60})]


def test_bootstrap_displays_only_when_explicitly_requested(tmp_path, capsys):
    path, _ = install(tmp_path)
    token = (path / "secrets/bootstrap_token").read_text()
    assert self_host.main(["--install-dir", str(path), "bootstrap"]) == 0
    assert capsys.readouterr().out.strip() == token


def test_agent_config_preserves_servers_and_contains_only_env_reference(tmp_path, capsys):
    path, _ = install(tmp_path)
    project = tmp_path / "customer-project"
    project.mkdir()
    protected(project / ".mcp.json", '{"mcpServers":{"existing":{"command":"existing"}}}')
    assert self_host.main(["--install-dir", str(path), "agent", "--client", "claude", "--project", str(project)]) == 0
    config = json.loads((project / ".mcp.json").read_text())
    assert config["mcpServers"]["existing"] == {"command": "existing"}
    assert config["mcpServers"]["knowledge_layer"]["headers"] == {"Authorization": "Bearer ${KNOWLEDGE_AGENT_TOKEN}"}
    output = capsys.readouterr().out
    assert str(path / "secrets/agent_token") in output
    assert (path / "secrets/agent_token").read_text() not in output + (project / ".mcp.json").read_text()


def test_status_filters_command_and_credential_diagnostics(tmp_path, monkeypatch, capsys):
    path, _ = install(tmp_path)
    payload = {"Service": "api", "State": "running", "Health": "healthy", "Command": "secret=private"}
    monkeypatch.setattr(self_host, "compose", lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(payload), ""))
    assert self_host.main(["--install-dir", str(path), "status"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"Service": "api", "State": "running", "Health": "healthy"}]


def test_explicit_purge_scopes_cleanup_to_owned_installation(tmp_path, monkeypatch):
    path, config = install(tmp_path)
    sibling = tmp_path / "unrelated"
    sibling.mkdir()
    protected(sibling / "preserve", "customer-data")
    calls = []
    def compose(directory, manifest, args, **kwargs):
        calls.append((directory, manifest["project"], args))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(self_host, "compose", compose)
    assert self_host.main(["--install-dir", str(path), "remove", "--purge"]) == 0
    assert calls == [(path, config["project"], ["down", "--volumes"])]
    assert not path.exists() and (sibling / "preserve").read_text() == "customer-data"


def test_docker_failure_is_sanitized_and_retains_installation(tmp_path, monkeypatch, capsys):
    path, _ = install(tmp_path)
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "postgresql://private:secret@host", "private diagnostic")
    monkeypatch.setattr(self_host.subprocess, "run", run)
    assert self_host.main(["--install-dir", str(path), "start", "--no-build"]) == 2
    assert path.exists()
    output = capsys.readouterr()
    assert "private" not in output.out and "secret@host" not in output.err and "private diagnostic" not in output.err


@pytest.mark.parametrize("value, expected", [
    ("https://EXAMPLE.com:443/", "https://example.com"),
    ("http://LOCALHOST:80/", "http://localhost"),
    ("https://[::1]:443/", "https://[::1]"),
    ("https://EXAMPLE.com:8443", "https://example.com:8443"),
])
def test_installer_uses_shared_canonical_origin(value, expected):
    assert self_host.origin(value) == expected


@pytest.mark.parametrize("value", [
    "https://bad_host.example", "https://-bad.example", "https://bad..example",
    "https://[fe80::1%25eth0]", "https://example.org\\evil", "https://$HOST",
    "https://example.org\x00", "https://999.999.999.999",
])
def test_malformed_dns_ip_and_interpolation_origins_refused(value):
    with pytest.raises(ValueError):
        self_host.origin(value)


def test_shared_origin_is_saved_canonically(tmp_path):
    _, config = install(tmp_path, public_url="https://EXAMPLE.com:443/")
    assert config["public_url"] == "https://example.com"


def test_occupied_port_refuses_before_build_or_start(tmp_path, monkeypatch, capsys):
    path, _ = install(tmp_path)
    monkeypatch.setattr(self_host, "project_container_ids", lambda *a, **k: set())
    monkeypatch.setattr(self_host, "compose", lambda *a, **k: pytest.fail("No container may start"))
    class Probe:
        def settimeout(self, *_):
            pass
        def connect_ex(self, *_):
            import errno
            return errno.ECONNREFUSED
        def setsockopt(self, *_):
            pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def bind(self, address):
            assert address == ("127.0.0.1", 4173)
            raise OSError("private socket diagnostic")
    monkeypatch.setattr(self_host.socket, "socket", lambda *a: Probe())
    assert self_host.main(["--install-dir", str(path), "start"]) == 2
    message = capsys.readouterr().err
    assert "port 4173" in message and "--port UNUSED_PORT" in message
    assert "private socket diagnostic" not in message and path.exists()


def test_failed_up_stops_only_exact_new_project_containers_and_preserves_data(tmp_path, monkeypatch, capsys):
    path, _ = install(tmp_path)
    owner = protected(path / "state/auth/local-auth.json", "retained-owner-record")
    existing, introduced, unrelated = "a" * 64, "b" * 64, "c" * 64
    inventories = iter([{existing}, {existing}, {existing, introduced}])
    monkeypatch.setattr(self_host, "project_container_ids", lambda *a, **k: next(inventories))
    monkeypatch.setattr(self_host, "require_free_port", lambda *a: pytest.fail("Existing installation is running"))
    def fail_up(*args, **kwargs):
        raise ValueError("postgresql://private:secret@host")
    monkeypatch.setattr(self_host, "compose", fail_up)
    calls = []
    monkeypatch.setattr(self_host, "docker", lambda args, **kwargs: calls.append((args, kwargs)))
    assert self_host.main(["--install-dir", str(path), "start", "--no-build"]) == 2
    assert calls == [(["container", "stop", "--time", "10", introduced], {"timeout": 30})]
    assert existing not in calls[0][0] and unrelated not in calls[0][0]
    assert owner.read_text() == "retained-owner-record" and (path / "secrets/database_url").exists()
    message = capsys.readouterr().err
    assert "Newly introduced containers" in message and "retry start" in message
    assert "private:secret" not in message


def test_unconfirmed_failure_cleanup_gives_manual_scoped_recovery(tmp_path, monkeypatch, capsys):
    path, _ = install(tmp_path)
    inventories = iter([set(), set()])
    def inventory(*args, **kwargs):
        try:
            return next(inventories)
        except StopIteration:
            raise ValueError("private diagnostic") from None
    monkeypatch.setattr(self_host, "project_container_ids", inventory)
    monkeypatch.setattr(self_host, "require_free_port", lambda *a: None)
    monkeypatch.setattr(self_host, "compose", lambda *a, **k: (_ for _ in ()).throw(ValueError("private")))
    assert self_host.main(["--install-dir", str(path), "start", "--no-build"]) == 2
    message = capsys.readouterr().err
    assert "cleanup could not be confirmed" in message and "installation's stop command" in message
    assert "private diagnostic" not in message and path.exists()


def test_container_inventory_cannot_redirect_cleanup_outside_project(tmp_path, monkeypatch):
    path, config = install(tmp_path)
    monkeypatch.setattr(self_host, "compose", lambda *a, **k: subprocess.CompletedProcess([], 0, "--all unrelated", ""))
    with pytest.raises(ValueError, match="inventory_invalid"):
        self_host.project_container_ids(path, config)
