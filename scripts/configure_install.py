"""Create a new owner-only single-organization installation, without starting it.

Secrets arrive through protected files rather than arguments or Terraform state.
The generated directory must live outside a repository. Existing installs refuse;
rotation and upgrades use the operational runbook instead of reprovisioning.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from knowledge_layer.operator_secrets import read_owner_only_file
from knowledge_layer.source_policy import SourcePolicy

KEYS = {
    "image", "postgres_image", "proxy_image", "public_url", "issuer", "client_id", "audience",
    "jwks_uri", "allowed_group", "steward_group", "enabled_kinds", "data_root",
}
DIGEST = re.compile(r"[a-z0-9][a-z0-9./_-]*@sha256:[a-f0-9]{64}")
SAFE_VALUE = re.compile(r"[A-Za-z0-9./:@_+ -]{1,512}")


def validate_settings(settings: dict) -> dict[str, str]:
    if not isinstance(settings, dict) or set(settings) != KEYS:
        raise ValueError("settings_keys")
    for name in ("image", "postgres_image", "proxy_image"):
        if not isinstance(settings[name], str) or not DIGEST.fullmatch(settings[name]):
            raise ValueError("immutable_image_required")
    for name in KEYS - {"enabled_kinds"}:
        if (not isinstance(settings[name], str) or not settings[name].strip()
                or settings[name] != settings[name].strip() or not SAFE_VALUE.fullmatch(settings[name])):
            raise ValueError("unsafe_setting")
    for name in ("public_url", "issuer", "jwks_uri"):
        url = urlsplit(settings[name])
        if (url.scheme != "https" or not url.hostname or any(char.isspace() for char in settings[name])
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                       for label in url.hostname.split(".")) or url.username or url.password
                or url.query or url.fragment or url.port not in (None, 443)):
            raise ValueError("https_required")
    public = urlsplit(settings["public_url"])
    if public.path not in ("", "/") or not re.fullmatch(r"[a-z0-9.-]+", public.hostname):
        raise ValueError("public_origin_required")
    if settings["allowed_group"] == settings["steward_group"]:
        raise ValueError("distinct_groups_required")
    kinds = settings["enabled_kinds"]
    if (not isinstance(kinds, list) or not kinds
            or any(not isinstance(kind, str) or kind not in {"postgres", "sql_catalog", "external_adapter"} for kind in kinds)
            or len(kinds) != len(set(kinds))):
        raise ValueError("qualified_kinds_required")
    data_root = Path(settings["data_root"])
    if not data_root.is_absolute() or ".." in data_root.parts or data_root == Path("/"):
        raise ValueError("absolute_data_root_required")
    mapping = {
        "image": "KNOWLEDGE_IMAGE", "postgres_image": "KNOWLEDGE_POSTGRES_IMAGE",
        "proxy_image": "KNOWLEDGE_PROXY_IMAGE", "public_url": "KNOWLEDGE_PUBLIC_URL",
        "issuer": "KNOWLEDGE_OIDC_ISSUER", "client_id": "KNOWLEDGE_OIDC_CLIENT_ID",
        "audience": "KNOWLEDGE_OIDC_AUDIENCE", "jwks_uri": "KNOWLEDGE_OIDC_JWKS_URI",
        "allowed_group": "KNOWLEDGE_OIDC_ALLOWED_GROUP",
        "steward_group": "KNOWLEDGE_OIDC_STEWARD_GROUP", "data_root": "KNOWLEDGE_DATA_ROOT",
    }
    return {**{value: settings[key] for key, value in mapping.items()},
            "KNOWLEDGE_HOST": public.hostname, "KNOWLEDGE_ENABLED_KINDS": ",".join(kinds)}


def _write(path: Path, contents: bytes, uid: int, gid: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(contents)
        if os.geteuid() == 0:
            os.fchown(stream.fileno(), uid, gid)


def configure(settings_path: Path, client_secret_path: Path, policy_path: Path,
              output: Path, uid: int, gid: int) -> None:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    env = validate_settings(settings)
    client_secret = read_owner_only_file(str(client_secret_path))
    if not client_secret.strip() or len(client_secret) > 4096:
        raise ValueError("client_secret_required")
    policy = read_owner_only_file(str(policy_path))
    SourcePolicy.from_json(policy.decode("utf-8"))
    output = output.absolute()
    if (output.exists() or output.is_symlink() or not output.parent.is_dir()
            or output.parent.resolve() != output.parent or output.is_relative_to(ROOT)):
        raise ValueError("new_external_directory_required")
    if uid < 1 or gid < 1 or (os.geteuid() != 0 and (uid != os.getuid() or gid != os.getgid())):
        raise ValueError("nonroot_secret_owner_required")
    # Validate all inputs before creating any files. Partial output stays private
    # on failure, preserving generated secrets for operator inspection.
    output.mkdir(mode=0o700)
    secret_root = output / "secrets"
    secret_root.mkdir(mode=0o700)
    password = secrets.token_hex(32)
    for name, value in {
        "database_password": password.encode(),
        "database_url": f"postgresql+psycopg://knowledge_layer:{password}@postgres:5432/knowledge_layer".encode(),
        "oidc_client_secret": client_secret,
        "session_key": secrets.token_hex(32).encode(), "source_policy": policy,
    }.items():
        _write(secret_root / name, value, uid, gid)
    env.update(KNOWLEDGE_RUN_UID=str(uid), KNOWLEDGE_RUN_GID=str(gid))
    _write(output / ".env", ("\n".join(f"{key}={value}" for key, value in env.items()) + "\n").encode(), uid, gid)
    for name in ("compose.yaml", "Caddyfile"):
        shutil.copyfile(ROOT / "deploy" / name, output / name)
        (output / name).chmod(0o600)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--client-secret-file", type=Path, required=True)
    parser.add_argument("--source-policy-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uid", type=int, default=10001 if os.geteuid() == 0 else os.getuid())
    parser.add_argument("--gid", type=int, default=10001 if os.geteuid() == 0 else os.getgid())
    args = parser.parse_args(argv)
    try:
        configure(args.settings, args.client_secret_file, args.source_policy_file,
                  args.output, args.uid, args.gid)
    except (OSError, ValueError, UnicodeError):
        # Config, parser and filesystem diagnostics can contain credentials.
        parser.exit(2, "Install configuration refused; check protected inputs and directory ownership\n")
    print("Installation prepared. Run the migration and startup commands in the deployment runbook.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
