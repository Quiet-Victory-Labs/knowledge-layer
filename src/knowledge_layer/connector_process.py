"""Private bounded connector entry point. It has no registry authority."""

from __future__ import annotations

import json
import math
import os
import re
import resource
import signal
import sys


def main() -> int:
    # A container also stops after coordinator/client loss. The kernel's default
    # SIGALRM action terminates even a driver blocked inside a native call.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(300)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    maximum = 32 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (maximum + 1, maximum + 1))
    raw = sys.stdin.buffer.read(maximum + 1)
    if len(raw) > maximum:
        return 2
    try:
        packet = json.loads(raw)
        keys = {"operation", "kind", "config", "request"}
        container = sys.argv[1:] == ["--environment-from-packet"]
        if container:
            keys.add("environment")
        elif sys.argv[1:]:
            return 2
        if isinstance(packet, dict) and "timeout_seconds" in packet:
            keys.add("timeout_seconds")
        if not isinstance(packet, dict) or set(packet) != keys or not isinstance(packet["config"], dict):
            return 2
        timeout = packet.get("timeout_seconds", 120)
        if type(timeout) not in {int, float} or not 0 < timeout <= 300:
            return 2
        signal.alarm(math.ceil(timeout))
        if container:
            environment = packet["environment"]
            if (not isinstance(environment, dict) or len(environment) > 20
                    or any(not isinstance(key, str) or not isinstance(value, str)
                           or len(value.encode()) > 65536 or "\x00" in value for key, value in environment.items())):
                return 2
            allowed = {"PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "PYTHONDONTWRITEBYTECODE",
                       "KNOWLEDGE_ICEBERG_PROFILES", "KNOWLEDGE_ALLOW_INSECURE_ADAPTER_ENDPOINTS",
                       "KNOWLEDGE_ALLOW_INSECURE_CATALOG_ENDPOINTS"}
            if any(key not in allowed and not re.fullmatch(r"KNOWLEDGE_SOURCE_[A-Z0-9_]{1,64}", key)
                   for key in environment):
                return 2
            # Keep only the image's interpreter path. Host paths, Docker settings
            # and any accidental image credentials are never source authority.
            path = os.environ.get("PATH", "/app/.venv/bin:/usr/bin")
            os.environ.clear()
            os.environ.update({key: value for key, value in environment.items() if key != "PATH"})
            os.environ["PATH"] = path
        if packet["operation"] == "snapshot" and packet["request"] is None:
            from knowledge_layer.curation import curate_source

            result = curate_source(packet["kind"], packet["config"])
        elif packet["operation"] == "inventory_page" and packet["kind"] == "external_adapter" and isinstance(packet["request"], dict):
            from knowledge_layer.connectors.external_adapter import _profile
            from knowledge_layer.connectors.external_inventory import fetch_inventory_page

            profile = _profile(packet["config"]["secret_ref"], packet["config"]["scope"])
            result = fetch_inventory_page(profile, packet["request"])
        else:
            return 2
        output = json.dumps(result, allow_nan=False, ensure_ascii=False).encode("utf-8")
        if len(output) > maximum:
            return 2
        sys.stdout.buffer.write(output)
        return 0
    except Exception:  # noqa: BLE001 - no driver errors, secrets or source text on stderr
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
