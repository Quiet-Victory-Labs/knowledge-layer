"""Source freshness follows discovery, never the act of reviewing metadata."""

import os
import subprocess
import sys

import pytest

import knowledge_layer.store as store_module
from knowledge_layer.store import CurationFailed, KnowledgeStore


def test_calendar_has_packaged_timezone_data_without_system_database():
    process = subprocess.run([sys.executable, "-c",
        ("from datetime import datetime,UTC; from knowledge_layer.schedules import next_due; "
        "result=next_due({'cadence':'daily','timezone':'America/Chicago','hour':2,'minute':15}, "
        "datetime(2026,3,8,7,0,tzinfo=UTC)); assert result.isoformat()=='2026-03-09T07:15:00+00:00'")],
        env={**os.environ, "PYTHONTZPATH": ""}, capture_output=True, timeout=10, check=False)
    assert process.returncode == 0, process.stderr.decode()


def test_review_and_failure_do_not_advance_completed_discovery(tmp_path, monkeypatch):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    try:
        source = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
        assert source["last_refresh_attempt_at"] is None
        assert source["last_complete_refresh_at"] is None
        monkeypatch.setattr(store_module, "utc_now", lambda: "2026-10-03T10:00:00Z")
        snapshot = store.refresh_connection("demo", source["id"])["snapshot"]
        complete = store.get_connection("demo", source["id"])
        assert complete["last_refresh_attempt_at"] == complete["last_complete_refresh_at"] == "2026-10-03T10:00:00Z"
        concept = snapshot["concepts"][0]
        proposal = store.submit_proposal("demo", source["id"], target_concept_id=concept["id"],
            proposed_definition="Identifier for a source order record.", evidence_ids=concept["evidence_ids"],
            rationale="Review based on the observed source field.")
        monkeypatch.setattr(store_module, "utc_now", lambda: "2026-10-03T11:00:00Z")
        def failed(_kind, _config):
            raise RuntimeError("connector unavailable")
        with pytest.raises(CurationFailed):
            store.refresh_connection("demo", source["id"], curator=failed)
        store.decide_proposal("demo", source["id"], proposal["id"], decision="approve")
        after = store.get_connection("demo", source["id"])
        assert after["status"] == "error" and after["last_error"]
        assert after["snapshot"]["agent_ready"] is False
        assert after["last_refresh_attempt_at"] == "2026-10-03T11:00:00Z"
        assert after["last_complete_refresh_at"] == after["last_refreshed_at"] == "2026-10-03T10:00:00Z"
    finally:
        store.close()


@pytest.mark.parametrize("status", ["partial", "ready", "discovery_complete", "error"])
def test_explicit_incomplete_refresh_preserves_prior_complete_inventory(tmp_path, status):
    (tmp_path / "orders.csv").write_text("order_id\nO-1\n", encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    try:
        source = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
        before = store.refresh_connection("demo", source["id"])["connection"]
        after = store.refresh_connection("demo", source["id"], curator=lambda _kind, _config: {
            "status": status, "discovery_complete": False, "assets": [], "concepts": [],
        })["connection"]
        assert after["status"] == "error"
        assert after["snapshot"]["id"] == before["snapshot"]["id"]
        assert after["last_complete_refresh_at"] == before["last_complete_refresh_at"]
    finally:
        store.close()


def test_complete_empty_inventory_is_success_but_stored_metadata_has_no_source_clock(tmp_path):
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    try:
        source = store.create_connection("demo", kind="local_files", name="Empty", config={"path": "."})
        payload = {"status": "discovery_complete", "discovery_complete": True, "assets": [], "concepts": []}
        store.save_snapshot("demo", source["id"], payload)
        assert store.get_connection("demo", source["id"])["last_complete_refresh_at"] is None
        result = store.refresh_connection("demo", source["id"], curator=lambda _kind, _config: payload)
        assert result["connection"]["last_complete_refresh_at"] is not None
        assert result["snapshot"]["assets"] == []
    finally:
        store.close()


def test_complete_inventory_clock_is_separate_from_partial_curation(tmp_path):
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    try:
        source = store.create_connection("demo", kind="local_files", name="Partial curation", config={"path": "."})
        result = store.refresh_connection("demo", source["id"], curator=lambda _kind, _config: {
            "status": "partial", "discovery_complete": True, "curation_complete": False,
            "assets": [], "concepts": [], "errors": [{"code": "retention_unavailable"}],
        })
        assert result["connection"]["last_complete_refresh_at"] is not None
        assert result["connection"]["status"] == "partial"
        assert result["snapshot"]["agent_ready"] is False
    finally:
        store.close()


def test_missing_complete_flag_cannot_replace_previous_complete_inventory(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id\nO-1\n", encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    try:
        source = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
        before = store.refresh_connection("demo", source["id"])["snapshot"]
        result = store.refresh_connection("demo", source["id"], curator=lambda _kind, _config: {
            "status": "discovery_complete", "assets": [], "concepts": [],
        })
        assert result["connection"]["status"] == "error"
        assert result["snapshot"]["id"] == before["id"]
    finally:
        store.close()
