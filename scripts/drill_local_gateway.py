"""Measure the loopback agent gateway with bounded concurrent read requests."""

from __future__ import annotations

import json
import os
import secrets
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def _quick_tool_count(status: int, response: dict) -> int:
    """Verify the required consumer/onboarding contract; record observed size."""
    result = response.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if status != 200 or not isinstance(tools, list) or not tools:
        raise RuntimeError("Quick MCP tool contract was unavailable")
    names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
    required = {"get_workspace", "connect_source", "refresh_source", "next_work",
                "search_evidence", "propose_meaning", "propose_contract", "resolve_context",
                "get_contract", "get_capabilities", "get_task_context", "revalidate_task_context",
                "plan_metric_query", "plan_relationship", "get_inventory", "get_maintenance_work",
                "resolve_guidance", "get_ontology_export"}
    if (len(names) != len(tools) or not all(isinstance(name, str) for name in names)
            or len(names) != len(set(names)) or not required <= set(names)):
        raise RuntimeError("Quick MCP tool contract was unavailable")
    return len(tools)


def main() -> int:
    values = dict(
        line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    token = values["KNOWLEDGE_AGENT_TOKEN"]
    base = f"http://127.0.0.1:{values.get('KNOWLEDGE_API_PORT', '8000')}"
    base_headers = {"Authorization": f"Bearer {token}"}
    mcp_headers = base_headers | {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }

    def request(path: str, headers: dict[str, str], body: dict | None = None) -> tuple[int, int, float, dict]:
        data = json.dumps(body).encode() if body is not None else None
        started = time.perf_counter()
        try:
            with urlopen(Request(base + path, data=data, headers=headers), timeout=10) as response:
                payload = response.read()
                status = response.status
        except HTTPError as exc:
            payload = exc.read()
            status = exc.code
        elapsed = (time.perf_counter() - started) * 1_000
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {}
        return status, len(payload), elapsed, parsed

    denied = request("/agent/api/sources", {})
    invalid = request("/agent/api/sources", {"Authorization": "Bearer invalid"})
    if denied[0] != 401 or invalid[0] != 401:
        raise RuntimeError("Agent gateway did not reject unauthenticated requests")

    definition = request("/agent/mcp/", mcp_headers, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })
    tool_count = _quick_tool_count(definition[0], definition[3])
    listed = request("/agent/api/sources", base_headers)
    if listed[0] != 200:
        raise RuntimeError("Authenticated source list was unavailable")
    if listed[3].get("total") == 0:
        # A fresh CI registry has no concepts to search. Exercise the same
        # agent onboarding path used by a customer before measuring search.
        seeded = request("/agent/api/sources", base_headers | {"Content-Type": "application/json"}, {
            "name": "Gateway synthetic Northstar", "kind": "local_files",
            "config": {"path": "northstar"},
        })
        if seeded[0] != 201 or seeded[3].get("counts", {}).get("concepts", 0) < 1:
            raise RuntimeError("Synthetic source onboarding did not create searchable concepts")
    concept_search = request("/agent/api/search?q=customer", base_headers)
    if concept_search[0] != 200 or concept_search[3].get("search_mode") != "indexed_lexical":
        raise RuntimeError("Authenticated indexed concept search was unavailable")

    def sample(index: int) -> tuple[int, int, float, dict]:
        return request("/agent/mcp/", mcp_headers, {
            "jsonrpc": "2.0", "id": index + 2, "method": "tools/call",
            "params": {"name": "get_workspace", "arguments": {}},
        })

    measurements = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(sample, index) for index in range(100)]
        for future in as_completed(futures):
            measurements.append(future.result())
    errors = [item for item in measurements if item[0] != 200 or "error" in item[3]]
    if errors:
        raise RuntimeError(f"MCP gateway returned {len(errors)} failed read requests")
    latencies = [item[2] for item in measurements]
    sizes = [item[1] for item in measurements]
    report = {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "result": "passed", "mode": "local_loopback_compose", "concurrency": 8,
        "quick_mcp_tool_count": tool_count, "tool_definition_bytes": definition[1],
        "concept_search_mode": concept_search[3]["search_mode"],
        "request_count": len(measurements), "error_count": 0,
        "workspace_response_bytes_mean": round(statistics.mean(sizes)),
        "workspace_response_bytes_max": max(sizes),
        "workspace_latency_ms_p50": round(_percentile(latencies, 0.50), 1),
        "workspace_latency_ms_p95": round(_percentile(latencies, 0.95), 1),
        "workspace_latency_ms_max": round(max(latencies), 1),
        "unauthenticated_status": denied[0], "invalid_token_status": invalid[0],
        "note": "Synthetic local file sources only. No customer IdP, warehouse, network latency, or model token accounting.",
    }
    output_dir = ROOT / ".local" / "recovery"
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = output_dir / f"gateway-drill-{secrets.token_hex(4)}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"result": "passed", "report": str(path),
                      "p95_ms": report["workspace_latency_ms_p95"],
                      "response_bytes_mean": report["workspace_response_bytes_mean"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
