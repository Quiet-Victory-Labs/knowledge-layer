"""Bounded source-only subprocesses; the coordinator retains registry authority.

Environment isolation is not an OS sandbox. Deploy source workers with only their
own secret mounts and read-only roots to isolate file and network capabilities.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any
from uuid import uuid4

from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.secrets import read_source_secret
from knowledge_layer.operator_secrets import operator_secret, read_owner_only_file

MAX_WIRE_BYTES = 32 * 1024 * 1024
_RUNTIME = frozenset({"PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"})
_FLAGS = frozenset({"KNOWLEDGE_ALLOW_INSECURE_ADAPTER_ENDPOINTS", "KNOWLEDGE_ALLOW_INSECURE_CATALOG_ENDPOINTS"})
_IMAGE = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._:/-]{0,240}@)?sha256:[a-f0-9]{64}")
_NETWORK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def container_command(name: str) -> list[str]:
    """An operator-pinned image, without host mounts, ports or daemon access."""
    binary = shutil.which("docker")
    image = os.getenv("KNOWLEDGE_CONNECTOR_IMAGE", "")
    network = os.getenv("KNOWLEDGE_CONNECTOR_NETWORK", "none")
    if not binary or not _IMAGE.fullmatch(image) or not _NETWORK.fullmatch(network) or network == "host":
        raise ConnectorError("connector_runtime_invalid", "A local digest-pinned connector image and isolated network are required")
    return [binary, "run", "--rm", "--pull=never", "--interactive", "--name", name,
            "--label", "knowledge.connector.run=" + name, "--network", network,
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
            "--user", "10001:10001", "--pids-limit", "64", "--memory", "512m",
            "--memory-swap", "512m", "--ulimit", "nofile=128:128", "--ulimit", "core=0:0",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--entrypoint", "python", image, "-I", "-m", "knowledge_layer.connector_process",
            "--environment-from-packet"]


def remove_container(command: list[str], name: str) -> None:
    """Cleanup requires verified task ownership; an unavailable daemon fails closed."""
    try:
        result = subprocess.run([command[0], "inspect", "--format",
            '{{index .Config.Labels "knowledge.connector.run"}}', name],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
        if result.returncode:
            if b"No such object" in result.stderr or b"No such container" in result.stderr:
                return
            raise ValueError
        if result.stdout.strip() != name.encode():
            raise ValueError
        subprocess.run([command[0], "rm", "--force", name], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=True)
    except (ValueError, OSError, subprocess.SubprocessError):
        raise ConnectorError("connector_cleanup_failed", "Connector container cleanup could not be verified") from None


def source_environment(kind: str, config: dict[str, Any]) -> dict[str, str]:
    """Resolve exactly one profile, not secret paths or ambient SDK credentials."""
    env = {name: value for name, value in os.environ.items() if name in _RUNTIME}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in _FLAGS:
        if name in os.environ:
            env[name] = os.environ[name]
    reference = config.get("secret_ref")
    if reference is not None:
        env[reference] = read_source_secret(reference, unavailable_code="source_credentials_unavailable")
    if kind == "iceberg":
        from knowledge_layer.connectors.iceberg import catalog_profile_reference

        reference = catalog_profile_reference(config["catalog"])
        if reference is not None:
            env[reference] = read_source_secret(reference, unavailable_code="catalog_credentials_unavailable")
            env["KNOWLEDGE_ICEBERG_PROFILES"] = json.dumps({config["catalog"]: reference})
        else:
            # Convert the legacy file configuration into one native profile. The
            # child never gets POLARIS_CLIENT_FILE or broad AWS environment keys.
            allowed = {name.strip() for name in os.getenv("KNOWLEDGE_ICEBERG_CATALOGS", "").split(",") if name.strip()}
            if config.get("catalog") not in allowed:
                raise ConnectorError("catalog_not_allowlisted", "The selected catalog is unavailable")
            try:
                credentials = json.loads(read_owner_only_file(os.environ["POLARIS_CLIENT_FILE"]))
                client_id, client_secret = credentials["client_id"], credentials["client_secret"]
                if not isinstance(client_id, str) or not client_id or not isinstance(client_secret, str) or not client_secret:
                    raise ValueError
                access, secret = os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"]
                if not access or not secret:
                    raise ValueError
            except (KeyError, ValueError, OSError):
                raise ConnectorError("catalog_credentials_unavailable", "The selected catalog credentials are unavailable") from None
            root = os.getenv("POLARIS_URL", "http://localhost:8181").rstrip("/")
            profile = {"uri": root + "/api/catalog", "warehouse": config["catalog"],
                       "credential": client_id + ":" + client_secret,
                       "oauth2-server-uri": root + "/api/catalog/v1/oauth/tokens",
                       "scope": "PRINCIPAL_ROLE:ALL", "s3.endpoint": os.getenv("S3_ENDPOINT", "http://localhost:9000"),
                       "s3.access-key-id": access, "s3.secret-access-key": secret,
                       "s3.region": "us-east-1", "s3.path-style-access": "true"}
            reference = "KNOWLEDGE_SOURCE_ISOLATED_CATALOG"
            env[reference] = json.dumps(profile)
            env["KNOWLEDGE_ICEBERG_PROFILES"] = json.dumps({config["catalog"]: reference})
    return env


def stop_process_group(process: subprocess.Popen) -> None:
    """Reap the owned subprocess and any connector-created descendants."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            continue
        if sig == signal.SIGTERM:
            # The group can outlive its leader; kill remaining descendants too.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return


