"""Model-free enterprise journey on isolated synthetic company data.

Run: uv run --no-sync python examples/enterprise_consumer.py
An operator creates fixtures and performs explicit steward review. A separate
consumer uses only MCP context/planning and its read-only source connection.
No model, customer source, service or external account is required.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from knowledge_layer.guidance import GuidanceRegistry
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeConflict, KnowledgeStore


def _publish(store, cid):
    snapshot = store.get_snapshot("demo", cid)
    store.publish_source("demo", cid, expected_snapshot_id=snapshot["id"],
        expected_snapshot_version=snapshot["version"], rationale="Fixture steward reviewed the exact synthetic source scope.")


def _review_metric(store, cid, *, description, filters):
    snapshot = store.get_snapshot("demo", cid)
    asset = snapshot["assets"][0]
    handle = next(item["id"] for item in snapshot["evidence"] if item.get("asset_id") == asset["id"]
                  and item["evidence_type"] == "asset_metadata")
    metric = {"name": "Revenue", "description": description, "measure_field": "amount", "aggregation": "sum",
        "unit": "USD", "schema_version": "metric-v2", "dimensions": ["region"], "filters": filters,
        "time": None, "cohort": {"description": description, "key_fields": ["order_id"]},
        "additivity": {"kind": "additive", "non_additive_dimensions": []}}
    draft = store.submit_contract_assertion("demo", cid, assertion_kind="metric", asset_id=asset["id"],
        payload=metric, evidence_ids=[handle], evidence_refs=[], expected_snapshot_id=snapshot["id"],
        rationale="The fixture steward chose this business unit's explicit cohort and source unit.")
    store.decide_proposal("demo", cid, draft["id"], decision="approve")
    _publish(store, cid)
    return draft["id"]


@contextmanager
def enterprise_fixture():
    """Yield reviewed synthetic company data and remove every owned resource on exit."""
    with tempfile.TemporaryDirectory(prefix="knowledge-enterprise-") as temporary:
        root = Path(temporary)
        documents = root / "policy"
        documents.mkdir()
        (documents / "revenue.md").write_text(
            "# Revenue\nFinance revenue includes booked orders only. Sales pipeline includes every opportunity. "
            "These populations overlap and must not be added together.\n")
        bindings, old_environment = {}, {}
        store = KnowledgeStore(database_url=f"sqlite:///{root / 'registry.db'}", local_files_root=root)
        try:
            for unit, records in {
                "finance": [("F1", 20, "east", "booked"), ("F2", 0, "east", "booked"),
                            ("F3", None, "west", "booked"), ("F4", 1000, "west", "canceled")],
                "sales": [("S1", 40, "east", "won"), ("S2", 1000, "west", "open")],
            }.items():
                database = root / f"{unit}.db"
                with sqlite3.connect(database) as fixture:
                    fixture.execute("CREATE TABLE orders(order_id TEXT PRIMARY KEY,amount REAL,region TEXT,status TEXT)")
                    fixture.executemany("INSERT INTO orders VALUES (?,?,?,?)", records)
                secret_name = "KNOWLEDGE_SOURCE_DEMO_" + unit.upper()  # pragma: allowlist secret - generated fixture reference/value
                secret = root / f"{unit}.secret"
                secret.write_text("sqlite:///" + str(database))
                secret.chmod(0o600)
                reference = secret_name + "_FILE"
                old_environment[reference] = os.environ.get(reference)
                os.environ[reference] = str(secret)
                source = store.create_connection("demo", kind="sql_catalog", name=unit.title(),
                    config={"provider": "sqlite", "secret_ref": secret_name})
                cid = source["id"]
                store.refresh_connection("demo", cid)
                description = ("Booked finance orders only; canceled orders excluded." if unit == "finance"
                               else "Sales opportunities including open pipeline; overlaps finance.")
                pid = _review_metric(store, cid, description=description,
                    filters=[{"field": "status", "op": "eq", "value": "booked"}] if unit == "finance" else [])
                bindings[unit] = {"connection_id": cid, "assertion_id": pid, "database": database}
            source = store.create_connection("demo", kind="local_files", name="Company policies", config={"path": "policy"})
            cid = source["id"]
            snap = store.refresh_connection("demo", cid)["snapshot"]
            asset = snap["assets"][0]
            handle = next(item["id"] for item in snap["evidence"] if item.get("asset_id") == asset["id"])
            draft = store.submit_contract_assertion("demo", cid, assertion_kind="dataset_description", asset_id=asset["id"],
                payload={"description": "Synthetic company revenue population policy."}, evidence_ids=[handle],
                expected_snapshot_id=snap["id"], rationale="The fixture owner inspected the exact document and its scope.")
            store.decide_proposal("demo", cid, draft["id"], decision="approve")
            _publish(store, cid)
            registry = GuidanceRegistry(store)
            guidance = registry.draft("demo", "company-revenue", {"title": "Revenue research", "description": "Inspect each unit's population.",
                "business_units": [], "tasks": [], "audience": {"company_wide": True, "subjects": [], "groups": []},
                "skills": [{"name": "revenue-research", "description": "Report source-specific revenue.",
                    "instructions": "Use each unit's approved revenue cohort. Explain overlap before combining results.",
                    "required_capabilities": ["knowledge_context", "read_only_sql"]}],
                "rules": [{"key": "source-writes", "statement": "Source writes are forbidden.", "value": False}],
                "dependencies": [{"connection_id": cid, "snapshot_id": store.get_snapshot("demo", cid)["id"],
                                  "snapshot_version": store.get_snapshot("demo", cid)["version"]}]}, expected_revision=0)
            registry.publish("demo", "company-revenue", revision=guidance["revision"], expected_digest=guidance["digest"],
                             rationale="The fixture steward reviewed this exact policy and its document dependency.")
            yield {"root": root, "store": store, "bindings": bindings,
                   "document_connection_id": cid,
                   "guidance": {"slug": "company-revenue", "revision": guidance["revision"],
                                "digest": guidance["digest"]}}
        finally:
            try:
                store.close()
            finally:
                for reference, old in old_environment.items():
                    if old is None:
                        os.environ.pop(reference, None)
                    else:
                        os.environ[reference] = old


async def run_example():
    with enterprise_fixture() as fixture:
        store = fixture["store"]
        bindings = fixture["bindings"]
        cid = fixture["document_connection_id"]
        server = build_quick_mcp_server(store)
        async def tool(name, arguments):
            response = await server.call_tool(name, arguments)
            if response.is_error or not isinstance(response.structured_content, dict):
                raise RuntimeError(f"Consumer tool failed: {name}")
            return response.structured_content
        packet = await tool("get_task_context", {"query": "Revenue",
            "connection_ids": [*(binding["connection_id"] for binding in bindings.values()), cid]})
        await tool("revalidate_task_context", {"receipt": packet["receipt"]})
        results = {}
        for unit, binding in bindings.items():
            plan = await tool("plan_metric_query", {"connection_id": binding["connection_id"],
                "assertion_id": binding["assertion_id"], "request": {"group_by": ["region"]}})
            # The harness source tool uses independent native read-only mode.
            with sqlite3.connect(f"file:{binding['database']}?mode=ro", uri=True) as source:
                source.row_factory = sqlite3.Row
                source.execute("PRAGMA query_only = ON")
                rows = [dict(row) for row in source.execute(plan["sql"], plan["parameters"])]
                if source.execute(plan["diagnostics"]["grain_check_sql"], plan["parameters"]).fetchall():
                    raise RuntimeError("Synthetic grain check failed")
            results[unit] = {"rows": rows, "known_total": sum(row["metric_value"] for row in rows
                            if row["metric_value"] is not None), "execution": "independent_read_only_fixture"}
        if (results["finance"]["known_total"], results["sales"]["known_total"]) != (20, 1040):
            raise RuntimeError("Consumer selected an incorrect synthetic cohort")
        store.revoke_publication("demo", bindings["finance"]["connection_id"], reason="Fixture owner withdraws finance meaning.")
        try:
            await tool("revalidate_task_context", {"receipt": packet["receipt"]})
        except Exception as exc:
            from mcp.server.mcpserver.exceptions import ToolError
            if not isinstance(exc, (ToolError, KnowledgeConflict)):
                raise
            revoked_refused = True
        else:
            raise RuntimeError("Withdrawn meaning remained usable")
        return {"schema_version": "enterprise-demo-v1", "result": "passed", "fixture_only": True,
            "model_calls": 0, "connected_sources": 3, "results": results,
            "combined_total": None, "combination_reason": "Overlapping incompatible cohorts lack a reviewed composition contract.",
            "guidance_loaded": packet["guidance"]["status"], "withdrawn_receipt_refused": revoked_refused,
            "qualification": "In-process MCP and local SQLite, not native harness or customer-provider qualification."}
if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_example()), indent=2))
