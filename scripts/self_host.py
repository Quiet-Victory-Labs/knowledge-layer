#!/usr/bin/env python3
"""Prepare and operate a portable, private Docker Compose installation.

Uses Python's standard library only. Initialization never starts Docker or
provisions infrastructure. Installation secrets and durable data live outside
the checkout. Stop preserves them; only explicit remove --purge deletes them.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
# This shared helper imports only the standard library, including under -S.
from knowledge_layer.origin import normalize_origin

KINDS = {"local_files", "postgres", "sql_catalog", "iceberg", "object_store", "google_drive", "external_adapter"}
OIDC_KEYS = {
    "issuer": "KNOWLEDGE_OIDC_ISSUER", "client_id": "KNOWLEDGE_OIDC_CLIENT_ID",
    "audience": "KNOWLEDGE_OIDC_AUDIENCE", "jwks_uri": "KNOWLEDGE_OIDC_JWKS_URI",
    "allowed_group": "KNOWLEDGE_OIDC_ALLOWED_GROUP", "steward_group": "KNOWLEDGE_OIDC_STEWARD_GROUP",
}
MANIFEST = "installation.json"
RUNTIME_IMAGES = "runtime-images.json"


class OperationError(ValueError):
    """A fixed, credential-free message safe for the operator's terminal."""


def default_install() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "knowledge-layer"


def private_read(path: Path, *, limit: int = 1024 * 1024) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid not in {os.getuid(), 0} and os.geteuid() != 0 or info.st_size > limit):
            raise ValueError("protected_file_required")
        contents = handle.read(limit + 1)
        if len(contents) > limit:
            raise ValueError("protected_file_too_large")
        return contents


def external_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    if (".." in path.parts or any(char in str(path) for char in "\r\n\0$")
            or path == Path("/") or path.is_relative_to(ROOT)):
        raise ValueError("external_installation_required")
    for parent in (path, *path.parents):
        if parent.is_symlink() or (parent / ".git").exists():
            raise ValueError("external_nonsymlink_installation_required")
    return path


def origin(value: str, *, oidc: bool = False) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 or char in "$\\%" for char in value):
        raise ValueError("public_origin_required")
    value = normalize_origin(value)
    if oidc and urlsplit(value).scheme != "https":
        raise ValueError("oidc_https_required")
    return value


def require_public_binding(public_url: str, port: int) -> None:
    parsed = urlsplit(public_url)
    if parsed.scheme == "http":
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("portable_http_requires_ipv4_loopback")
        if (parsed.port or 80) != port:
            raise ValueError("loopback_public_port_must_match")


def write_private(path: Path, contents: bytes, uid: int, gid: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())
        if os.geteuid() == 0:
            os.fchown(handle.fileno(), uid, gid)


def replace_private(path: Path, contents: bytes, uid: int, gid: int) -> None:
    """Atomically publish a private receipt; a failed write keeps the old one."""
    temporary = path.with_name("." + path.name + "-" + secrets.token_hex(8))
    write_private(temporary, contents, uid, gid)
    os.replace(temporary, path)


