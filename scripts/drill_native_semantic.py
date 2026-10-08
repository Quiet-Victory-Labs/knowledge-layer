"""Qualify actual native artifact paging through disposable PostgreSQL workers.

Reads synthetic mounted dbt artifacts through the shipped HTTP sample. No dbt
service, DataHub deployment or customer credentials are used. Local synthetic
metric interpretations have separate owner review and steward publication.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

from drill_inventory import _census, _check, _cleanup_registry, _database_url, _write_report
from drill_inventory_refresh import (
    _enqueue,
    _environment,
    _job,
    _manager,
    _private_json,
    _source_state,
    _worker_environment,
    _worker_once,
)
from sqlalchemy import func, select

from knowledge_layer.access import Principal
from knowledge_layer.adapters.catalog import SCHEMA_VERSION
from knowledge_layer.adapters.catalog_mapping import binding_digest
from knowledge_layer.adapters.paging import NativeInventoryPager
from knowledge_layer.contracts import semantic_contract
from knowledge_layer.inventory import InventoryStore
from knowledge_layer.inventory_concepts import InventoryConcepts
from knowledge_layer.jobs import ACTIVE, RefreshQueue
from knowledge_layer.mcp import _contract_tool_response
from knowledge_layer.migrate import ensure_current_schema
from knowledge_layer.retrieval import search_knowledge
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeConflict, KnowledgeStore
from knowledge_layer.task_context import revalidate_context, task_context

ROOT = Path(__file__).resolve().parents[1]


def _sample():
    for name, filename in (("reference_adapter", "reference_adapter.py"),
                           ("native_semantic_sample", "native_semantic_adapter.py")):
        spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    module.NativeHandler.pager = NativeInventoryPager()
    return module


def _manifest(namespace):
    return {"metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                         "generated_at": "2026-01-01T00:00:00Z", "project_name": namespace,
                         "dbt_version": "1.10.0", "invocation_id": "00000000-0000-0000-0000-000000000001",
                         "env": {"PRIVATE_EXCLUDED": "fixture-canary"}},
            "metrics": {f"metric.{namespace}.revenue_{index}": {
                "unique_id": f"metric.{namespace}.revenue_{index}", "name": "Revenue",
                "resource_type": "metric", "package_name": namespace, "path": "metrics/revenue.yml",
                "original_file_path": "models/metrics/revenue.yml", "fqn": [namespace, "Revenue"],
                "label": "Revenue",
                "description": "Synthetic policy " + namespace + ":" + "x" * 5000,
                "type": "simple", "type_params": {"measure": {"name": "revenue"}},
                "config": {"meta": {"unit": "USD" if namespace == "finance" else "EUR",
                                    "unknown": None, "zero": 0, "empty": "", "false": False}},
                "depends_on": {"nodes": [f"semantic_model.{namespace}.ledger"]},
            } for index in range(101)}}


def _review_inventory_metric(report, store, tenant, owner, directory, provider, selected,
                             source_ids, policy, policy_file):
    """Review fixture meaning on a CSV snapshot, without certifying native metadata."""
    target_path = directory / (provider + "-ledger")
    target_path.mkdir(mode=0o700)
    field = "net_amount" if provider == "databricks" else "gross_amount"
    descriptor = os.open(target_path / "ledger.csv", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(f"order_id,{field}\n1,0\n2,10\n")
    config = {"path": target_path.name, "include": "*.csv"}
    policy["sources"].append({"tenant_id": tenant, "kind": "local_files", "selector": config,
                              "allowed_subjects": [owner.subject], "allowed_groups": []})
    policy_file.write_text(json.dumps(policy))
    store.source_policy = SourcePolicy.from_json(json.dumps(policy))
    target = store.create_connection(tenant, kind="local_files", name="Synthetic " + provider + " ledger", config=config)
    source_ids.append(target["id"])
    store.grant_source_access(tenant, target["id"], principal_type="user", principal_id=owner.subject, role="manage")
    entity, proof = selected["entities"][0], selected["evidence"][0]
    reference = {"connection_id": selected["connection_id"], "inventory_id": selected["inventory"]["id"],
                 "manifest_digest": selected["inventory"]["manifest_digest"], "entity_id": entity["id"], "evidence_id": proof["id"]}
    concepts = InventoryConcepts(store)
    with _manager(store, owner):
        local = concepts.propose(tenant, selected["connection_id"], evidence_ref=reference,
            name="Revenue", definition="Synthetic " + provider + " revenue uses its own scoped business basis.",
            concept_kind="metric_definition", rationale="Fixture owner interprets exact inventory metadata as local business meaning.")
        _check(report, provider + "_primary_inventory_concept_requires_local_review",
               local["publication_state"] == "unpublished" and store.get_context(tenant, concept_id=local["id"]) is None)
        store.decide_proposal(tenant, selected["connection_id"], local["id"], decision="approve")
        concepts.publish(tenant, selected["connection_id"], concept_id=local["id"],
                         expected_proposal_digest=local["proposal_digest"],
                         rationale="Fixture steward publishes only this reviewed local business meaning.")
        consumed = store.get_context(tenant, concept_id=local["id"])
        scoped = concepts.page(tenant, selected["connection_id"], inventory_id=selected["inventory"]["id"],
                               expected_manifest_digest=selected["inventory"]["manifest_digest"], query="Revenue")
        _check(report, provider + "_snapshotless_primary_concept_consumed_with_exact_scope",
               consumed["publication_state"] == "published" and consumed["evidence_ref"] == reference
               and consumed["concept"]["definition"] == local["concept"]["definition"]
               and scoped["items"][0]["id"] == local["id"] and "snapshot_id" not in consumed
               and not consumed["locally_executable"] and not consumed["native_meaning_certified"]
               and store.get_snapshot(tenant, selected["connection_id"]) is None)
        term = store.get_context(tenant, term="Revenue", connection_id=selected["connection_id"])
        found = search_knowledge(store, tenant_id=tenant, query="Revenue", limit=10)
        _check(report, provider + "_primary_inventory_term_and_global_search_preserve_proof",
               term["matches"][0]["id"] == local["id"] and term["matches"][0]["evidence_ref"] == reference
               and any(item["concept_id"] == local["id"] and item["context"]["evidence_ref"] == reference
                       and item["definition"] == local["concept"]["definition"] for item in found["results"]))
        selection = [{"connection_id": selected["connection_id"], "concept_ids": [local["id"]]}]
        inventory_context = task_context(store, tenant_id=tenant, query="Revenue",
            connection_ids=[selected["connection_id"]], inventory_selection=selection)
        inventory_current = revalidate_context(store, tenant_id=tenant, receipt=inventory_context["receipt"])
        _check(report, provider + "_inventory_only_signed_context_provides_meaning_without_execution",
               inventory_context["schema_version"] == "task-context-v2" and inventory_current["status"] == "current"
               and inventory_context["answerability"] == "business_meaning_available"
               and inventory_context["sources"][0]["basis"] == "inventory"
               and "snapshot_id" not in inventory_context["sources"][0]
               and not inventory_context["sources"][0]["source_agent_ready"]
               and not inventory_current["source_permissions_verified"])
    metric_name = "Fixture " + provider.title() + " Revenue"
    with _manager(store, owner):
        snapshot = store.refresh_connection(tenant, target["id"])["snapshot"]
        asset = snapshot["assets"][0]
        handle = next(item["id"] for item in snapshot["evidence"] if item.get("asset_id") == asset["id"]
                      and (item.get("details") or {}).get("field") == field)
        proposal = store.submit_contract_assertion(tenant, target["id"], assertion_kind="metric", asset_id=asset["id"],
            payload={"name": metric_name, "description": "Synthetic ledger revenue interpreted by its local owner.",
                     "measure_field": field, "aggregation": "sum", "unit": "USD" if provider == "databricks" else "EUR"},
            evidence_ids=[handle], evidence_refs=[reference], expected_snapshot_id=snapshot["id"],
            rationale="The local owner interprets an exact captured native observation against this fixture ledger.")
        _check(report, provider + "_inventory_metric_starts_pending_owner_review", proposal["status"] == "pending_review")
        store.decide_proposal(tenant, target["id"], proposal["id"], decision="approve",
                              rationale="The fixture owner reviewed the local measure and unit independently.")
        snapshot = store.get_snapshot(tenant, target["id"])
        store.publish_source(tenant, target["id"], expected_snapshot_id=snapshot["id"], expected_snapshot_version=snapshot["version"],
                             rationale="A fixture steward publishes the separately reviewed local interpretation.")
        compact = _contract_tool_response(semantic_contract(store, tenant_id=tenant, connection_id=target["id"]))
        citations = compact["cited_inventory_evidence"]
        _check(report, provider + "_postgres_reviewed_metric_retains_exact_raw_inventory_citation",
               compact["agent_ready"] and len(compact["metrics"]) == len(citations) == 1
               and compact["metrics"][0]["name"] == metric_name and citations[0]["evidence_ref"] == reference
               and citations[0]["entity_digest"] == entity["digest"] and citations[0]["evidence_digest"] == proof["digest"]
               and citations[0]["source_assertion"]["assertion_id"] == proposal["id"]
               and citations[0]["native_authority"] == "untrusted_evidence" and not citations[0]["meaning_certified"]
               and not citations[0]["locally_executable"] and "native_record" not in citations[0])
        context = task_context(store, tenant_id=tenant, query=metric_name, connection_ids=[target["id"]])
        current = revalidate_context(store, tenant_id=tenant, receipt=context["receipt"])
        _check(report, provider + "_reviewed_metric_task_receipt_revalidates",
               context["status"] == "context_available" and current["status"] == "current"
               and context["execution_state"] == "not_executed" and not context["rules_enforced"])
        integrated = task_context(store, tenant_id=tenant, query=metric_name,
            connection_ids=[selected["connection_id"], target["id"]], inventory_selection=selection)
        _check(report, provider + "_mixed_catalog_dataset_context_keeps_separate_authority",
               integrated["status"] == "context_available" and integrated["meaning_available"]
               and integrated["answerability"] == "requires_source_execution"
               and {item["basis"] for item in integrated["sources"]} == {"inventory", "snapshot"}
               and integrated["query_coverage"] == "not_established"
               and revalidate_context(store, tenant_id=tenant, receipt=integrated["receipt"])["status"] == "current")
    return {"connection_id": target["id"], "snapshot_id": snapshot["id"], "receipt": context["receipt"],
            "inventory_connection_id": selected["connection_id"], "inventory_concept_id": local["id"],
            "inventory_receipt": inventory_context["receipt"], "integrated_receipt": integrated["receipt"]}


def _assert_inventory_metric_invalidated(report, store, tenant, owner, provider, reviewed):
    with _manager(store, owner):
        source = store.get_connection(tenant, reviewed["connection_id"])
        contract = semantic_contract(store, tenant_id=tenant, connection_id=reviewed["connection_id"])
        _check(report, provider + "_retained_inventory_error_suspends_local_metric_and_publication",
               source["snapshot"]["id"] == reviewed["snapshot_id"]
               and not contract["metrics"] and not contract["cited_inventory_evidence"] and not contract["approved_assertions"]
               and not source["agent_ready"] and source["snapshot"]["publication"]["active"] is False)
        refused = False
        try:
            revalidate_context(store, tenant_id=tenant, receipt=reviewed["receipt"])
        except KnowledgeConflict:
            refused = True
        _check(report, provider + "_retained_inventory_error_invalidates_prior_task_receipt", refused)
        refused = False
        try:
            InventoryConcepts(store).get(tenant, reviewed["inventory_connection_id"], concept_id=reviewed["inventory_concept_id"])
        except KnowledgeConflict:
            refused = True
        _check(report, provider + "_retained_inventory_error_invalidates_primary_concept", refused)
        refused_receipts = 0
        for receipt in (reviewed["inventory_receipt"], reviewed["integrated_receipt"]):
            try:
                revalidate_context(store, tenant_id=tenant, receipt=receipt)
            except KnowledgeConflict:
                refused_receipts += 1
        _check(report, provider + "_retained_inventory_error_invalidates_inventory_and_mixed_receipts",
               refused_receipts == 2)


def _portable_exports(report, store, tenant, owner, directory, native_file, native_profile,
                      server, source_ids, nonce, policy, policy_file):
    """Use the same native sample/worker with arbitrary provider-neutral exports."""
    identities = []
    for provider, namespace in (("databricks", "finance_portable"), ("atlan", "studio_portable")):
        scope = {"provider": provider, "namespace": namespace, "source_identity": "synthetic." + provider,
                 "scope_id": namespace, "selectors": ["domain:" + namespace]}
        package = {"schema_version": SCHEMA_VERSION, "scope": scope,
            "observation": {"exported_at": "2026-10-03T00:00:00Z", "source_observed_at": None,
                "native_revision": 0, "field_coverage": {"metrics": "partial", "policies": "unknown"}, "redaction": "unknown"},
            "listing_complete": True, "next_cursor": None, "record_count": 101,
            "records": [{"native_id": str(index), "native_type": "NativeMetric", "kind": "semantic_definition",
                "name": "Revenue", "native_record": {"id": index, "name": "Revenue", "definition": "x" * 5000,
                    "expression": "sum(net_amount)" if provider == "databricks" else "sum(gross_amount)",
                    "unit": "USD" if provider == "databricks" else "EUR", "owner": "Fixture steward",
                    "policy_refs": ["unverified-native-policy"], "relationships": [{"id": "unresolved-native-asset"}],
                    "unknown": None, "zero": 0, "empty": "", "false": False}} for index in range(101)]}
        artifact_file = directory / (provider + "-export.json")
        _private_json(artifact_file, package)
        profile = {"endpoint": f"http://127.0.0.1:{server.server_port}/discover", "provider": provider,
            "protocol_version": "1.3", "allowed_scopes": [namespace], "revision": "portable.fixture.1",
            "source_identity": "portable." + provider + "." + nonce,
            "inventory_scope_bindings": {namespace: binding_digest(scope)}, "bearer_token": os.environ["ADAPTER_BEARER_TOKEN"]}
        native_profile[namespace] = {"provider": provider, "format": "catalog_export", "artifact": str(artifact_file),
            "expected_scope": scope, "platform_profile": {key: value for key, value in profile.items() if key != "bearer_token"}}
        native_file.write_text(json.dumps(native_profile))
        profile_file = directory / (provider + "-platform.json")
        _private_json(profile_file, profile)
        reference = "KNOWLEDGE_SOURCE_PORTABLE_" + nonce.upper() + "_" + provider.upper()
        config = {"secret_ref": reference, "scope": namespace, "discovery_mode": "inventory", "retain_inventory_payloads": True}
        policy["sources"].append({"tenant_id": tenant, "kind": "external_adapter", "selector": config,
                                  "allowed_subjects": [owner.subject], "allowed_groups": []})
        policy_file.write_text(json.dumps(policy))
        store.source_policy = SourcePolicy.from_json(json.dumps(policy))
        with _environment({reference + "_FILE": str(profile_file)}):
            source = store.create_connection(tenant, kind="external_adapter", name="Synthetic portable " + provider, config=config)
            source_ids.append(source["id"])
            store.grant_source_access(tenant, source["id"], principal_type="user", principal_id=owner.subject, role="manage")
            environment = _worker_environment(policy_file, reference, profile_file, nonce)
            queued = _enqueue(store, tenant, source["id"], owner)
            _worker_once(environment)
            job = _job(store, queued["id"])
            _check(report, provider + "_portable_actual_parent_child_worker", job["status"] == "succeeded"
                   and job["result_inventory_id"] is not None and job["result_snapshot_id"] is None)
            reader = InventoryStore(store)
            manifest = reader.status(tenant, source["id"], job["result_inventory_id"])
            _check(report, provider + "_portable_complete_capture", manifest["entity_count"] == manifest["evidence_count"] == 101
                   and manifest["page_count"] > 1, entities=101, evidence=101, pages=manifest["page_count"])
            item = reader.page(tenant, source["id"], manifest["id"], collection="entities")["items"][0]
            selected = reader.select(tenant, source["id"], manifest["id"], expected_manifest_digest=manifest["manifest_digest"],
                                     entity_ids=[item["id"]])
            proof = selected["evidence"][0]["payload"]
            _check(report, provider + "_portable_full_native_meaning_and_unknown_coverage",
                   len(proof["native_record"]["definition"]) == 5000
                   and proof["native_record"]["unit"] == ("USD" if provider == "databricks" else "EUR")
                   and proof["native_record"]["policy_refs"] == ["unverified-native-policy"]
                   and proof["native_record"]["relationships"] == [{"id": "unresolved-native-asset"}]
                   and proof["observation"]["source_observed_at"] is None
                   and proof["observation"]["field_coverage"]["policies"] == "unknown"
                   and not selected["agent_ready"] and not selected["meaning_certified"])
            identities.append(item["id"])
            reviewed = _review_inventory_metric(report, store, tenant, owner, directory, provider, selected,
                                                source_ids, policy, policy_file)
            before = _source_state(store, tenant, source["id"])
            # Keep the operator binding fixed while a producer substitutes an
            # account. Actual refresh must fail and preserve head/history/clocks.
            package["scope"] = {**scope, "source_identity": "substituted-account"}
            artifact_file.write_text(json.dumps(package))
            failed = _enqueue(store, tenant, source["id"], owner)
            _worker_once(environment)
            job = _job(store, failed["id"])
            after = _source_state(store, tenant, source["id"])
            _check(report, provider + "_portable_scope_substitution_preserves_head_and_clock",
                   job["status"] == "retry_wait" and job["result_inventory_id"] is None
                   and after[1:] == before[1:] and after[0]["status"] == "error")
            _assert_inventory_metric_invalidated(report, store, tenant, owner, provider, reviewed)
            with _manager(store, owner):
                RefreshQueue(store).cancel(tenant, failed["id"])
    _check(report, "portable_provider_accounts_keep_same_named_definitions_distinct", identities[0] != identities[1])


def qualify(report, *, output=None):
    raw_url, _ = _database_url()
    nonce = secrets.token_hex(6)
    tenant, source_ids = "native_semantic_" + nonce, []
    owner = Principal("native_owner_" + nonce, frozenset({"native-fixture-stewards"}), frozenset({"knowledge.sources.manage"}))
    store = server = thread = baseline = None
    report["fixture_ownership"] = {"tenant_id": tenant, "connection_ids": source_ids, "nonce": nonce}
    with tempfile.TemporaryDirectory(prefix="knowledge-native-semantic-") as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        try:
            store = KnowledgeStore(database_url=raw_url, local_files_root=directory, initialize_schema=False)
            store.source_admin_group = "native-fixture-stewards"
            ensure_current_schema(store)
            with store.engine.connect() as conn:
                active = conn.execute(select(func.count()).select_from(store.refresh_jobs).where(
                    store.refresh_jobs.c.status.in_(ACTIVE))).scalar_one()
                scheduled = conn.execute(select(func.count()).select_from(store.refresh_schedules).where(
                    store.refresh_schedules.c.paused.is_(False))).scalar_one()
            _check(report, "no_unrelated_active_worker_jobs", active == scheduled == 0)
            baseline = _census(store)
            native_profile = {}
            for namespace in ("finance", "studio"):
                artifact = _manifest(namespace)
                path = directory / f"{namespace}.json"
                _private_json(path, artifact)
                native_profile[namespace] = {"provider": "dbt", "namespace": namespace,
                    "manifest": str(path), "unique_ids": sorted(artifact["metrics"])}
            native_file = directory / "native-profile.json"
            _private_json(native_file, native_profile)
            sample = _sample()
            server = sample.BoundedHTTPServer(("127.0.0.1", 0), sample.NativeHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            token = secrets.token_hex(24)
            local_env = {"NATIVE_ADAPTER_PROFILE_FILE": str(native_file), "ADAPTER_BEARER_TOKEN": token,
                         "KNOWLEDGE_ALLOW_INSECURE_ADAPTER_ENDPOINTS": "1"}
            profiles, configs, approvals = {}, {}, []
            for namespace in native_profile:
                reference = "KNOWLEDGE_SOURCE_NATIVE_" + nonce.upper() + "_" + namespace.upper()
                file = directory / f"{namespace}-profile.json"
                _private_json(file, {"endpoint": f"http://127.0.0.1:{server.server_port}/discover",
                    "provider": "dbt", "protocol_version": "1.3", "revision": "synthetic.1",
                    "source_identity": namespace + "." + nonce, "allowed_scopes": [namespace], "bearer_token": token})
                config = {"secret_ref": reference, "scope": namespace, "discovery_mode": "inventory",
                          "retain_inventory_payloads": True}
                profiles[namespace], configs[namespace] = (reference, file), config
                local_env[reference + "_FILE"] = str(file)
                approvals.append({"tenant_id": tenant, "kind": "external_adapter", "selector": config,
                                  "allowed_subjects": [owner.subject], "allowed_groups": []})
            policy = {"version": 1, "sources": approvals}
            policy_file = directory / "policy.json"
            _private_json(policy_file, policy)
            with _environment(local_env):
                thread.start()
                store.source_policy = SourcePolicy.from_json(json.dumps(policy))
                identities, saved = [], {}
                for namespace, config in configs.items():
                    source = store.create_connection(tenant, kind="external_adapter", name="Synthetic " + namespace, config=config)
                    source_ids.append(source["id"])
                    if output:
                        _write_report(output, report)
                    store.grant_source_access(tenant, source["id"], principal_type="user", principal_id=owner.subject, role="manage")
                    queued = _enqueue(store, tenant, source["id"], owner)
                    reference, file = profiles[namespace]
                    environment = _worker_environment(policy_file, reference, file, nonce)
                    _worker_once(environment)
                    job = _job(store, queued["id"])
                    _check(report, namespace + "_actual_parent_child_inventory_result", job["status"] == "succeeded"
                           and job["result_inventory_id"] is not None and job["result_snapshot_id"] is None)
                    reader = InventoryStore(store)
                    manifest = reader.status(tenant, source["id"], job["result_inventory_id"])
                    _check(report, namespace + "_full_large_semantic_capture", manifest["status"] == "committed"
                           and manifest["entity_count"] == manifest["evidence_count"] == 101 and manifest["page_count"] > 1,
                           entities=101, evidence=101, pages=manifest["page_count"])
                    item = reader.page(tenant, source["id"], manifest["id"], collection="entities")["items"][0]
                    selected = reader.select(tenant, source["id"], manifest["id"],
                        expected_manifest_digest=manifest["manifest_digest"], entity_ids=[item["id"]])
                    proof = selected["evidence"][0]["payload"]
                    native = proof["native_record"]
                    _check(report, namespace + "_selected_full_native_meaning_readback",
                           native["name"] == "Revenue" and len(native["description"]) > 4000
                           and native["config"]["meta"] == {"unit": "USD" if namespace == "finance" else "EUR",
                               "unknown": None, "zero": 0, "empty": "", "false": False}
                           and native["depends_on"]["nodes"] == [f"semantic_model.{namespace}.ledger"]
                           and "env" not in proof["artifact_metadata"] and selected["text_complete"])
                    state = _source_state(store, tenant, source["id"])
                    _check(report, namespace + "_discovery_does_not_publish_or_certify_meaning",
                           not selected["meaning_certified"] and not selected["agent_ready"]
                           and not selected["source_reads_executed"] and state[0]["last_refreshed_at"] is None
                           and state[0]["last_complete_refresh_at"] is None and state[2:] == (0, 0))
                    identities.append(item["id"])
                    saved[namespace] = (source["id"], manifest, state, environment)
                _check(report, "same_named_two_unit_definitions_keep_distinct_native_identity", identities[0] != identities[1])
                # Artifact disappearance must fail this refresh and retain the
                # exact historical discovery head, never establish deletion.
                Path(native_profile["finance"]["manifest"]).unlink()
                source_id, _old_manifest, old_state, environment = saved["finance"]
                failed = _enqueue(store, tenant, source_id, owner)
                _worker_once(environment)
                job = _job(store, failed["id"])
                state = _source_state(store, tenant, source_id)
                _check(report, "missing_artifact_refuses_without_empty_success_or_new_head",
                       job["status"] == "retry_wait" and job["result_inventory_id"] is None
                       and state[1:] == old_state[1:] and state[0]["status"] == "error")
                with _manager(store, owner):
                    RefreshQueue(store).cancel(tenant, failed["id"])
                _portable_exports(report, store, tenant, owner, directory, native_file, native_profile,
                                  server, source_ids, nonce, policy, policy_file)
                report["qualification"] = {"actual_postgres_worker": True, "mounted_synthetic_dbt_artifacts": True,
                    "business_units": 2, "native_vendor_account": False, "native_acl": False,
                    "portable_catalog_export_families": 2,
                    "synthetic_local_metric_review_publication": True, "inventory_citation_invalidation": True,
                    "source_writes": 0, "source_sql": False, "meaning_certified": False, "model_calls": 0}
        finally:
            if server is not None:
                if thread is not None and thread.is_alive():
                    server.shutdown()
                    thread.join(5)
                server.server_close()
                _check(report, "owned_native_adapter_stopped", thread is None or not thread.is_alive())
            if store is not None:
                try:
                    for source_id in source_ids:
                        remaining = _cleanup_registry(store, tenant, source_id)
                        _check(report, "exact_native_fixture_rows_removed", not any(remaining.values()))
                    if baseline is not None:
                        _check(report, "all_registry_table_counts_restored", _census(store) == baseline)
                finally:
                    store.close()
