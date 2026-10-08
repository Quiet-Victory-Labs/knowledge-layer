"""Qualify company guidance on an explicitly disposable PostgreSQL registry.

Prepare v10, verify its backup/restore separately, then upgrade and exercise.
Only this registry's metadata is written; source discovery uses bundled fixtures.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from alembic import command
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, insert, inspect, select

from knowledge_layer.migrate import SchemaMigrationError, _config, ensure_current_schema, run
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.store import KnowledgeStore

ROOT = Path(__file__).resolve().parents[1]


def prepare(store: KnowledgeStore) -> dict:
    if run("status", store)["revision"] is not None:
        raise RuntimeError("Prepare requires a new disposable registry")
    with store.engine.begin() as db:
        command.upgrade(_config(db, store), "0010_refresh_schedules")
        stamp = datetime.now(UTC).isoformat()
        db.execute(insert(store.connections).values(
            id="legacy_guidance_drill", tenant_id="demo", kind="local_files",
            name="Legacy preservation fixture", config={"path": "northstar"},
            status="created", created_at=stamp, updated_at=stamp,
        ))
    try:
        unintended = KnowledgeStore(database_url=operator_secret("DATABASE_URL"))
    except SchemaMigrationError:
        pass
    else:
        unintended.close()
        raise RuntimeError("Demo startup bypassed the versioned schema upgrade")
    assert "knowledge_guidance_bundles" not in inspect(store.engine).get_table_names()
    return {"revision": run("status", store)["revision"], "legacy_connection_created": True,
            "demo_refuses_stale_versioned_registry_without_creating_tables": True,
            "next_step": "Verify a v10 backup/restore before running upgrade."}


def upgrade(store: KnowledgeStore) -> dict:
    result = run("upgrade", store)
    ensure_current_schema(store)
    assert store.get_connection("demo", "legacy_guidance_drill") is not None
    return {"migration": result, "legacy_connection_preserved": True}


def reviewed_fixture_source(store: KnowledgeStore) -> dict:
    source = store.create_connection("demo", kind="local_files", name="Northstar guidance dependency",
                                     config={"path": "northstar"})
    snap = store.refresh_connection("demo", source["id"])["snapshot"]
    asset = snap["assets"][0]
    evidence = next(row["id"] for row in snap["evidence"] if row.get("asset_id") == asset["id"])
    proposal = store.submit_contract_assertion(
        "demo", source["id"], assertion_kind="dataset_description", asset_id=asset["id"],
        payload={"description": "A bundled synthetic source used to verify guidance dependency binding."},
        evidence_ids=[evidence], expected_snapshot_id=snap["id"],
        rationale="The evaluator reviewed the actual bundled source inventory for this drill.",
    )
    store.decide_proposal("demo", source["id"], proposal["id"], decision="approve",
                          rationale="The evaluator approved only this synthetic source description.")
    current = store.get_snapshot("demo", source["id"])
    store.publish_source("demo", source["id"], expected_snapshot_id=current["id"],
                         expected_snapshot_version=current["version"],
                         rationale="The evaluator accepts this reviewed fixture for the isolated drill.")
    assert store.get_snapshot("demo", source["id"])["agent_ready"] is True
    return {"connection_id": source["id"], "snapshot_id": current["id"],
            "snapshot_version": current["version"]}


async def exercise(store: KnowledgeStore, base: str) -> dict:
    from knowledge_layer.guidance import GuidanceRegistry

    ensure_current_schema(store)
    with store.engine.connect() as db:
        if db.execute(select(func.count()).select_from(store.guidance_bundles)).scalar_one():
            raise RuntimeError("Exercise requires a disposable registry with no prior guidance")
    token = operator_secret("KNOWLEDGE_AGENT_TOKEN")
    if not token:
        raise RuntimeError("A protected agent token reference is required")
    payload = json.loads((ROOT / "examples/company-guidance-synthetic.json").read_text())
    dependency = reviewed_fixture_source(store)
    payload["dependencies"] = [dependency]
    registry = GuidanceRegistry(store)
    args = {"business_unit": "synthetic-analytics", "task": "sales-analysis"}
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx.AsyncClient(headers=headers, timeout=30) as http,
        streamable_http_client(f"{base}/agent/mcp/", http_client=http) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()

        async def call(name: str, arguments: dict) -> dict:
            result = await session.call_tool(name, arguments)
            if result.is_error:
                raise RuntimeError(f"Expected successful company guidance tool: {name}")
            return result.structured_content

        tools = {item.name for item in (await session.list_tools()).tools}
        assert {"resolve_guidance", "get_guidance", "draft_guidance", "export_guidance_bundle"} <= tools
        assert not {"publish_guidance", "revoke_guidance"} & tools
        row = await call("draft_guidance", {"slug": "synthetic-analytics-research", "payload": payload})
        assert (await call("resolve_guidance", args))["status"] == "empty"
        registry.publish("demo", "synthetic-analytics-research", revision=row["revision"],
                         expected_digest=row["digest"],
                         rationale="The evaluator reviewed this isolated Example procedure template.")
        ready = await call("resolve_guidance", args)
        assert ready["status"] == "ready" and ready["bundles"][0]["digest"] == row["digest"]
        assert (await call("resolve_guidance", {}))["status"] == "empty"
        package = await call("export_guidance_bundle", args)
        assert payload["skills"][0]["instructions"] in "\n".join(package["files"].values())
        export_path = ROOT / ".local/company-guidance-2026-10-03/export.json"
        export_path.write_text(
            json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        export_path.chmod(0o600)
        response = await http.get(f"{base}/agent/api/guidance/resolve", params=args)
        response.raise_for_status()
        assert response.json()["bundles"][0]["digest"] == row["digest"]
        assert (await http.post(f"{base}/agent/api/guidance/synthetic-analytics-research/publish", json={})).status_code == 404
        revised = copy.deepcopy(payload)
        revised["description"] += " Second review draft."
        second = await call("draft_guidance", {"slug": "synthetic-analytics-research", "payload": revised,
                                               "expected_revision": 1})
        assert second["revision"] == 2
        assert (await call("resolve_guidance", args))["bundles"][0]["revision"] == 1
        conflict = copy.deepcopy(payload)
        conflict["rules"][0]["value"] = "retrospective"
        conflicting = await call("draft_guidance", {"slug": "conflicting-time-policy", "payload": conflict})
        registry.publish("demo", "conflicting-time-policy", revision=1,
                         expected_digest=conflicting["digest"],
                         rationale="The evaluator deliberately publishes a conflicting fixture for refusal.")
        blocked = await call("resolve_guidance", args)
        assert blocked["status"] == "blocked" and blocked["conflicts"][0]["key"] == "research-input-timing"
        assert (await session.call_tool("export_guidance_bundle", args)).is_error is True
        registry.revoke("demo", "conflicting-time-policy", revision=1,
                        reason="The evaluator withdrew the deliberately conflicting fixture.")
        withdrawn = await call("resolve_guidance", args)
        assert withdrawn["status"] == "blocked" and any(
            "revoked" in item["reasons"] for item in withdrawn["blocked"])
        # A withdrawn bundle remains a visible blocker, so use another
        # applicable context to independently verify source invalidation.
        dependency_payload = copy.deepcopy(payload)
        dependency_payload["tasks"] = ["dependency-check"]
        dep_row = await call("draft_guidance", {"slug": "dependency-check", "payload": dependency_payload})
        registry.publish("demo", "dependency-check", revision=1, expected_digest=dep_row["digest"],
                         rationale="The evaluator reviews a separately scoped dependency fixture.")
        dep_args = {"business_unit": "synthetic-analytics", "task": "dependency-check"}
        assert (await call("resolve_guidance", dep_args))["status"] == "ready"
        store.revoke_publication("demo", dependency["connection_id"],
                                 reason="The evaluator withdrew the synthetic source publication.")
        suspended = await call("resolve_guidance", dep_args)
        assert suspended["status"] == "blocked" and suspended["bundles"] == []
        assert (await session.call_tool("export_guidance_bundle", dep_args)).is_error is True
        with store.engine.connect() as db:
            revision_count = db.execute(select(store.guidance_revisions)).all()
            audit_types = set(db.execute(select(store.audit_events.c.event_type)).scalars())
        return {"native_http_mcp_roundtrip": True, "http_same_published_digest": True,
                "immutable_revision_count": len(revision_count), "published_during_new_draft": 1,
                "declared_conflict_blocks_export": True, "withdrawal_visible": True,
                "source_publication_withdrawal_blocks_guidance": True,
                "audit_event_types": sorted(audit_types), "synthetic-analytics_source_writes": 0,
            "qualification": "Example procedure template and synthetic dependency; no customer query or live harness activation"}


async def concurrency(store: KnowledgeStore, base: str) -> dict:
    ensure_current_schema(store)
    token = operator_secret("KNOWLEDGE_AGENT_TOKEN")
    if not token:
        raise RuntimeError("A protected agent token reference is required")
    payload = json.loads((ROOT / "examples/company-guidance-synthetic.json").read_text())
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
        responses = await asyncio.gather(*[
            http.post(f"{base}/agent/api/guidance/concurrency-fixture/draft", json={"payload": payload})
            for _ in range(2)
        ])
    assert sorted(item.status_code for item in responses) == [201, 409]
    with store.engine.connect() as db:
        revisions = db.execute(select(func.count()).select_from(store.guidance_revisions).where(
            store.guidance_revisions.c.tenant_id == "demo",
            store.guidance_revisions.c.slug == "concurrency-fixture",
        )).scalar_one()
        audits = db.execute(select(store.audit_events.c.details).where(
            store.audit_events.c.event_type == "guidance.draft",
        )).scalars().all()
    assert revisions == 1 and sum(item.get("slug") == "concurrency-fixture" for item in audits) == 1
    return {"concurrent_draft_statuses": [201, 409], "persisted_revisions": revisions,
            "successful_mutation_audit_count": 1}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "upgrade", "exercise", "concurrency"))
    parser.add_argument("--base", default="http://127.0.0.1:4175")
    args = parser.parse_args()
    url = operator_secret("DATABASE_URL")
    if not url:
        parser.error("A protected disposable DATABASE_URL reference is required")
    store = KnowledgeStore(database_url=url, initialize_schema=False)
    try:
        if (store.backend != "postgresql" or not str(store.engine.url.database).startswith("knowledge_guidance_drill")):
            raise RuntimeError("This drill requires an explicitly disposable knowledge_guidance_drill registry")
        if args.base != "http://127.0.0.1:4175":
            raise RuntimeError("The qualification endpoint must use the fixed loopback drill port 4175")
        if args.action == "prepare":
            result = prepare(store)
        elif args.action == "upgrade":
            result = upgrade(store)
        else:
            result = asyncio.run((exercise if args.action == "exercise" else concurrency)(store, args.base))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
