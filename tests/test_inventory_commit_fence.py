"""Lease expiry during manifest commit rolls back the effective discovery head."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from knowledge_layer import refresh_fence as module
from knowledge_layer.inventory import InventoryStore
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.refresh_fence import RefreshFenceLost, refresh_fence
from knowledge_layer.store import KnowledgeStore


def test_lease_expiring_after_commit_validation_rolls_back_all_effects(tmp_path, monkeypatch):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'late-expiry.db'}")
    try:
        source = store.create_connection("demo", kind="local_files", name="Fixture", config={})
        cid = source["id"]
        queue = RefreshQueue(store)
        job = queue.enqueue("demo", cid)
        claim = queue.claim("worker")
        inventory = InventoryStore(store)
        with refresh_fence(job["id"], "worker", claim["attempt"]):
            run = inventory.begin("demo", cid, scope={"namespace": "finance", "selectors": ["orders"]},
                policy={"id": "metadata-only", "revision": None}, watermark={"kind": "unknown", "value": None},
                limits={"max_pages": 2, "max_entities": 2, "max_evidence": 2, "max_total_bytes": 8192})
            run = inventory.append("demo", cid, run["id"], page_index=0, cursor=None, next_cursor=None,
                entities=[], evidence=[], **{key: run[key] for key in ("scope_digest", "policy_digest", "watermark_digest")})
            clock = {"expired": False}
            original = store._audit
            def audit(*args, **kwargs):
                original(*args, **kwargs)
                if args[3] == "inventory.commit":
                    clock["expired"] = True
            class ControlledClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    value = datetime.now(UTC) + (timedelta(hours=1) if clock["expired"] else timedelta())
                    return value.astimezone(tz) if tz else value.replace(tzinfo=None)
            monkeypatch.setattr(store, "_audit", audit)
            monkeypatch.setattr(module, "datetime", ControlledClock)
            with pytest.raises(RefreshFenceLost):
                inventory.commit("demo", cid, run["id"], completion={"listing_complete": True,
                    "pages": 1, "entities": 0, "evidence": 0, "manifest_digest": run["manifest_digest"]})
        assert clock["expired"] is True
        assert inventory.status("demo", cid, run["id"])["status"] == "staging"
        assert inventory.current("demo", cid, scope_digest=run["scope_digest"], policy_digest=run["policy_digest"]) is None
        with store.engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(store.inventory_heads)).scalar_one() == 0
            assert conn.execute(select(func.count()).select_from(store.audit_events).where(
                store.audit_events.c.event_type == "inventory.commit")).scalar_one() == 0
        assert store.get_connection("demo", cid)["last_complete_refresh_at"] is None
    finally:
        store.close()
