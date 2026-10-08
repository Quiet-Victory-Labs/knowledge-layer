#!/usr/bin/env python3
"""Add this service to a project's Codex, Claude or Cursor MCP configuration.

Credentials are environment references; existing servers are preserved. Review
client trust/permission prompts after setup, then load the portable skill.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

PATHS = {"codex": ".codex/config.toml", "claude": ".mcp.json", "cursor": ".cursor/mcp.json"}


def configure(*, client: str, project: Path, url: str, replace=False) -> Path:
    if client not in PATHS:
        raise ValueError("Client must be codex, claude or cursor")
    parsed = urlparse(url)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or
            parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("MCP URL must use HTTPS or loopback HTTP with no embedded credential")
    project = project.expanduser().absolute()
    if not project.is_dir():
        raise ValueError("Project directory must exist")
    target = project / PATHS[client]
    for path in (target, *target.parents):
        if path.is_symlink():
            raise ValueError("Configuration paths must not contain symlinks")
    old = target.read_text() if target.exists() else ""
    if client == "codex":
        config = tomllib.loads(old)
        existing = config.get("mcp_servers", {}).get("knowledge_layer")
        if existing is not None:
            if existing.get("url") == url and existing.get("bearer_token_env_var") == "KNOWLEDGE_AGENT_TOKEN":
                return target
            # Do not rewrite arbitrary user TOML; replacement uses a precise section only.
            if not replace:
                raise ValueError("knowledge_layer already exists; use --replace to update its section")
            pattern = r"(?ms)^\[mcp_servers\.knowledge_layer\]\s*\n.*?(?=^\[|\Z)"
            old, count = re.subn(pattern, "", old)
            if count != 1 or "knowledge_layer" in tomllib.loads(old).get("mcp_servers", {}):
                raise ValueError("Existing knowledge_layer uses a complex TOML shape; edit it in your client")
        new = old.rstrip() + ('\n\n' if old.strip() else '') + (
            '[mcp_servers.knowledge_layer]\nurl = ' + json.dumps(url) + '\n'
            'bearer_token_env_var = "KNOWLEDGE_AGENT_TOKEN"\n')
        tomllib.loads(new)
    else:
        config = json.loads(old) if old.strip() else {}
        if not isinstance(config, dict) or not isinstance(config.get("mcpServers", {}), dict):
            raise ValueError("MCP configuration must contain a servers object")
        value = {"url": url, "headers": {"Authorization": "Bearer " + (
            "${KNOWLEDGE_AGENT_TOKEN}" if client == "claude" else "${env:KNOWLEDGE_AGENT_TOKEN}")}}
        if client == "claude":
            value["type"] = "http"
        existing = config.setdefault("mcpServers", {}).get("knowledge_layer")
        if existing == value:
            return target
        if existing is not None and not replace:
            raise ValueError("knowledge_layer already exists; use --replace to update it")
        config["mcpServers"]["knowledge_layer"] = value
        new = json.dumps(config, indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(target, target.with_name(target.name + ".backup-" + stamp))
    temporary = target.with_name(target.name + ".knowledge-new")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("Temporary configuration path already exists")
    try:
        with temporary.open("x") as stream:
            stream.write(new)
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True, choices=PATHS)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--url", default="http://127.0.0.1:4173/agent/mcp/")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    try:
        target = configure(**vars(args))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Configured {target}; expose KNOWLEDGE_AGENT_TOKEN to your client and confirm server trust.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
