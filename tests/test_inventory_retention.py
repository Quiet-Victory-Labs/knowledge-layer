"""Offline local retention preserves current meaning and refuses uncertain cohorts."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select, update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.inventory import InventoryStore
from knowledge_layer.inventory_retention import InventoryRetention
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)

CUTOFF = "2026-01-01T00:00:00Z"
OLD = "2025-01-01T00:00:00Z"
SENTINEL = "private source text never belongs in a retention receipt"


@pytest.fixture
def registry(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'retention.db'}")
    source = store.create_connection("demo", kind="local_files", name="Offline fixture", config={})
    yield store, source["id"], InventoryStore(store), InventoryRetention(store)
    store.close()


def _run(registry, *, status="aborted", clock=OLD, connection=None, content=True):
    store, source, inventory, _retention = registry
    source = source if connection is None else connection
    run = inventory.begin("demo", source, scope={"namespace": "fixture", "selectors": ["fixture.*"]},
                          policy={"id": "metadata-only", "revision": None},
                          watermark={"kind": "unknown", "value": None},
                          limits={"max_pages": 10, "max_entities": 20,
                                  "max_evidence": 20, "max_total_bytes": 100000})
    if content:
        run = inventory.append("demo", source, run["id"], page_index=0, cursor=None, next_cursor=None,
                               entities=[{"id": "entity.1", "kind": "semantic_definition",
                                          "payload": {"definition": SENTINEL}, "evidence_ids": ["proof.1"]}],
                               evidence=[{"id": "proof.1", "kind": "document_excerpt",
                                          "payload": {"excerpt": SENTINEL}}],
                               scope_digest=run["scope_digest"], policy_digest=run["policy_digest"],
                               watermark_digest=run["watermark_digest"])
    if status == "aborted":
        inventory.abort("demo", source, run["id"])
    elif status == "committed":
        run = inventory.commit("demo", source, run["id"], completion={
            "listing_complete": True, "pages": run["page_count"], "entities": run["entity_count"],
            "evidence": run["evidence_count"], "manifest_digest": run["manifest_digest"]})
    with store.engine.begin() as conn:
        conn.execute(update(store.inventory_runs).where(store.inventory_runs.c.id == run["id"])
                     .values(created_at=clock))
    return run


def _state(store):
    with store.engine.connect() as conn:
        return {table.name: sorted(json.dumps(dict(row), sort_keys=True) for row in
                                 conn.execute(select(table)).mappings())
                for table in store.metadata.sorted_tables}


def _preview(registry, **changes):
    _store, source, _inventory, retention = registry
    return retention.preview("demo", source, created_before=CUTOFF, **changes)


def _apply(registry, report, **changes):
    _store, source, _inventory, retention = registry
    return retention.apply("demo", source, created_before=CUTOFF,
                           expected_preview_digest=report["preview_digest"], **changes)


def test_preview_is_content_free_exact_and_makes_no_writes(registry):
    store, _source, _inventory, _retention = registry
    run = _run(registry)
    before = _state(store)
    report = _preview(registry)
    assert _state(store) == before
    assert report["status"] == "ready" and report["operation"] == "preview"
    assert report["counts"] == {"runs": 1, "pages": 1, "entities": 1, "evidence": 1, "total_rows": 4}
    assert report["runs"][0]["inventory_id"] == run["id"]
    assert report["population_count"] == 1 and report["population_count_state"] == "exact"
    encoded = json.dumps(report)
    for private in (SENTINEL, "entity.1", "proof.1", "metadata-only", "namespace"):
        assert private not in encoded
    assert not report["source_reads_executed"] and not report["source_writes_executed"]
    assert not report["registry_writes_executed"] and not report["source_clocks_changed"]


def test_retention_refuses_incoming_refresh_inventory_result(registry):
    store, source, _inventory, _retention = registry
    run = _run(registry)
    queue = RefreshQueue(store)
    job = queue.enqueue("demo", source)
    with store.engine.begin() as conn:
        conn.execute(update(store.refresh_jobs).where(store.refresh_jobs.c.id == job["id"]).values(
            status="succeeded", result_inventory_id=run["id"]))
    before = _state(store)
    with pytest.raises(KnowledgeConflict, match="refresh result"):
        _preview(registry)
    assert _state(store) == before


def test_apply_deletes_exact_aborted_rows_preserves_all_other_tables_and_clocks(registry):
    store, source, _inventory, _retention = registry
    _run(registry, status="committed")
    _run(registry, status="committed")  # Previous comparable committed inventory must survive.
    _run(registry, status="staging")
    _run(registry, clock="2026-02-01T00:00:00Z")
    other = store.create_connection("demo", kind="local_files", name="Other fixture", config={})
    _run(registry, connection=other["id"])
    doomed = _run(registry)
    before = _state(store)
    result = _apply(registry, _preview(registry))
    after = _state(store)
    assert result["status"] == "applied" and result["registry_writes_executed"]
    for name in before:
        if name not in {store.inventory_runs.name, store.inventory_pages.name,
                        store.inventory_entities.name, store.inventory_evidence.name, store.audit_events.name}:
            assert after[name] == before[name], name
    for table in (store.inventory_runs, store.inventory_pages, store.inventory_entities, store.inventory_evidence):
        assert len(before[table.name]) - len(after[table.name]) == 1
    assert len(after[store.audit_events.name]) == len(before[store.audit_events.name]) + 1
    with store.engine.connect() as conn:
        audit = conn.execute(select(store.audit_events).where(
            store.audit_events.c.event_type == "inventory.retention")).mappings().one()
    assert audit["actor"] == "operator" and audit["connection_id"] == source
    assert audit["details"]["inventory_ids"] == [doomed["id"]]
    assert audit["details"]["counts"] == result["counts"]
    assert SENTINEL not in json.dumps(dict(audit))
    assert result["source_clocks_changed"] is False


def test_empty_is_exact_zero_and_apply_does_not_insert_audit(registry):
    store, _source, _inventory, _retention = registry
    before = _state(store)
    report = _preview(registry)
    assert report["status"] == "empty" and report["population_count"] == 0
    assert report["counts"]["total_rows"] == 0 and report["runs"] == []
    assert _apply(registry, report)["status"] == "empty"
    assert _state(store) == before


def test_zero_child_rows_are_valid_and_run_row_counts_toward_budget(registry):
    _run(registry, content=False)
    report = _preview(registry, max_rows=1)
    assert report["counts"] == {"runs": 1, "pages": 0, "entities": 0, "evidence": 0, "total_rows": 1}
    assert _apply(registry, report, max_rows=1)["status"] == "applied"


def test_bounded_cohort_can_progress_without_claiming_unknown_population_count(registry):
    _run(registry, clock="2024-01-01T00:00:00Z")
    _run(registry, clock="2024-02-01T00:00:00Z")
    _run(registry, clock="2024-03-01T00:00:00Z")
    first = _preview(registry, max_runs=2)
    assert first["has_more"] and first["population_count"] is None
    assert first["population_count_state"] == "unknown" and first["counts"]["runs"] == 2
    _apply(registry, first, max_runs=2)
    second = _preview(registry, max_runs=2)
    assert second["population_count"] == 1 and not second["has_more"]
    _apply(registry, second, max_runs=2)
    assert _preview(registry)["status"] == "empty"


@pytest.mark.parametrize("clock", ["2025-12-31T23:59:59Z", "2025-12-31T23:59:59.000001Z"])
def test_aware_cutoff_normalizes_timezone_and_strictly_excludes_equal_clock(registry, clock):
    store, source, _inventory, retention = registry
    _run(registry, clock=clock)
    before = _state(store)
    equal = retention.preview("demo", source, created_before=clock)
    assert equal["status"] == "empty"
    normalized = retention.preview("demo", source, created_before="2026-01-01T01:00:00+01:00")
    assert normalized["counts"]["runs"] == 1 and normalized["created_before"] == CUTOFF
    assert _state(store) == before


def test_zero_microsecond_clock_sorts_before_nonzero_in_same_second(registry):
    zero = _run(registry, clock="2025-01-01T00:00:00Z")
    _run(registry, clock="2025-01-01T00:00:00.000001Z")
    report = _preview(registry, max_runs=1)
    assert report["runs"][0]["inventory_id"] == zero["id"]


@pytest.mark.parametrize("clock", ["unknown", "2025-99-01T00:00:00Z", "2025-01-01T00:00:00+00:00",
                                   "2025-01-01T00:00:00.000000Z", "2025-01-01T00:00:00.1Z"])
def test_invalid_persisted_clocks_refuse_without_deleting(registry, clock):
    store, _source, _inventory, _retention = registry
    _run(registry, clock=clock)
    before = _state(store)
    with pytest.raises(KnowledgeConflict, match="clock"):
        _preview(registry)
    assert _state(store) == before


@pytest.mark.parametrize("changes", [{"max_runs": True}, {"max_runs": 0}, {"max_runs": 21},
                                     {"max_rows": False}, {"max_rows": 0}, {"max_rows": 10001}])
def test_invalid_budgets_refuse(registry, changes):
    with pytest.raises(KnowledgeValidationError):
        _preview(registry, **changes)


@pytest.mark.parametrize("cutoff", [None, True, "2026-01-01", datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)])
def test_missing_or_naive_cutoff_refuses(registry, cutoff):
    _store, source, _inventory, retention = registry
    with pytest.raises(KnowledgeValidationError):
        retention.preview("demo", source, created_before=cutoff)


@pytest.mark.parametrize("tenant,source", [(None, "source"), ("", "source"), ("demo", ""),
                                         ("demo", "source\n"), ("demo", 1)])
def test_explicit_tenant_and_source_are_required(registry, tenant, source):
    _store, _source, _inventory, retention = registry
    with pytest.raises(KnowledgeValidationError):
        retention.preview(tenant, source, created_before=CUTOFF)


def test_unknown_source_is_not_empty(registry):
    _store, _source, _inventory, retention = registry
    with pytest.raises(KnowledgeNotFound):
        retention.preview("demo", "missing", created_before=CUTOFF)


@pytest.mark.parametrize("managed", [False, True])
def test_managed_request_principal_cannot_run_offline_retention(registry, managed):
    store, source, _inventory, retention = registry
    _run(registry)
    report = _preview(registry)
    store.access_enforced = managed
    with principal_scope(Principal("operator", frozenset(), frozenset({"knowledge.sources.manage"}))):
        with pytest.raises(KnowledgeAccessDenied):
            retention.preview("demo", source, created_before=CUTOFF)
        with pytest.raises(KnowledgeAccessDenied):
            retention.apply("demo", source, created_before=CUTOFF, expected_preview_digest=report["preview_digest"])


def test_access_enforced_without_principal_is_also_refused(registry):
    store, _source, _inventory, _retention = registry
    store.access_enforced = True
    with pytest.raises(KnowledgeAccessDenied):
        _preview(registry)


def test_withdrawn_source_approval_does_not_prevent_local_cleanup(registry):
    store, _source, _inventory, _retention = registry
    _run(registry)
    report = _preview(registry)
    store.source_policy = SourcePolicy.from_json('{"version":1,"sources":[]}')
    assert _apply(registry, report)["status"] == "applied"


@pytest.mark.parametrize("status", ["queued", "running", "retry_wait", "unknown"])
def test_active_expired_cancel_requested_or_unknown_job_is_not_quiescent(registry, status):
    store, source, _inventory, _retention = registry
    _run(registry)
    job = RefreshQueue(store).enqueue("demo", source)
    with store.engine.begin() as conn:
        conn.execute(update(store.refresh_jobs).where(store.refresh_jobs.c.id == job["id"])
                     .values(status=status, lease_until="2000-01-01T00:00:00Z", cancel_requested=True))
    before = _state(store)
    with pytest.raises(KnowledgeConflict):
        _preview(registry)
    assert _state(store) == before


def test_terminal_source_job_does_not_block_local_cleanup(registry):
    store, source, _inventory, _retention = registry
    _run(registry)
    queue = RefreshQueue(store)
    job = queue.enqueue("demo", source)
    queue.cancel("demo", job["id"])
    assert _apply(registry, _preview(registry))["status"] == "applied"


@pytest.mark.parametrize("reference", ["head", "previous"])
def test_corrupt_aborted_current_or_previous_reference_blocks_deletion(registry, reference):
    store, _source, _inventory, _retention = registry
    _run(registry, status="committed")
    aborted = _run(registry)
    with store.engine.begin() as conn:
        if reference == "head":
            conn.execute(update(store.inventory_heads).values(inventory_id=aborted["id"]))
        else:
            staged = _run(registry, status="staging")
            conn.execute(update(store.inventory_runs).where(store.inventory_runs.c.id == staged["id"])
                         .values(previous_inventory_id=aborted["id"], tenant_id="foreign"))
    before = _state(store)
    with pytest.raises(KnowledgeConflict, match="referenced"):
        _preview(registry)
    assert _state(store) == before


def test_actual_rows_cannot_evade_budget_using_stale_counters(registry):
    store, _source, _inventory, _retention = registry
    run = _run(registry)
    with store.engine.begin() as conn:
        original = dict(conn.execute(select(store.inventory_entities)).mappings().one())
        conn.execute(insert(store.inventory_entities).values(**{**original, "id": "unexpected.row"}))
        conn.execute(update(store.inventory_runs).where(store.inventory_runs.c.id == run["id"])
                     .values(entity_count=1))
    before = _state(store)
    with pytest.raises(KnowledgeConflict, match="Actual inventory rows"):
        _preview(registry, max_rows=4)
    with pytest.raises(KnowledgeConflict, match="counters"):
        _preview(registry, max_rows=5)
    assert _state(store) == before


@pytest.mark.parametrize("column,value", [("page_count", -1), ("entity_count", 0), ("byte_count", 0)])
def test_invalid_or_inconsistent_run_counters_refuse(registry, column, value):
    store, _source, _inventory, _retention = registry
    run = _run(registry)
    with store.engine.begin() as conn:
        conn.execute(update(store.inventory_runs).where(store.inventory_runs.c.id == run["id"])
                     .values(**{column: value}))
    with pytest.raises(KnowledgeConflict, match="counters"):
        _preview(registry)


def test_child_digest_and_cohort_change_invalidate_preview(registry):
    store, _source, _inventory, _retention = registry
    _run(registry)
    report = _preview(registry)
    with store.engine.begin() as conn:
        conn.execute(update(store.inventory_entities).values(digest="a" * 64))
    before = _state(store)
    with pytest.raises(KnowledgeConflict, match="preview changed"):
        _apply(registry, report)
    assert _state(store) == before
    report = _preview(registry)
    _run(registry, clock="2024-01-01T00:00:00Z")
    before = _state(store)
    with pytest.raises(KnowledgeConflict, match="preview changed"):
        _apply(registry, report)
    assert _state(store) == before


@pytest.mark.parametrize("table_name", ["inventory_pages", "inventory_entities", "inventory_evidence"])
def test_invalid_child_digest_refuses_without_deleting(registry, table_name):
    store, _source, _inventory, _retention = registry
    _run(registry)
    table = getattr(store, table_name)
    with store.engine.begin() as conn:
        conn.execute(update(table).values(digest="unknown"))
    before = _state(store)
    with pytest.raises(KnowledgeConflict, match="digest"):
        _preview(registry)
    assert _state(store) == before


def test_child_with_nonexistent_page_refuses(registry):
    store, _source, _inventory, _retention = registry
    _run(registry)
    with store.engine.begin() as conn:
        conn.execute(update(store.inventory_entities).values(page_index=7))
    with pytest.raises(KnowledgeConflict, match="page references"):
        _preview(registry)


def test_page_counters_must_match_their_own_children_not_just_run_totals(registry):
    store, source, inventory, _retention = registry
    run = inventory.begin("demo", source, scope={"namespace": "fixture", "selectors": ["fixture.*"]},
                          policy={"id": "metadata-only", "revision": None},
                          watermark={"kind": "unknown", "value": None},
                          limits={"max_pages": 10, "max_entities": 20,
                                  "max_evidence": 20, "max_total_bytes": 100000})
    for index in range(2):
        run = inventory.append("demo", source, run["id"], page_index=index,
                               cursor=None if index == 0 else "second", next_cursor="second" if index == 0 else None,
                               entities=[{"id": f"entity.{index}", "kind": "semantic_definition",
                                          "payload": {"definition": SENTINEL}, "evidence_ids": []}], evidence=[],
                               scope_digest=run["scope_digest"], policy_digest=run["policy_digest"],
                               watermark_digest=run["watermark_digest"])
    inventory.abort("demo", source, run["id"])
    with store.engine.begin() as conn:
        conn.execute(update(store.inventory_runs).where(store.inventory_runs.c.id == run["id"])
                     .values(created_at=OLD))
        conn.execute(update(store.inventory_entities).values(page_index=0))
    with pytest.raises(KnowledgeConflict, match="page counters"):
        _preview(registry)


def test_active_job_after_preview_blocks_apply_without_consuming_digest(registry):
    store, source, _inventory, _retention = registry
    _run(registry)
    report = _preview(registry)
    queue = RefreshQueue(store)
    job = queue.enqueue("demo", source)
    before = _state(store)
    with pytest.raises(KnowledgeConflict, match="active refresh jobs"):
        _apply(registry, report)
    assert _state(store) == before
    queue.cancel("demo", job["id"])
    assert _apply(registry, report)["status"] == "applied"


def test_new_predecessor_reference_after_preview_blocks_apply(registry):
    store, source, inventory, _retention = registry
    doomed = _run(registry)
    report = _preview(registry)
    newer = inventory.begin("demo", source, scope={"namespace": "new", "selectors": ["fixture.*"]},
                            policy={"id": "metadata-only", "revision": None},
                            watermark={"kind": "unknown", "value": None},
                            limits={"max_pages": 10, "max_entities": 20,
                                    "max_evidence": 20, "max_total_bytes": 100000})
    with store.engine.begin() as conn:
        conn.execute(update(store.inventory_runs).where(store.inventory_runs.c.id == newer["id"])
                     .values(previous_inventory_id=doomed["id"]))
    before = _state(store)
    with pytest.raises(KnowledgeConflict, match="predecessor"):
        _apply(registry, report)
    assert _state(store) == before


@pytest.mark.parametrize("digest", [None, "", "a" * 63, "A" * 64, True])
def test_apply_requires_an_exact_lowercase_sha256_preview_digest(registry, digest):
    _store, source, _inventory, retention = registry
    with pytest.raises(KnowledgeValidationError):
        retention.apply("demo", source, created_before=CUTOFF, expected_preview_digest=digest)


def test_changed_cutoff_budget_or_replayed_apply_refuses(registry):
    _store, source, _inventory, retention = registry
    _run(registry)
    report = _preview(registry)
    with pytest.raises(KnowledgeConflict, match="preview changed"):
        _apply(registry, report, max_rows=1000)
    with pytest.raises(KnowledgeConflict, match="preview changed"):
        retention.apply("demo", source, created_before="2027-01-01T00:00:00Z",
                        expected_preview_digest=report["preview_digest"])
    _apply(registry, report)
    with pytest.raises(KnowledgeConflict, match="preview changed"):
        _apply(registry, report)


def test_oversized_clock_population_refuses_before_child_reads(registry, monkeypatch):
    import knowledge_layer.inventory_retention as module

    monkeypatch.setattr(module, "MAX_CLOCK_ROWS", 2)
    for _ in range(3):
        _run(registry, content=False)
    with pytest.raises(KnowledgeConflict, match="clock scan"):
        _preview(registry)


def test_audit_failure_rolls_back_all_deleted_children_and_runs(registry, monkeypatch):
    store, _source, _inventory, _retention = registry
    _run(registry)
    report = _preview(registry)
    before = _state(store)

    def fail(*_args, **_kwargs):
        raise RuntimeError("offline audit failure")

    monkeypatch.setattr(store, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        _apply(registry, report)
    assert _state(store) == before


def test_cli_defaults_preview_uses_secret_loader_and_never_auto_migrates(registry, monkeypatch, capsys):
    from knowledge_layer import inventory_retention_cli as cli

    store, source, _inventory, _retention = registry
    _run(registry)
    calls = []

    def secret(name):
        calls.append(name)
        return "offline-registry-config"

    def factory(**kwargs):
        assert kwargs == {"database_url": "offline-registry-config", "initialize_schema": False}
        return store

    monkeypatch.setattr(cli, "operator_secret", secret)
    monkeypatch.setattr(cli, "KnowledgeStore", factory)
    monkeypatch.setattr(cli, "ensure_current_schema", lambda candidate: calls.append(candidate))
    monkeypatch.setattr(store, "close", lambda: None)
    before = _state(store)
    assert cli.main(["--tenant", "demo", "--connection", source, "--created-before", CUTOFF]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["operation"] == "preview" and report["status"] == "ready"
    assert calls == ["DATABASE_URL", store] and _state(store) == before


@pytest.mark.parametrize("error", [RuntimeError(SENTINEL), ValueError(SENTINEL), OSError(SENTINEL),
                                   KnowledgeConflict(SENTINEL)])
def test_cli_refuses_without_rendering_arbitrary_exception_content(monkeypatch, capsys, error):
    from knowledge_layer import inventory_retention_cli as cli

    def fail(_args):
        raise error

    monkeypatch.setattr(cli, "_run", fail)
    assert cli.main(["--tenant", "demo", "--connection", "source", "--created-before", CUTOFF]) == 2
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    report = json.loads(captured.out)
    assert report["status"] == "refused" and report["registry_writes_executed"] is None


@pytest.mark.parametrize("extra", [["--apply"], ["--expected-preview-digest", "a" * 64],
                                   ["--unknown-option", SENTINEL], ["--max-rows", SENTINEL]])
def test_cli_invalid_arguments_never_echo_input(extra, capsys):
    from knowledge_layer.inventory_retention_cli import main

    with pytest.raises(SystemExit) as error:
        main(["--tenant", "demo", "--connection", "source", "--created-before", CUTOFF, *extra])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    assert json.loads(captured.out)["error_code"] == "invalid_arguments"


def test_private_output_refuses_existing_symlink_relative_path_and_sets_0600(tmp_path):
    from knowledge_layer.inventory_retention_cli import _write_private

    output = tmp_path / "receipt.json"
    _write_private(output, {"status": "ready"})
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        _write_private(output, {"status": "changed"})
    link = tmp_path / "link.json"
    link.symlink_to(output)
    with pytest.raises(OSError):
        _write_private(link, {"status": "changed"})
    with pytest.raises(ValueError):
        _write_private(output.relative_to(tmp_path), {})
    assert json.loads(output.read_text()) == {"status": "ready"}


@pytest.mark.parametrize("destination", ["existing", "symlink", "relative"])
def test_cli_reserves_output_before_any_registry_operation(tmp_path, monkeypatch, capsys, destination):
    from knowledge_layer import inventory_retention_cli as cli

    existing = tmp_path / "existing.json"
    existing.write_text(SENTINEL)
    if destination == "existing":
        output = existing
    elif destination == "symlink":
        output = tmp_path / "linked.json"
        output.symlink_to(existing)
    else:
        output = existing.relative_to(tmp_path)
    called = []
    monkeypatch.setattr(cli, "_run", lambda args: called.append(args))
    assert cli.main(["--tenant", "demo", "--connection", "source", "--created-before", CUTOFF,
                     "--apply", "--expected-preview-digest", "a" * 64, "--output", str(output)]) == 2
    assert called == []
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["registry_writes_executed"] is False and report["status"] == "refused"
    assert report["receipt_file_written"] is False
    assert SENTINEL not in captured.out + captured.err
    assert existing.read_text() == SENTINEL


def _apply_through_cli(registry, monkeypatch):
    from knowledge_layer import inventory_retention_cli as cli

    _store, source, _inventory, retention = registry
    report = _preview(registry)

    def run(args):
        return retention.apply(args.tenant, args.connection, created_before=args.created_before,
                               expected_preview_digest=args.expected_preview_digest,
                               max_runs=args.max_runs, max_rows=args.max_rows)

    monkeypatch.setattr(cli, "_run", run)
    arguments = ["--tenant", "demo", "--connection", source, "--created-before", CUTOFF,
                 "--apply", "--expected-preview-digest", report["preview_digest"]]
    return cli, arguments


@pytest.mark.parametrize("phase", ["write", "fsync"])
def test_post_commit_output_failure_preserves_actual_applied_stdout_receipt(
        registry, tmp_path, monkeypatch, capsys, phase):
    store, _source, _inventory, _retention = registry
    _run(registry)
    cli, arguments = _apply_through_cli(registry, monkeypatch)
    output = tmp_path / "application.json"

    def fail(*_args):
        raise OSError(SENTINEL)

    monkeypatch.setattr(cli if phase == "write" else cli.os,
                        "_write_reserved" if phase == "write" else "fsync", fail)
    assert cli.main([*arguments, "--output", str(output)]) == 1
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    report = json.loads(captured.out)
    assert report["status"] == "applied" and report["registry_writes_executed"] is True
    assert report["counts"]["total_rows"] == 4 and report["receipt_file_written"] is False
    assert report["receipt_file_error"] == "write_failed"
    assert _preview(registry)["status"] == "empty"
    with store.engine.connect() as conn:
        assert len(conn.execute(select(store.audit_events).where(
            store.audit_events.c.event_type == "inventory.retention")).all()) == 1
    assert output.exists() and stat.S_IMODE(output.stat().st_mode) == 0o600
    if phase == "fsync":
        assert json.loads(output.read_text())["status"] == "applied"


@pytest.mark.parametrize("fail_after_replace", [False, True])
def test_replaced_output_is_never_unlinked_and_does_not_misreport_registry_result(
        registry, tmp_path, monkeypatch, capsys, fail_after_replace):
    _run(registry)
    cli, arguments = _apply_through_cli(registry, monkeypatch)
    output = tmp_path / "application.json"
    reserved = tmp_path / "reserved.json"
    writer = cli._write_reserved

    def replace(descriptor, report):
        writer(descriptor, report)
        output.rename(reserved)
        output.write_text(SENTINEL)
        if fail_after_replace:
            raise OSError(SENTINEL)

    monkeypatch.setattr(cli, "_write_reserved", replace)
    assert cli.main([*arguments, "--output", str(output)]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["status"] == "applied" and report["registry_writes_executed"] is True
    assert report["receipt_file_written"] is False
    assert report["receipt_file_error"] == ("write_failed" if fail_after_replace else "path_changed")
    assert output.read_text() == SENTINEL and reserved.exists()
    assert SENTINEL not in captured.out + captured.err


def test_successful_cli_output_uses_reserved_private_descriptor_and_reports_saved(
        registry, tmp_path, monkeypatch, capsys):
    _run(registry)
    cli, arguments = _apply_through_cli(registry, monkeypatch)
    output = tmp_path / "application.json"
    assert cli.main([*arguments, "--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "applied" and report["receipt_file_written"] is True
    assert report["receipt_file_error"] is None
    persisted = json.loads(output.read_text())
    assert persisted["counts"] == report["counts"] and persisted["status"] == "applied"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_datetime_cutoff_is_accepted_without_host_timezone_assumptions(registry):
    _store, source, _inventory, retention = registry
    _run(registry)
    assert retention.preview("demo", source, created_before=datetime(2026, 1, 1, tzinfo=UTC))["counts"]["runs"] == 1