def compose_config(config: dict) -> dict:
    uid = f"{config['uid']}:{config['gid']}"
    image = "knowledge-layer-local:" + config["project"]
    logs = {"driver": "local", "options": {"max-size": "10m", "max-file": "3"}}
    common = {
        "image": image, "pull_policy": "never", "user": uid, "read_only": True,
        "tmpfs": ["/tmp:size=64m,mode=1777"], "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"], "pids_limit": 128,
        "mem_limit": "512m", "cpus": 1.0, "logging": logs,
    }
    registry = {"DATABASE_URL_FILE": "/run/secrets/database_url", "KNOWLEDGE_AUTH_MODE": config["auth"],
                "KNOWLEDGE_ENABLED_KINDS": ",".join(config["enabled_kinds"]),
                "KNOWLEDGE_FIXTURE_ROOT": "/data/sources"}
    policy_mounts = []
    if config["source_policy"]:
        registry["KNOWLEDGE_SOURCE_POLICY_FILE"] = "/run/secrets/source_policy"
        policy_mounts = ["./secrets/source_policy:/run/secrets/source_policy:ro"]
    api_env = {**registry, "KNOWLEDGE_PUBLIC_URL": config["public_url"],
               "KNOWLEDGE_AGENT_SOURCE_MANAGEMENT": "0", "KNOWLEDGE_AGENT_AUTONOMY": "0",
               "KNOWLEDGE_AGENT_ALLOWED_KINDS": ""}
    api_mounts = ["./secrets/database_url:/run/secrets/database_url:ro", *policy_mounts,
                  "./sources:/data/sources:ro"]
    if config["auth"] == "local":
        api_env.update({"KNOWLEDGE_LOCAL_AUTH_FILE": "/state/auth/local-auth.json",
                        "KNOWLEDGE_LOCAL_BOOTSTRAP_TOKEN_FILE": "/run/secrets/bootstrap_token",
                        "KNOWLEDGE_LOCAL_SESSION_KEY_FILE": "/run/secrets/session_key",
                        "KNOWLEDGE_AGENT_TOKEN_FILE": "/run/secrets/agent_token"})
        api_mounts += ["./state/auth:/state/auth", "./secrets/bootstrap_token:/run/secrets/bootstrap_token:ro",
                       "./secrets/session_key:/run/secrets/session_key:ro",
                       "./secrets/agent_token:/run/secrets/agent_token:ro"]
    else:
        api_env.update(config["oidc"])
        api_env.update({"KNOWLEDGE_OIDC_CLIENT_SECRET_FILE": "/run/secrets/oidc_client_secret",
                        "KNOWLEDGE_OIDC_SESSION_KEY_FILE": "/run/secrets/session_key"})
        api_mounts += ["./secrets/oidc_client_secret:/run/secrets/oidc_client_secret:ro",
                       "./secrets/session_key:/run/secrets/session_key:ro"]
    services = {
        "postgres": {
            "image": "knowledge-layer-postgres:" + config["project"],
            "build": {"context": config["checkout"], "dockerfile": "Dockerfile.postgres"},
            "pull_policy": "never", "user": uid, "restart": "unless-stopped",
            "read_only": True, "tmpfs": ["/tmp:size=64m,mode=1777", "/var/run/postgresql:size=16m,mode=1777"],
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "mem_limit": "768m", "cpus": 1.0, "pids_limit": 128, "shm_size": "128m", "logging": logs,
            "environment": {"POSTGRES_USER": "postgres", "POSTGRES_DB": "postgres",
                            "POSTGRES_PASSWORD_FILE": "/run/secrets/bootstrap_database_password",
                            "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256"},
            "command": ["postgres", "-c", "shared_buffers=128MB", "-c", "max_connections=40"],
            "volumes": ["./postgres:/var/lib/postgresql",
                        "./secrets/bootstrap_database_password:/run/secrets/bootstrap_database_password:ro",
                        "./secrets/database_password:/run/secrets/application_password:ro",
                        "./init.sql:/docker-entrypoint-initdb.d/10-knowledge-layer.sql:ro"],
            # initdb's temporary server listens only on its Unix socket. TCP
            # becomes ready after every init script has completed successfully.
            "healthcheck": {"test": ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U postgres -d knowledge_layer"],
                            "interval": "5s", "timeout": "3s", "retries": 30},
        },
        "migrate": {**common, "restart": "no", "mem_limit": "384m",
                    "command": ["python", "-m", "knowledge_layer.migrate", "upgrade"],
                    "environment": {"DATABASE_URL_FILE": "/run/secrets/database_url"},
                    "volumes": ["./secrets/database_url:/run/secrets/database_url:ro"],
                    "depends_on": {"postgres": {"condition": "service_healthy"}}},
        "api": {**common, "build": {"context": config["checkout"], "dockerfile": "Dockerfile"},
                "restart": "unless-stopped", "environment": api_env, "volumes": api_mounts,
                "ports": [f"127.0.0.1:{config['port']}:8000"],
                "depends_on": {"migrate": {"condition": "service_completed_successfully"}},
                "healthcheck": {"test": ["CMD", "python", "-c",
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=2)"],
                    "interval": "5s", "timeout": "3s", "retries": 24, "start_period": "5s"}},
        "worker": {**common, "restart": "unless-stopped", "mem_limit": "768m",
                   "command": ["python", "-m", "knowledge_layer.worker"], "environment": registry,
                   "volumes": ["./secrets/database_url:/run/secrets/database_url:ro", *policy_mounts,
                               "./sources:/data/sources:ro"],
                   "depends_on": {"api": {"condition": "service_healthy"}}},
    }
    return {"name": config["project"], "services": services}


