"""Validate the bundled proxy's header/body budgets offline with pinned Caddy."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

CADDY_IMAGE = "caddy@sha256:13b7fbadd017b042956fddbceedeeea12bb1e560534f9b3df281269dbcc61813"


def main() -> int:
    config = Path(__file__).resolve().parents[1] / "deploy" / "Caddyfile"
    try:
        result = subprocess.run([
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--memory", "128m", "--cpus", "0.5", "--pids-limit", "64",
            "--tmpfs", "/data:size=16m", "--tmpfs", "/config:size=16m",
            "--env", "KNOWLEDGE_HOST=knowledge.example.test",
            "--mount", f"type=bind,src={config},dst=/etc/caddy/Caddyfile,readonly",
            "--entrypoint", "caddy", CADDY_IMAGE, "adapt", "--config",
            "/etc/caddy/Caddyfile", "--adapter", "caddyfile", "--validate",
        ], capture_output=True, text=True, timeout=60, check=True)
        servers = json.loads(result.stdout)["apps"]["http"]["servers"].values()
        listeners = set()
        for server in servers:
            listeners.update(server["listen"])
            if (server.get("read_header_timeout") != 5_000_000_000
                    or server.get("read_timeout") != 10_000_000_000
                    or server.get("idle_timeout") != 30_000_000_000
                    or server.get("max_header_bytes") != 32_000
                    or server.get("write_timeout", 0) != 0):
                raise ValueError("Proxy budgets changed")
        if listeners != {":80", ":443"}:
            raise ValueError("Proxy listeners changed")
        print("Offline proxy configuration validates; both listeners retain header/body budgets")
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("Offline proxy configuration verification failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
