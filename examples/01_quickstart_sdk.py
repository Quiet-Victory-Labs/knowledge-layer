"""Discover, review and publish a synthetic source; verify its SDK MCP retrieval.

Run: uv run --no-sync python examples/01_quickstart_sdk.py
No model, external account or customer data is used.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from knowledge_layer import KnowledgeClient


async def verify_mcp(client: KnowledgeClient, connection_id: str) -> None:
    server = client.get_mcp_server()
    sources = (await server.call_tool("list_knowledge_sources", {})).structured_content
    assert sources is not None
    assert sources["tenant"] == client.tenant_id
    assert [source["id"] for source in sources["connections"]] == [connection_id]
    contract = (await server.call_tool(
        "get_semantic_contract", {"connection_id": connection_id}
    )).structured_content
    assert contract is not None and contract["agent_ready"] is True
    print("MCP retrieved the same tenant's published source.")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="kl-quickstart-") as temp_dir:
        root = Path(temp_dir)
        data_dir = root / "source"
        data_dir.mkdir()
        (data_dir / "orders.csv").write_text(
            "order_id,total\nO-1,12\nO-2,24\n", encoding="utf-8"
        )
        with KnowledgeClient(
            database_url=f"sqlite:///{root / 'registry.db'}",
            tenant_id="quickstart-org",
            local_files_root=data_dir,
        ) as client:
            connected = client.connect_source(
                kind="local_files", config={"path": "."}, name="Synthetic orders"
            )
            cid = connected["connection_id"]
            snapshot = connected["snapshot"]
            assert snapshot["discovery_complete"] is True
            assert snapshot["agent_ready"] is False
            asset = next(item for item in snapshot["assets"] if item["name"] == "orders.csv")
            assert asset["profile"]["row_count"] == 2
            evidence = next(item for item in snapshot["evidence"] if item.get("asset_id") == asset["id"])
            matches = client.search_evidence("schema", connection_id=cid)
            assert matches and all(item.get("source_pointer") for item in matches)
            print(f"Discovered {len(snapshot['assets'])} asset; metadata evidence is retrievable.")

            proposal = client.propose_assertion(
                connection_id=cid,
                assertion_kind="grain",
                asset_id=asset["id"],
                payload={
                    "description": "One synthetic order per order_id.",
                    "key_fields": ["order_id"],
                },
                evidence_ids=[evidence["id"]],
                rationale="The demo owner inspected the two synthetic records and their schema.",
            )
            # A real installation requires an accountable person's review here.
            # Only this exact synthetic claim is approved; other drafts stay pending.
            client.decide_assertion(cid, proposal["id"], decision="approve")
            publication = client.certify_snapshot(
                cid,
                reviewer="synthetic-demo-owner",
                rationale="The demo owner reviewed this exact grain and current evidence.",
            )
            assert publication["agent_ready"] is True
            assert publication["meaning_certified"] is False
            contract = client.get_semantic_contract(cid)
            dataset = next(item for item in contract["datasets"] if item["asset_id"] == asset["id"])
            assert dataset["grain"]["key_fields"] == ["order_id"]
            assert contract["agent_ready"] is True
            print("Reviewed grain published. Publication records review, not certified business truth.")
            asyncio.run(verify_mcp(client, cid))


if __name__ == "__main__":
    main()