def initialize(path: Path, *, public_url=None, auth="local", port=4173,
               enabled_kinds=None, source_policy=None, oidc_settings=None, client_secret=None,
               uid=None, gid=None, examples=False) -> dict:
    path = external_path(path)
    if path.exists():
        raise ValueError("installation_already_exists")
    uid = uid if uid is not None else (10001 if os.geteuid() == 0 else os.getuid())
    gid = gid if gid is not None else (10001 if os.geteuid() == 0 else os.getgid())
    if (uid < 1 or gid < 1 or os.geteuid() != 0 and (uid != os.getuid() or gid != os.getgid())
            or auth not in {"local", "oidc"} or not 1 <= port <= 65535):
        raise ValueError("invalid_runtime_configuration")
    public_url = origin(public_url or f"http://127.0.0.1:{port}", oidc=auth == "oidc")
    require_public_binding(public_url, port)
    oidc = {}
    if auth == "oidc":
        if not oidc_settings or not client_secret or not source_policy:
            raise ValueError("oidc_protected_inputs_required")
        settings = json.loads(private_read(oidc_settings, limit=65536))
        if not isinstance(settings, dict) or not OIDC_KEYS.keys() <= settings.keys():
            raise ValueError("oidc_settings_required")
        for name, key in OIDC_KEYS.items():
            value = settings[name]
            if (not isinstance(value, str) or not value.strip() or len(value) > 512
                    or any(ord(char) < 32 or char == "$" for char in value)):
                raise ValueError("oidc_settings_invalid")
            if name in {"issuer", "jwks_uri"}:
                parsed = urlsplit(value)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                    raise ValueError("oidc_https_required")
            oidc[key] = value
        if settings["allowed_group"] == settings["steward_group"]:
            raise ValueError("oidc_distinct_groups_required")
        if enabled_kinds is None:
            enabled_kinds = settings.get("enabled_kinds", ["local_files"])
        client_bytes = private_read(client_secret, limit=4096)
        if not client_bytes.strip():
            raise ValueError("oidc_client_secret_required")
    else:
        if oidc_settings or client_secret:
            raise ValueError("unexpected_oidc_inputs")
        client_bytes = None
    kinds = list(enabled_kinds if enabled_kinds is not None else ["local_files"])
    if (not kinds or len(kinds) != len(set(kinds)) or any(kind not in KINDS for kind in kinds)
            or kinds != ["local_files"] and source_policy is None):
        raise ValueError("explicit_source_policy_required")
    policy_bytes = private_read(source_policy, limit=65536) if source_policy else None
    if policy_bytes is not None:
        policy = json.loads(policy_bytes)
        if (not isinstance(policy, dict) or type(policy.get("version")) is not int
                or policy["version"] not in {1, 2} or not isinstance(policy.get("sources"), list)):
            raise ValueError("source_policy_invalid")
    config = {"version": 1, "project": "knowledge-layer-" + secrets.token_hex(6), "uid": uid, "gid": gid,
              "checkout": str(ROOT), "auth": auth, "public_url": public_url, "port": port,
              "enabled_kinds": kinds, "source_policy": bool(policy_bytes), "oidc": oidc}
    previous_umask = os.umask(0o077)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for relative in ("", "secrets", "state", "state/auth", "sources", "source-secrets", "postgres"):
            directory = path / relative
            directory.mkdir(mode=0o700)
            if os.geteuid() == 0:
                os.chown(directory, uid, gid)
        password = secrets.token_hex(32)
        values = {
            "database_password": password.encode(), "bootstrap_database_password": secrets.token_hex(32).encode(),
            "database_url": f"postgresql+psycopg://knowledge_layer:{password}@postgres:5432/knowledge_layer".encode(),
            "session_key": secrets.token_hex(32).encode(),
        }
        if auth == "local":
            values.update(bootstrap_token=secrets.token_hex(32).encode(), agent_token=secrets.token_hex(32).encode())
        else:
            values["oidc_client_secret"] = client_bytes
        if policy_bytes is not None:
            values["source_policy"] = policy_bytes
        for name, contents in values.items():
            write_private(path / "secrets" / name, contents, uid, gid)
        if examples:
            for name in ("northstar", "juniper"):
                shutil.copytree(ROOT / "data/connectable" / name, path / "sources" / name, symlinks=True)
            for child in (path / "sources").rglob("*"):
                if child.is_symlink():
                    raise ValueError("example_symlink_refused")
                child.chmod(0o700 if child.is_dir() else 0o600)
                if os.geteuid() == 0:
                    os.chown(child, uid, gid)
        write_private(path / "init.sql", (ROOT / "deploy/self-host/init.sql").read_bytes(), uid, gid)
        write_private(path / "compose.yaml", (json.dumps(compose_config(config), indent=2) + "\n").encode(), uid, gid)
        # Commit the manifest last: a partial preparation cannot be mistaken for a ready install.
        write_private(path / MANIFEST, (json.dumps(config, indent=2) + "\n").encode(), uid, gid)
    finally:
        os.umask(previous_umask)
    return config