def run_connector(operation: str, kind: str, config: dict[str, Any], *,
                  request: dict | None = None, timeout_seconds: float = 120) -> dict:
    if operation not in {"snapshot", "inventory_page"} or not 0 < timeout_seconds <= 300:
        raise ConnectorError("connector_isolation_invalid", "The connector operation is invalid")
    env = source_environment(kind, config)
    backend = os.getenv("KNOWLEDGE_CONNECTOR_RUNTIME", "process")
    name = "knowledge-connector-" + uuid4().hex
    if backend == "container":
        if kind == "local_files":
            raise ConnectorError("connector_mount_required", "Local files require a separately qualified source-only mount")
        command = container_command(name)
        message = {"operation": operation, "kind": kind, "config": config,
                   "request": request, "environment": env, "timeout_seconds": timeout_seconds}
        # Only the trusted Docker client sees its own connection configuration.
        # Source credentials travel on private stdin, never argv/Docker inspect.
        environment = {key: value for key, value in os.environ.items() if key in
            _RUNTIME | {"HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}}
    elif backend == "process":
        command = [sys.executable, "-I", "-m", "knowledge_layer.connector_process"]
        message = {"operation": operation, "kind": kind, "config": config, "request": request,
                   "timeout_seconds": timeout_seconds}
        environment = env
    else:
        raise ConnectorError("connector_runtime_invalid", "The configured connector runtime is unsupported")
    try:
        packet = json.dumps(message, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise ConnectorError("connector_input_invalid", "The connector input is invalid") from None
    if len(packet) > MAX_WIRE_BYTES:
        raise ConnectorError("connector_input_too_large", "The connector input exceeds its budget")
    with tempfile.TemporaryDirectory(prefix="knowledge-connector-") as directory, tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as outgoing:
        incoming.write(packet)
        incoming.seek(0)
        process = subprocess.Popen(command,
                                   cwd=directory, env=environment, stdin=incoming, stdout=outgoing,
                                   stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        started = time.monotonic()
        try:
            while process.poll() is None:
                if time.monotonic() - started >= timeout_seconds:
                    raise ConnectorError("connector_timeout", "The connector exceeded its deadline")
                if os.fstat(outgoing.fileno()).st_size > MAX_WIRE_BYTES:
                    raise ConnectorError("connector_output_too_large", "The connector exceeded its output budget")
                try:
                    process.wait(timeout=min(0.05, timeout_seconds))
                except subprocess.TimeoutExpired:
                    pass
            outgoing.seek(0)
            raw = outgoing.read(MAX_WIRE_BYTES + 1)
            if len(raw) > MAX_WIRE_BYTES:
                raise ConnectorError("connector_output_too_large", "The connector exceeded its output budget")
            if process.returncode != 0:
                raise ConnectorError("connector_isolated_failed", "The isolated connector failed")
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ConnectorError("connector_output_invalid", "The connector returned invalid output") from None
            if not isinstance(result, dict):
                raise ConnectorError("connector_output_invalid", "The connector returned invalid output")
            return result
        finally:
            stop_process_group(process)
            if backend == "container":
                remove_container(command, name)


def isolated_curator(kind: str, config: dict[str, Any]) -> dict:
    return run_connector("snapshot", kind, config)


def isolated_inventory_page(config: dict, request: dict, *, timeout_seconds: float) -> dict:
    return run_connector("inventory_page", "external_adapter", config,
                         request=request, timeout_seconds=min(timeout_seconds, 120))


def coordinator_environment(kind: str, config: dict) -> dict[str, str]:
    """The trusted refresh coordinator needs the registry, not other sources."""
    env = source_environment(kind, config)
    references = [config["secret_ref"]] if config.get("secret_ref") else []
    if kind == "iceberg" and os.getenv("KNOWLEDGE_ICEBERG_PROFILES_FILE"):
        env.pop("KNOWLEDGE_ICEBERG_PROFILES", None)
        env["KNOWLEDGE_ICEBERG_PROFILES_FILE"] = os.environ["KNOWLEDGE_ICEBERG_PROFILES_FILE"]
        from knowledge_layer.connectors.iceberg import catalog_profile_reference

        references.append(catalog_profile_reference(config["catalog"]))
    elif kind == "iceberg" and "KNOWLEDGE_ICEBERG_PROFILES" in env:
        references.extend(json.loads(env["KNOWLEDGE_ICEBERG_PROFILES"]).values())
    for reference in references:
        if os.getenv(reference + "_FILE"):
            env.pop(reference, None)
            env[reference + "_FILE"] = os.environ[reference + "_FILE"]
    for name in ("DATABASE_URL", "KNOWLEDGE_SOURCE_POLICY"):
        value = operator_secret(name)
        if value is not None:
            if os.getenv(name + "_FILE"):
                env[name + "_FILE"] = os.environ[name + "_FILE"]
            else:
                env[name] = value
    for name in ("KNOWLEDGE_AUTH_MODE", "KNOWLEDGE_ENABLED_KINDS", "KNOWLEDGE_FIXTURE_ROOT",
                 "KNOWLEDGE_WORKER_CONNECTION_IDS", "KNOWLEDGE_WORKER_TENANT",
                 "KNOWLEDGE_CONNECTOR_RUNTIME", "KNOWLEDGE_CONNECTOR_IMAGE", "KNOWLEDGE_CONNECTOR_NETWORK",
                 "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "HOME"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env
