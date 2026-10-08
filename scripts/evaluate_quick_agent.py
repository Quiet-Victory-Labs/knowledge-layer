"""Measure the compact MCP contract over a real local HTTP connection.

This is a protocol and payload budget check, not a model-token or agent-quality
measurement. It reads existing knowledge and does not approve proposals.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]


def _token() -> str:
    value = os.getenv("KNOWLEDGE_AGENT_TOKEN")
    if value:
        return value
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("KNOWLEDGE_AGENT_TOKEN="):
            return line.partition("=")[2]
    raise RuntimeError("A local agent token is required")


def _base_url() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("KNOWLEDGE_UI_PORT="):
            return f"http://127.0.0.1:{line.partition('=')[2]}"
    return "http://127.0.0.1:4173"


async def _inspect(base_url: str, path: str, token: str, connection_id: str | None) -> dict[str, Any]:
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as client,
        streamable_http_client(f"{base_url.rstrip('/')}/agent/{path}/", http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        protocol = await session.initialize()
        tools = (await session.list_tools()).tools
        result: dict[str, Any] = {
            "protocol": protocol.protocol_version,
            "tool_count": len(tools),
            "schema_bytes": len(json.dumps([tool.model_dump(mode="json") for tool in tools],
                                           separators=(",", ":")).encode("utf-8")),
        }
        if path != "mcp":
            return result

        async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
            started = time.perf_counter()
            response = await session.call_tool(name, args)
            if response.is_error or not isinstance(response.structured_content, dict):
                raise RuntimeError(f"{name} returned an MCP error")
            payload = response.structured_content
            result[f"{name}_latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            result[f"{name}_bytes"] = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            return payload

        workspace = await call("get_workspace", {})
        source = connection_id or next((item["id"] for item in workspace.get("sources", [])), None)
        result["source_count"] = workspace.get("total_sources", 0)
        result["connection_id"] = source
        if source:
            for track in ("meaning", "contract"):
                packet = await call("next_work", {"connection_id": source, "track": track, "limit": 2})
                result[f"{track}_work_total"] = packet.get("total", 0)
                result[f"{track}_work_bytes"] = result["next_work_bytes"]
        return result


async def evaluate(base_url: str, token: str, connection_id: str | None) -> dict[str, Any]:
    quick = await _inspect(base_url, "mcp", token, connection_id)
    advanced = await _inspect(base_url, "advanced-mcp", token, None)
    checks = {
        "small_tool_surface": quick["tool_count"] <= 10,
        "schema_smaller_than_advanced": quick["schema_bytes"] < advanced["schema_bytes"],
        "bounded_workspace": quick["get_workspace_bytes"] < 24_000,
        "bounded_work_packets": all(
            quick.get(f"{track}_work_bytes", 0) < 12_000 for track in ("meaning", "contract")
        ),
    }
    return {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "endpoint": f"{base_url.rstrip('/')}/agent/mcp/",
        "quick": quick, "advanced": advanced,
        "checks": checks, "passed": all(checks.values()),
        "measurement_scope": "One local client session. Byte counts are serialized tool schemas and JSON results, not model tokens or a load test.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=_base_url())
    parser.add_argument("--connection-id")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/quick-agent-evaluation.json")
    args = parser.parse_args()
    result = asyncio.run(evaluate(args.base_url, _token(), args.connection_id))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "quick_tools": result["quick"]["tool_count"],
                      "quick_schema_bytes": result["quick"]["schema_bytes"],
                      "advanced_schema_bytes": result["advanced"]["schema_bytes"],
                      "report": str(args.output)}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