def load(path: Path) -> tuple[Path, dict]:
    path = external_path(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("private_installation_directory_required")
    config = json.loads(private_read(path / MANIFEST, limit=65536))
    if (not isinstance(config, dict) or config.get("version") != 1
            or not re.fullmatch(r"knowledge-layer-[a-f0-9]{12}", config.get("project", ""))
            or config.get("auth") not in {"local", "oidc"}):
        raise ValueError("invalid_installation_manifest")
    private_read(path / "compose.yaml")
    return path, config


def docker(args: list[str], *, timeout=30) -> subprocess.CompletedProcess:
    # Never echo subprocess diagnostics: Docker and database errors can include credentials.
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise ValueError("docker_command_failed")
    return result


def compose(path: Path, config: dict, args: list[str], *, timeout=30) -> subprocess.CompletedProcess:
    command = ["compose", "--project-name", config["project"], "--project-directory", str(path),
               "--file", str(path / "compose.yaml")]
    override = path / "worker.override.yaml"
    if override.exists() or override.is_symlink():
        private_read(override)
        command += ["--file", str(override)]
    return docker([*command, *args], timeout=timeout)


def project_container_ids(path: Path, config: dict, *, running=False) -> set[str]:
    args = ["ps", "--quiet", "--status", "running"] if running else ["ps", "--all", "--quiet"]
    output = compose(path, config, args).stdout
    if len(output) > 4096:
        raise ValueError("project_inventory_too_large")
    identifiers = set(output.split())
    if len(identifiers) > 16 or any(not re.fullmatch(r"[a-f0-9]{12,64}", item) for item in identifiers):
        raise ValueError("project_inventory_invalid")
    return identifiers


def require_free_port(port: int) -> None:
    # A recently stopped listener can leave TIME_WAIT connections. First refuse
    # a current listener (including wildcard listeners), then permit that stale
    # TCP state during the bind check. Never listen, serve, or send a payload.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.25)
            if connection.connect_ex(("127.0.0.1", port)) != errno.ECONNREFUSED:
                raise OSError("listener_or_unknown_port_state")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    except OSError:
        raise OperationError(
            f"Loopback port {port} is occupied or unavailable. Free that port, or initialize a separate "
            "installation with --install-dir DIRECTORY init --port UNUSED_PORT. No services were started."
        ) from None


def record_runtime_images(path: Path, config: dict) -> dict:
    """Record image IDs from exact-project containers, never their mutable tags."""
    identifiers = project_container_ids(path, config)
    if not identifiers:
        raise ValueError("runtime_containers_required")
    template = ('{"id":{{json .Id}},"image_id":{{json .Image}},'
                '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}')
    raw = docker(["container", "inspect", "--format", template, *sorted(identifiers)]).stdout
    if len(raw) > 16384:
        raise ValueError("runtime_image_inventory_limit")
    rows, seen = {}, set()
    for line in raw.splitlines():
        row = json.loads(line)
        if (not isinstance(row, dict) or set(row) != {"id", "image_id", "project", "service"}
                or not isinstance(row["id"], str) or row["id"] not in identifiers or row["id"] in seen
                or row["project"] != config["project"] or not isinstance(row["service"], str) or row["service"] in rows
                or row["service"] not in {"api", "worker", "migrate", "postgres"}
                or not isinstance(row["image_id"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", row["image_id"])):
            raise ValueError("runtime_image_inventory_invalid")
        seen.add(row["id"])
        rows[row["service"]] = row["image_id"]
    if seen != identifiers or not {"api", "postgres"} <= rows.keys():
        raise ValueError("runtime_image_inventory_incomplete")
    if any(image != rows["api"] for service, image in rows.items() if service != "postgres"):
        raise ValueError("runtime_application_images_differ")
    result = {"version": 1, "project": config["project"], "images": {"app": rows["api"], "postgres": rows["postgres"]}}
    replace_private(path / RUNTIME_IMAGES, (json.dumps(result) + "\n").encode(), config["uid"], config["gid"])
    return result


def stop(path: Path, config: dict) -> None:
    failed_receipt = False
    if project_container_ids(path, config):
        try:
            record_runtime_images(path, config)
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
            (path / RUNTIME_IMAGES).unlink(missing_ok=True)
            failed_receipt = True
    compose(path, config, ["down"], timeout=90)
    if failed_receipt:
        raise OperationError("Stopped this installation and preserved its files. Runtime image identity could not be "
                             "recorded; complete a verified start and stop before making a cold backup.")


def backup_installation(path: Path, config: dict, output: Path) -> dict:
    if __package__:
        from . import self_host_recovery
    else:
        import self_host_recovery
    return self_host_recovery.backup_installation(path, config, output, ops=sys.modules[__name__])


def restore_installation(bundle: Path, output: Path, *, port=None, public_url=None) -> dict:
    if __package__:
        from . import self_host_recovery
    else:
        import self_host_recovery
    return self_host_recovery.restore_installation(bundle, output, port=port, public_url=public_url,
                                                   ops=sys.modules[__name__])


def start(path: Path, config: dict, *, no_build=False, wait_seconds=180, allow_upgrade=False) -> None:
    recovery_image = config.get("recovery_image_id")
    if recovery_image and not allow_upgrade:
        if not no_build:
            raise OperationError("This restored installation requires start --no-build using its recorded app image. "
                                 "After a new cold backup, use --allow-upgrade only for an operator-reviewed upgrade.")
        observed = docker(["image", "inspect", "--format", "{{.Id}}", "knowledge-layer-local:" + config["project"]]).stdout.strip()
        if observed != recovery_image:
            raise OperationError("The restored app tag does not match the recorded image. Tag the exact recorded app "
                                 "image for this installation, then retry start --no-build. No services were started.")
    try:
        previous = project_container_ids(path, config)
        running = project_container_ids(path, config, running=True)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise OperationError(
            "Docker Engine/Compose or this installation's configuration is unavailable. Run doctor and "
            "check Docker before retrying start. No services were started by this command."
        ) from None
    if not running:
        require_free_port(config["port"])
    if not no_build:
        print("Building the application from this checkout; preparing PostgreSQL and migrations.", flush=True)
        try:
            targets = ["api"] if recovery_image else ["api", "postgres"]
            compose(path, config, ["build", *targets], timeout=900)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise OperationError(
                "Application build failed. Check Docker availability, registry access and disk space, then retry "
                "start. No services were started by this command; installation files are preserved."
            ) from None
    try:
        (path / RUNTIME_IMAGES).unlink(missing_ok=True)
        compose(path, config, ["up", "--detach", "--wait", "--wait-timeout", str(wait_seconds)],
                timeout=wait_seconds + 30)
        runtime = record_runtime_images(path, config)
        if recovery_image and allow_upgrade:
            config["recovery_image_id"] = runtime["images"]["app"]
            replace_private(path / MANIFEST, (json.dumps(config, indent=2) + "\n").encode(), config["uid"], config["gid"])
    except (OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt):
        try:
            introduced = project_container_ids(path, config) - previous
            if introduced:
                docker(["container", "stop", "--time", "10", *sorted(introduced)], timeout=30)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise OperationError(
                "Startup failed and cleanup could not be confirmed. Run this installation's stop command; "
                "then doctor and inspect its private PostgreSQL/migration/API logs. Durable data is preserved."
            ) from None
        raise OperationError(
            "Startup did not become ready. Newly introduced containers for this installation were stopped; "
            "durable data and prior containers are preserved. Run doctor, inspect private "
            "PostgreSQL/migration/API logs, resolve the failure, then retry start."
        ) from None


def doctor(path: Path) -> list[str]:
    messages = []
    docker(["compose", "version"])
    docker(["info", "--format", "{{.ServerVersion}}"])
    messages.append("Docker Engine and Compose are available.")
    if path.exists():
        path, config = load(path)
        for name in ("database_url", "database_password", "bootstrap_database_password", "session_key"):
            private_read(path / "secrets" / name)
        if config["auth"] == "local":
            for name in ("bootstrap_token", "agent_token"):
                private_read(path / "secrets" / name)
            auth = path / "state/auth/local-auth.json"
            if auth.exists():
                private_read(auth)
        compose(path, config, ["config", "--quiet"])
        messages.append("Private installation files and Compose configuration are valid.")
    else:
        messages.append("Installation is not initialized; start will initialize it.")
    return messages


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--install-dir", type=Path, default=default_install())
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("init", "start"):
        sub = commands.add_parser(name)
        sub.add_argument("--public-url")
        sub.add_argument("--auth", choices=("local", "oidc"))
        sub.add_argument("--port", type=int)
        sub.add_argument("--enabled-kinds", type=lambda value: value.split(","))
        sub.add_argument("--source-policy", type=Path)
        sub.add_argument("--oidc-settings", type=Path)
        sub.add_argument("--client-secret-file", type=Path)
        sub.add_argument("--examples", action="store_true", help="Copy only bundled synthetic Northstar/Juniper examples")
        if name == "start":
            sub.add_argument("--no-build", action="store_true")
            sub.add_argument("--allow-upgrade", action="store_true", help="Explicitly permit an upgrade of a restored installation")
            sub.add_argument("--wait-seconds", type=int, default=180)
    for name in ("status", "doctor", "stop", "bootstrap"):
        commands.add_parser(name)
    sub = commands.add_parser("remove")
    sub.add_argument("--purge", action="store_true", required=True)
    sub = commands.add_parser("backup")
    sub.add_argument("--output", type=Path, required=True)
    sub = commands.add_parser("restore")
    sub.add_argument("--backup", type=Path, required=True)
    sub.add_argument("--port", type=int)
    sub.add_argument("--public-url")
    sub = commands.add_parser("agent")
    sub.add_argument("--client", choices=("codex", "claude", "cursor"), required=True)
    sub.add_argument("--project", type=Path, default=Path.cwd())
    sub.add_argument("--replace", action="store_true")
    return result


def main(argv=None) -> int:
    arguments = parser().parse_args(argv)
    try:
        path = external_path(arguments.install_dir)
        if arguments.command == "restore":
            report = restore_installation(arguments.backup, path, port=arguments.port, public_url=arguments.public_url)
            print(json.dumps(report, indent=2))
            return 0
        if arguments.command == "start" and not 10 <= arguments.wait_seconds <= 600:
            raise OperationError("Use --wait-seconds between 10 and 600; no services were started.")
        prepared = False
        if arguments.command in {"init", "start"} and not path.exists():
            initialize(path, public_url=arguments.public_url, auth=arguments.auth or "local",
                       port=arguments.port if arguments.port is not None else 4173,
                       enabled_kinds=arguments.enabled_kinds, source_policy=arguments.source_policy,
                       oidc_settings=arguments.oidc_settings, client_secret=arguments.client_secret_file,
                       examples=arguments.examples)
            prepared = True
        elif arguments.command == "init":
            raise ValueError("installation_already_exists")
        if arguments.command == "doctor":
            print("\n".join(doctor(path)))
            return 0
        path, config = load(path)
        if arguments.command == "start" and not prepared:
            for field, value in (("auth", arguments.auth), ("public_url", arguments.public_url),
                                 ("port", arguments.port), ("enabled_kinds", arguments.enabled_kinds)):
                if field == "public_url" and value is not None:
                    value = origin(value, oidc=config["auth"] == "oidc")
                if value is not None and value != config[field]:
                    raise ValueError("existing_installation_settings_must_match")
            if arguments.source_policy or arguments.oidc_settings or arguments.client_secret_file:
                raise ValueError("existing_protected_inputs_must_not_be_reinitialized")
        if arguments.command == "init":
            print(f"Installation prepared at {path}. Run ./knowledge-layer start.")
        elif arguments.command == "start":
            start(path, config, no_build=arguments.no_build, wait_seconds=arguments.wait_seconds,
                  allow_upgrade=arguments.allow_upgrade)
            print(f"Knowledge Layer is ready at {config['public_url']}.")
            if config["auth"] == "local":
                if (path / "state/auth/local-auth.json").exists():
                    print("Open the installation and sign in with your existing owner account.")
                else:
                    print(f"Open the setup page and paste the token from {path / 'secrets/bootstrap_token'}.")
                    print("Run ./knowledge-layer bootstrap to display it explicitly.")
        elif arguments.command == "status":
            result = compose(path, config, ["ps", "--format", "json"])
            if len(result.stdout) > 65536:
                raise ValueError("status_payload_too_large")
            raw = result.stdout.strip()
            rows = json.loads(raw) if raw.startswith("[") else [json.loads(row) for row in raw.splitlines()]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("status_payload_invalid")
            safe = [{key: row[key] for key in ("Service", "State", "Health", "ExitCode") if key in row} for row in rows]
            print(json.dumps(safe, indent=2) if safe else "No containers are running for this installation.")
        elif arguments.command == "stop":
            stop(path, config)
            print("Stopped this installation. Database, owner login and secrets are preserved.")
        elif arguments.command == "backup":
            result = backup_installation(path, config, arguments.output)
            safe = {key: value for key, value in result.items() if key != "receipt"}
            safe.update({key: result["receipt"][key] for key in ("images", "database", "platform", "checkout")})
            print(json.dumps(safe, indent=2))
        elif arguments.command == "remove":
            compose(path, config, ["down", "--volumes"], timeout=90)
            shutil.rmtree(path)
            print("Removed this installation and its durable data.")
        elif arguments.command == "bootstrap":
            if config["auth"] != "local":
                raise ValueError("local_bootstrap_only")
            print(private_read(path / "secrets/bootstrap_token", limit=4096).decode())
        elif arguments.command == "agent":
            if __package__:
                from .configure_agent import configure
            else:
                from configure_agent import configure
            target = configure(client=arguments.client, project=arguments.project,
                               url=config["public_url"] + "/agent/mcp/", replace=arguments.replace)
            print(f"Configured {target}; confirm server trust in your client.")
            if arguments.client == "codex":
                print("Codex loads project .codex/config.toml only after you trust that project."
                      " From the trusted project, verify with: codex mcp list")
            if config["auth"] == "local":
                print("Expose KNOWLEDGE_AGENT_TOKEN to your client from the private file:")
                # A file reference only: credential bytes never enter configuration or CLI output.
                print("export KNOWLEDGE_AGENT_TOKEN=\"$(cat " + shlex.quote(str(path / "secrets/agent_token")) + ")\"")
            else:
                print("Expose an IdP-issued KNOWLEDGE_AGENT_TOKEN with the configured audience to your client.")
    except OperationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, tarfile.TarError, subprocess.TimeoutExpired):
        print("Self-host operation refused. Check private installation files, Docker readiness and the self-host guide.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
