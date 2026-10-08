"""Inspect distributions and smoke the wheel in an isolated base-only runtime.

Produces checksums and a local build receipt. Neither artifact signatures nor
release publication are implied by this verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

BASE_SMOKE = """import json
import hashlib
import io
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from sqlalchemy import func, select, update
import knowledge_layer.builds as builds_module
import knowledge_layer.build_transport as build_transport_module
import knowledge_layer.build_worker as build_worker_module
import knowledge_layer.iap as iap_module
import knowledge_layer.registry_bootstrap as registry_bootstrap_module
import knowledge_layer.origin as origin_module
import knowledge_layer.local_auth as local_auth_module
import knowledge_layer.local_profile as local_profile_module
import knowledge_layer.setup as setup_module
import knowledge_layer.source_permissions as source_permissions_module
import knowledge_layer.connector_isolation as connector_isolation_module
import knowledge_layer.composite_metrics as composite_metrics_module
import knowledge_layer.review_batches as review_batches_module
import knowledge_layer.store as store_module
import knowledge_layer.rdf_validation as rdf_validation_module
import knowledge_layer.adapters.conformance as conformance_module
import knowledge_layer.adapters.dbt as dbt_module
import knowledge_layer.adapters.datahub as datahub_module
import knowledge_layer.adapters.paging as native_paging_module
import knowledge_layer.adapters.catalog as catalog_module
import knowledge_layer.adapters.catalog_mapping as catalog_mapping_module
import knowledge_layer.adapters.catalog_cli as catalog_cli_module
import knowledge_layer.inventory as inventory_module
import knowledge_layer.inventory_citations as inventory_citations_module
import knowledge_layer.inventory_concepts as inventory_concepts_module
import knowledge_layer.inventory_concept_search as inventory_concept_search_module
import knowledge_layer.task_context as task_context_module
import knowledge_layer.inventory_retention as retention_module
import knowledge_layer.inventory_retention_cli as retention_cli_module
import knowledge_layer.inventory_refresh as inventory_refresh_module
import knowledge_layer.connectors.external_inventory as external_inventory_module
import knowledge_layer.migrations.versions.v0014_inventory_results as inventory_results_migration
import knowledge_layer.migrations.versions.v0015_inventory_concepts as inventory_concepts_migration
from knowledge_layer.connectors.external_adapter import normalize_response
from knowledge_layer.store import KnowledgeError, KnowledgeStore
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.capabilities import capability_manifest
from knowledge_layer.inventory import InventoryStore, inventory_digest

for module in (builds_module, build_transport_module, build_worker_module, store_module, conformance_module, inventory_module, retention_module, retention_cli_module,
               iap_module, registry_bootstrap_module, source_permissions_module,
               origin_module, local_auth_module, local_profile_module, setup_module,
               connector_isolation_module, composite_metrics_module, review_batches_module,
               inventory_citations_module, inventory_concepts_module, inventory_concepts_migration,
               inventory_concept_search_module, task_context_module,
               inventory_refresh_module, external_inventory_module, inventory_results_migration,
               dbt_module, datahub_module, native_paging_module,
               catalog_module, catalog_mapping_module, catalog_cli_module):
    assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), 'Import is not installed in isolated runtime'
assert inventory_results_migration.revision == '0014_inventory_results'
assert origin_module.normalize_origin('https://EXAMPLE.org:443/') == 'https://example.org'
assert local_auth_module.OWNER == 'local-owner' and local_auth_module.AGENT == 'local-agent'
assert inventory_results_migration.down_revision == '0013_autonomy'
assert inventory_concepts_migration.revision == '0015_inventory_concepts'
assert inventory_concepts_migration.down_revision == '0014_inventory_results'
native_manifest = {'metadata': {'dbt_schema_version': dbt_module.SCHEMA, 'generated_at': None},
    'metrics': {f'metric.synthetic.m{i}': {'unique_id': f'metric.synthetic.m{i}', 'name': 'Revenue',
        'resource_type': 'metric', 'package_name': 'synthetic', 'path': 'metrics/revenue.yml',
        'original_file_path': 'models/metrics/revenue.yml', 'fqn': ['synthetic', 'Revenue'],
        'label': 'Revenue', 'type': 'simple', 'type_params': {'measure': {'name': 'revenue'}},
        'description': 'é' * 2500, 'config': {'meta': {'zero': 0, 'unknown': None, 'empty': '', 'false': False}}}
        for i in range(101)}}
native_capture = dbt_module.inventory_records(json.dumps(native_manifest).encode('utf-8'),
    namespace='synthetic', unique_ids=list(native_manifest['metrics']))
assert native_capture['entities'][0]['payload']['source_clock_state'] == 'unknown'
assert native_capture['evidence'][0]['payload']['native_record']['config']['meta'] == {
    'zero': 0, 'unknown': None, 'empty': '', 'false': False}
catalog_contract = {'scope': {'provider': 'synthetic', 'source_identity': 'fixture-account',
        'namespace': 'finance', 'scope_id': 'finance', 'selectors': ['container:finance']},
    'observation': {'exported_at': None, 'source_observed_at': None, 'native_revision': None,
        'field_coverage': {'definitions': 'complete', 'policies': 'unknown'}, 'redaction': 'unknown'},
    'listing_complete': True, 'next_cursor': None, 'record_count': 1}
catalog_mapping = {'records_pointer': '/records', 'id_pointer': '/id', 'id_encoding': 'integer_decimal',
    'type_pointer': None, 'native_type': 'term', 'name_pointer': '/name',
    'kind_by_type': {'term': 'semantic_definition'}}
catalog_native = {'id': 0, 'name': 'Revenue', 'definition': 'Complete synthetic definition.',
    'unknown': None, 'empty': '', 'false': False, 'native_approval': 'approved',
    'unresolved_lineage': ['tables/unknown']}
catalog_raw = json.dumps({'records': [catalog_native]}, allow_nan=False).encode('utf-8')
catalog_export = catalog_mapping_module.map_export(catalog_raw, contract=catalog_contract, mapping=catalog_mapping)
catalog_capture = catalog_module.inventory_records(catalog_export, expected_scope=catalog_contract['scope'])
assert catalog_capture['evidence'][0]['payload']['native_record'] == catalog_native
assert catalog_capture['entities'][0]['payload']['native_id'] == '0'
assert catalog_capture['entities'][0]['payload']['locally_executable'] is False
assert catalog_capture['entities'][0]['payload']['source_clock_state'] == 'unknown'
with tempfile.TemporaryDirectory(prefix='knowledge-installed-catalog-') as catalog_directory:
    catalog_paths = {}
    for key, data in (('input', catalog_raw), ('contract', json.dumps(catalog_contract).encode('utf-8')),
                      ('mapping', json.dumps(catalog_mapping).encode('utf-8'))):
        catalog_path = Path(catalog_directory) / (key + '.json')
        catalog_path.write_bytes(data)
        catalog_paths[key] = str(catalog_path)
    catalog_output = io.StringIO()
    with redirect_stdout(catalog_output):
        assert catalog_cli_module.main(['check', '--input', catalog_paths['input'],
            '--contract', catalog_paths['contract'], '--mapping', catalog_paths['mapping']]) == 0
    catalog_check = json.loads(catalog_output.getvalue())
    assert catalog_check['result'] == 'passed' and catalog_check['counts'] == {'entities': 1, 'evidence': 1}
    assert catalog_check['input_sha256'] == hashlib.sha256(catalog_raw).hexdigest()
    assert catalog_check['export_sha256'] == hashlib.sha256(catalog_export).hexdigest()
    assert catalog_check['qualification'] == {'wire_contract': True, 'vendor_account': False,
        'clock_live_validated': False,
        'source_completeness': False, 'native_acl': False, 'business_meaning': False, 'publication': False}
    assert catalog_check['execution'] == {'network_calls': 0, 'registry_writes': 0, 'source_writes': 0, 'model_calls': 0}
    assert 'Complete synthetic definition.' not in catalog_output.getvalue()
    assert Path(catalog_paths['input']).read_bytes() == catalog_raw
now = datetime.now(UTC)
native_request = external_inventory_module.make_request(provider='dbt', scope='synthetic',
    profile_digest='a' * 64, scan_id='packaged.native.scan', page_index=0, cursor=None,
    scan_started_at=(now - timedelta(seconds=1)).isoformat().replace('+00:00', 'Z'),
    scan_expires_at=(now + timedelta(seconds=299)).isoformat().replace('+00:00', 'Z'),
    inventory_limits={'max_pages': 10, 'max_entities': 101, 'max_evidence': 101, 'max_total_bytes': 1048576})
native_pager = native_paging_module.NativeInventoryPager()
native_page = native_pager.page(native_request, operator_digest='b' * 64, load_records=lambda: native_capture)
native_entities, native_proofs = [], []
while True:
    native_entities += native_page['entities']
    native_proofs += native_page['evidence']
    if native_page['listing_complete']:
        break
    native_request = native_paging_module.continuation_request(native_request, native_page)
    native_page = native_pager.page(native_request, operator_digest='b' * 64, load_records=lambda: None)
assert native_entities == native_capture['entities'] and native_proofs == native_capture['evidence']
request = external_inventory_module.make_request(provider='synthetic', scope='readings',
    profile_digest=inventory_digest({'synthetic_profile': True}), scan_id='packaged.synthetic.scan',
    page_index=0, cursor=None, scan_started_at='2000-01-01T00:00:00Z',
    scan_expires_at='2000-01-01T00:05:00Z',
    inventory_limits={'max_pages': 2, 'max_entities': 1, 'max_evidence': 1, 'max_total_bytes': 16384})
entity_page = {'id': 'synthetic.entity', 'kind': 'asset', 'evidence_ids': ['synthetic.proof'],
               'payload': {'native_version': None, 'observed_zero': 0, 'text': 'Complete synthetic page text.'}}
proof_page = {'id': 'synthetic.proof', 'kind': 'source_observation',
              'payload': {'source_observed_at': None}}
for complete in (False, True):
    page = {key: value for key, value in request.items()
            if key not in {'inventory_limits', 'max_entities', 'max_evidence', 'max_response_bytes'}}
    page.update(next_cursor=None if complete else 'synthetic.continuation', listing_complete=complete,
                watermark={'kind': 'unknown', 'value': None}, entities=[entity_page], evidence=[proof_page])
    page_wire = json.dumps(page, allow_nan=False).encode('utf-8')
    assert external_inventory_module.validate_inventory_page(page_wire, request) == page
    checked_page = conformance_module.check_inventory_page(page_wire, request)
    assert checked_page['result'] == 'passed' and checked_page['protocol_version'] == '1.3'
    assert checked_page['counts_scope'] == 'supplied_page'
    assert checked_page['counts'] == {'entities': 1, 'evidence': 1}
    assert checked_page['listing']['state'] == ('declared_terminal_page' if complete else 'continuing_page')
    assert checked_page['listing']['full_page_validated'] is True
    assert checked_page['listing']['inventory_complete_verified'] is False
    assert checked_page['scan']['clock_live_validated'] is False
    assert checked_page['watermark'] == {'state': 'unknown', 'continuation_pin_validated': False, 'source_verified': False}
    assert checked_page['qualification'] == {'wire_contract': True, 'inventory_completion': False,
        'source_policy': False, 'native_acl': False, 'vendor_account': False, 'business_meaning': False, 'publication': False}
    assert checked_page['execution'] == {'network_calls': 0, 'source_writes': 0, 'registry_writes': 0, 'model_calls': 0}
    assert 'Complete synthetic page text.' not in json.dumps(checked_page)
    assert 'synthetic.entity' not in json.dumps(checked_page) and 'synthetic.proof' not in json.dumps(checked_page)
page['profile_digest'] = inventory_digest({'synthetic_profile': 'changed'})
assert conformance_module.check_inventory_page(json.dumps(page).encode('utf-8'), request)['error_code'] == 'adapter_inventory_pin_changed'
response = {'protocol_version': '1.2', 'listing_complete': True, 'assets': [{
    'name': 'Synthetic readings', 'pointer': 'tables/readings', 'schema': [
        {'name': 'reading_id', 'native_type': 'BIGINT', 'nullable': None}],
    'observations': {'row_count': 0, 'row_count_basis': 'exact'}}]}
wire = json.dumps(response, allow_nan=False).encode('utf-8')
checked = conformance_module.check_response(wire, provider='synthetic', scope='readings', protocol_version='1.2')
assert checked['result'] == 'passed' and checked['counts']['assets'] == checked['counts']['columns'] == 1
assert checked['listing']['full_response_validated'] is True and checked['listing']['source_verified'] is False
assert checked['execution'] == {'network_calls': 0, 'source_writes': 0, 'registry_writes': 0, 'model_calls': 0}
assert checked['qualification']['business_meaning'] is False and checked['qualification']['publication'] is False
normalized = normalize_response(wire, provider='synthetic', scope='readings', protocol_version='1.2')
assert normalized['assets'][0]['schema'][0]['nullable'] is None
assert normalized['assets'][0]['profile']['row_count'] == 0 and normalized['assets'][0]['profile']['row_count_is_complete'] is True
store = KnowledgeStore(database_url='sqlite:///:memory:')
try:
    assert build_quick_mcp_server(store)
    assert capability_manifest()['schema_version'] == 'knowledge-capabilities-v1'
    source = store.create_connection('demo', kind='local_files', name='Packaged synthetic inventory', config={})
    durable = builds_module.BuildRuns(store).start(
        "demo", connection_ids=[source["id"]], max_items=1, max_seconds=60)
    assert durable["status"] == "waiting_for_discovery"
    assert builds_module.BuildRuns(store).get("demo", durable["id"])["id"] == durable["id"]
    inventory = InventoryStore(store)
    entity = {'id': 'synthetic.entity', 'kind': 'semantic_definition',
              'payload': {'name': 'Synthetic definition', 'native_version': None,
                          'definition': 'Complete synthetic text.\\nSecond paragraph.',
                          'source_observed_at': None, 'observed_zero': 0},
              'evidence_ids': ['synthetic.proof']}
    proof = {'id': 'synthetic.proof', 'kind': 'upstream_semantic_definition',
             'payload': {'native_id': 'synthetic.term', 'native_version': None,
                         'reference': 'synthetic/term', 'source_observed_at': None}}
    run = inventory.begin('demo', source['id'],
        scope={'namespace': 'synthetic', 'selectors': ['synthetic.entity']},
        policy={'id': 'metadata-only', 'revision': None}, watermark={'kind': 'unknown', 'value': None},
        limits={'max_pages': 2, 'max_entities': 1, 'max_evidence': 1, 'max_total_bytes': 16384},
        expected_counts={'pages': 2, 'entities': 1, 'evidence': 1})
    for index, entities, evidence, cursor, next_cursor in (
            (0, [entity], [], None, 'later.proof'), (1, [], [proof], 'later.proof', None)):
        run = inventory.append('demo', source['id'], run['id'], page_index=index,
            entities=entities, evidence=evidence, cursor=cursor, next_cursor=next_cursor,
            scope_digest=run['scope_digest'], policy_digest=run['policy_digest'], watermark_digest=run['watermark_digest'])
    run = inventory.commit('demo', source['id'], run['id'], completion={
        'listing_complete': True, 'pages': 2, 'entities': 1, 'evidence': 1, 'manifest_digest': run['manifest_digest']})
    scopes = inventory.scopes('demo', source['id'], limit=1)
    assert len(scopes['items']) == 1 and scopes['next_cursor'] is None
    head = scopes['items'][0]
    assert head['id'] == run['id'] and head['manifest_digest'] == run['manifest_digest']
    assert head['generation'] == 1 and head['watermark'] == {'kind': 'unknown', 'value': None}
    selected = inventory.select('demo', source['id'], head['id'],
        expected_manifest_digest=head['manifest_digest'], entity_ids=['synthetic.entity'])
    assert selected['entities'] == [{**entity, 'digest': inventory_digest(entity)}]
    assert selected['evidence'] == [{**proof, 'digest': inventory_digest(proof)}]
    assert selected['inventory']['id'] == head['id'] and selected['inventory']['manifest_digest'] == head['manifest_digest']
    assert selected['text_complete'] is True and selected['agent_ready'] is False
    assert selected['meaning_certified'] is False and selected['source_reads_executed'] is False
    assert selected['source_text_authority'] == 'untrusted_evidence'
    assert len(json.dumps(selected, ensure_ascii=False).encode('utf-8')) < 16384
    assert store.get_snapshot('demo', source['id']) is None
    citation_ref = {'connection_id': source['id'], 'inventory_id': head['id'],
        'manifest_digest': head['manifest_digest'], 'entity_id': entity['id'], 'evidence_id': proof['id']}
    citation_state, citation_proof = inventory_citations_module.resolve_inventory_citation(store, 'demo', citation_ref)
    assert citation_state == {'inventory_id': head['id'], 'manifest_digest': head['manifest_digest'],
        'entity': selected['entities'][0], 'inventory': selected['inventory']}
    assert citation_proof == selected['evidence'][0]
    concepts = inventory_concepts_module.InventoryConcepts(store)
    concept = concepts.propose('demo', source['id'], evidence_ref=citation_ref,
        name='Synthetic business term', definition='A locally interpreted synthetic business definition.',
        concept_kind='business_term', rationale='Synthetic owner interpretation for installed package qualification.')
    assert concepts.page('demo', source['id'], inventory_id=head['id'],
        expected_manifest_digest=head['manifest_digest'])['items'] == []
    store.decide_proposal('demo', source['id'], concept['id'], decision='approve')
    concepts.publish('demo', source['id'], concept_id=concept['id'],
        expected_proposal_digest=concept['proposal_digest'],
        rationale='Synthetic steward publication of reviewed local meaning.')
    context = store.get_context('demo', concept_id=concept['id'])
    assert context['basis'] == 'inventory' and context['publication_state'] == 'published'
    assert context['evidence_ref'] == citation_ref and 'snapshot_id' not in context
    assert context['concept']['definition'] == concept['concept']['definition']
    assert not context['locally_executable'] and not context['native_meaning_certified']
    assert store.get_snapshot('demo', source['id']) is None
    term = store.get_context('demo', term='Synthetic business term', connection_id=source['id'])
    assert term['matches'][0]['id'] == concept['id'] and term['matches'][0]['evidence_ref'] == citation_ref
    from knowledge_layer.retrieval import search_knowledge
    found = search_knowledge(store, tenant_id='demo', query='Synthetic business term')
    assert found['results'][0]['concept_id'] == concept['id'] and found['results'][0]['basis'] == 'inventory'
    assert found['results'][0]['context']['concept']['definition'] == concept['concept']['definition']
    packet = task_context_module.task_context(store, tenant_id='demo', query='Synthetic business term',
        connection_ids=[source['id']], inventory_selection=[{'connection_id': source['id'], 'concept_ids': [concept['id']]}])
    assert packet['schema_version'] == 'task-context-v2' and packet['answerability'] == 'business_meaning_available'
    assert packet['sources'][0]['basis'] == 'inventory' and 'snapshot_id' not in packet['sources'][0]
    assert task_context_module.revalidate_context(store, tenant_id='demo', receipt=packet['receipt'])['status'] == 'current'
    try:
        inventory_citations_module.resolve_inventory_citation(store, 'demo', {**citation_ref, 'evidence_id': 'unlinked.proof'})
    except KnowledgeError:
        pass
    else:
        raise AssertionError('Installed inventory citation accepted unlinked native proof')
    aborted = inventory.begin('demo', source['id'],
        scope={'namespace': 'synthetic-aborted', 'selectors': ['synthetic.entity']},
        policy={'id': 'metadata-only', 'revision': None}, watermark={'kind': 'unknown', 'value': None},
        limits={'max_pages': 1, 'max_entities': 1, 'max_evidence': 1, 'max_total_bytes': 16384})
    aborted = inventory.append('demo', source['id'], aborted['id'], page_index=0,
        entities=[entity], evidence=[proof], cursor=None, next_cursor=None,
        scope_digest=aborted['scope_digest'], policy_digest=aborted['policy_digest'],
        watermark_digest=aborted['watermark_digest'])
    inventory.abort('demo', source['id'], aborted['id'])
    retention = retention_module.InventoryRetention(store)
    cutoff = datetime.fromisoformat(aborted['created_at']) + timedelta(microseconds=1)
    options = {'created_before': cutoff, 'max_runs': 1, 'max_rows': 4}
    tables = {'runs': store.inventory_runs, 'pages': store.inventory_pages,
              'entities': store.inventory_entities, 'evidence': store.inventory_evidence}
    unchanged = [table for table in store.metadata.sorted_tables
                 if table not in tuple(tables.values()) + (store.audit_events,)]
    def state(conn):
        return {table.name: sorted(json.dumps(dict(row), sort_keys=True) for row in
                                  conn.execute(select(table)).mappings()) for table in unchanged}
    with store.engine.connect() as conn:
        before = state(conn)
        counts_before = {key: conn.execute(select(func.count()).select_from(table)).scalar_one()
                         for key, table in tables.items()}
        audit_before = conn.execute(select(func.count()).select_from(store.audit_events)).scalar_one()
    preview = retention.preview('demo', source['id'], **options)
    expected_counts = {'runs': 1, 'pages': 1, 'entities': 1, 'evidence': 1, 'total_rows': 4}
    assert preview['schema_version'] == 'knowledge-inventory-retention-v1'
    assert preview['operation'] == 'preview' and preview['status'] == 'ready'
    assert preview['counts'] == expected_counts and preview['registry_writes_executed'] is False
    assert [item['inventory_id'] for item in preview['runs']] == [aborted['id']]
    with store.engine.connect() as conn:
        assert state(conn) == before
        assert {key: conn.execute(select(func.count()).select_from(table)).scalar_one()
                for key, table in tables.items()} == counts_before
        assert conn.execute(select(func.count()).select_from(store.audit_events)).scalar_one() == audit_before
    receipt = retention.apply('demo', source['id'], expected_preview_digest=preview['preview_digest'], **options)
    assert receipt['operation'] == 'apply' and receipt['status'] == 'applied'
    assert receipt['counts'] == expected_counts and receipt['registry_writes_executed'] is True
    assert receipt['preview_digest'] == preview['preview_digest']
    for output in (preview, receipt):
        assert output['source_reads_executed'] is False and output['source_writes_executed'] is False
        assert output['source_clocks_changed'] is False
        assert 'Complete synthetic text' not in json.dumps(output)
        assert 'synthetic.entity' not in json.dumps(output) and 'synthetic.proof' not in json.dumps(output)
    with store.engine.connect() as conn:
        assert state(conn) == before
        for key, table in tables.items():
            assert conn.execute(select(func.count()).select_from(table)).scalar_one() == counts_before[key] - 1
            identifier = table.c.id if key == 'runs' else table.c.inventory_id
            assert conn.execute(select(identifier).where(identifier == aborted['id'])).first() is None
        assert conn.execute(select(func.count()).select_from(store.audit_events)).scalar_one() == audit_before + 1
        audit = conn.execute(select(store.audit_events).where(
            store.audit_events.c.event_type == 'inventory.retention')).mappings().one()
        assert audit['actor'] == 'operator' and audit['connection_id'] == source['id']
        assert audit['details']['inventory_ids'] == [aborted['id']]
        assert audit['details']['counts'] == expected_counts
        assert audit['details']['preview_digest'] == preview['preview_digest']
        source_row = conn.execute(select(store.connections).where(store.connections.c.id == source['id'])).mappings().one()
        for clock in ('last_refreshed_at', 'last_refresh_attempt_at', 'last_complete_refresh_at'):
            assert source_row[clock] is None
    assert inventory.scopes('demo', source['id'], limit=1)['items'][0]['id'] == run['id']
    assert inventory.select('demo', source['id'], run['id'], expected_manifest_digest=run['manifest_digest'],
                            entity_ids=['synthetic.entity']) == selected
    assert retention.preview('demo', source['id'], **options)['status'] == 'empty'
    assert store.get_snapshot('demo', source['id']) is None
    with store.engine.begin() as conn:
        conn.execute(update(store.connections).where(store.connections.c.id == source['id']).values(status='error'))
    try:
        inventory_citations_module.resolve_inventory_citation(store, 'demo', citation_ref)
    except KnowledgeError:
        pass
    else:
        raise AssertionError('Installed inventory citation accepted a failed source')
    try:
        concepts.get('demo', source['id'], concept_id=concept['id'])
    except KnowledgeError:
        pass
    else:
        raise AssertionError('Installed inventory concept accepted a failed source')
    try:
        task_context_module.revalidate_context(store, tenant_id='demo', receipt=packet['receipt'])
    except KnowledgeError:
        pass
    else:
        raise AssertionError('Installed inventory receipt accepted a failed source')
    assert not search_knowledge(store, tenant_id='demo', query='Synthetic business term')['results']
finally:
    store.close()
assert len(rdf_validation_module.shapes_graph()) > 0
try:
    rdf_validation_module.validate_graph(rdf_validation_module.Graph())
except rdf_validation_module.RDFValidationError as exc:
    assert str(exc) == 'semantic_validation_extra_required'
else:
    raise AssertionError('Base package unexpectedly includes the optional SHACL validator')
print(version('knowledge-layer'))
"""

SHACL_SMOKE = """import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from rdflib import RDF, Literal
from knowledge_layer.ontology import KL, project_snapshot
from knowledge_layer.rdf_validation import main, validate_graph
graph = project_snapshot({'snapshot_id': 'synthetic-export', 'status': 'discovery_complete'},
                         tenant_id='demo', connection_id='synthetic-source')
before = set(graph)
assert validate_graph(graph)['conforms'] is True and set(graph) == before
with tempfile.TemporaryDirectory() as temporary:
    artifact = Path(temporary) / 'export.ttl'
    artifact.write_text(graph.serialize(format='turtle'))
    output = io.StringIO()
    with redirect_stdout(output):
        assert main(['--input', str(artifact)]) == 0
    assert json.loads(output.getvalue())['conforms'] is True
snapshot = next(graph.subjects(RDF.type, KL.Snapshot))
graph.set((snapshot, KL.agentReady, Literal('false')))
assert validate_graph(graph)['conforms'] is False
"""


def _inventory_fixture() -> tuple[dict, dict]:
    """Generate explicit synthetic wire files with no installed-package imports."""
    request = {"protocol_version": "1.3", "operation": "inventory_page", "provider": "synthetic",
               "scope": "readings", "profile_digest": hashlib.sha256(b"synthetic target").hexdigest(),
               "scan_id": "packaged.synthetic.scan", "page_index": 0, "cursor": None,
               "scan_started_at": "2000-01-01T00:00:00Z", "scan_expires_at": "2000-01-01T00:05:00Z",
               "expected_watermark_digest": None, "max_entities": 100, "max_evidence": 200,
               "max_response_bytes": 256 * 1024,
               "inventory_limits": {"max_pages": 2, "max_entities": 1, "max_evidence": 1,
                                    "max_total_bytes": 16384}}
    request["request_id"] = hashlib.sha256(json.dumps(
        request, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False).encode("utf-8")).hexdigest()
    page = {key: value for key, value in request.items()
            if key not in {"inventory_limits", "max_entities", "max_evidence", "max_response_bytes"}}
    page.update(next_cursor=None, listing_complete=True, watermark={"kind": "unknown", "value": None},
                entities=[{"id": "synthetic.entity", "kind": "asset", "evidence_ids": ["synthetic.proof"],
                           "payload": {"native_version": None, "observed_zero": 0,
                                       "text": "Complete synthetic page text."}}],
                evidence=[{"id": "synthetic.proof", "kind": "source_observation",
                           "payload": {"source_observed_at": None}}])
    return request, page


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    wheels = list(args.dist.glob("knowledge_layer-*.whl"))
    sources = list(args.dist.glob("knowledge_layer-*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        parser.error("Use a directory containing exactly one wheel and one source archive")
    wheel, source = wheels[0].resolve(), sources[0].resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        required = {"knowledge_layer/platform.py", "knowledge_layer/planning.py",
                    "knowledge_layer/iap.py", "knowledge_layer/registry_bootstrap.py",
                    "knowledge_layer/origin.py", "knowledge_layer/local_auth.py",
                    "knowledge_layer/local_profile.py", "knowledge_layer/setup.py",
                    "knowledge_layer/source_permissions.py", "knowledge_layer/connector_isolation.py",
                    "knowledge_layer/connector_process.py", "knowledge_layer/source_io_audit.py",
                    "knowledge_layer/refresh_authorization.py", "knowledge_layer/composite_metrics.py",
                    "knowledge_layer/builds.py", "knowledge_layer/build_schema.py",
                    "knowledge_layer/build_transport.py", "knowledge_layer/build_worker.py",
                    "knowledge_layer/build_handoff.py", "knowledge_layer/review_batches.py",
                    "knowledge_layer/migrations/versions/v0016_builds.py",
                    "knowledge_layer/metric_semantics.py", "knowledge_layer/task_context.py",
                    "knowledge_layer/inventory.py", "knowledge_layer/inventory_transport.py",
                    "knowledge_layer/inventory_citations.py",
                    "knowledge_layer/inventory_concepts.py", "knowledge_layer/inventory_concept_schema.py",
                    "knowledge_layer/inventory_concept_search.py",
                    "knowledge_layer/migrations/versions/v0015_inventory_concepts.py",
                    "knowledge_layer/inventory_retention.py", "knowledge_layer/inventory_retention_cli.py",
                    "knowledge_layer/inventory_refresh.py", "knowledge_layer/connectors/external_inventory.py",
                    "knowledge_layer/adapters/conformance.py",
                    "knowledge_layer/adapters/dbt.py", "knowledge_layer/adapters/datahub.py",
                    "knowledge_layer/adapters/paging.py",
                    "knowledge_layer/adapters/catalog.py", "knowledge_layer/adapters/catalog_mapping.py",
                    "knowledge_layer/adapters/catalog_cli.py",
                    "knowledge_layer/autonomy.py", "knowledge_layer/autonomy_schema.py",
                    "knowledge_layer/autonomy_transport.py",
                    "knowledge_layer/migrations/versions/v0013_autonomy.py",
                    "knowledge_layer/migrations/versions/v0014_inventory_results.py",
                    "knowledge_layer/migrations/versions/v0012_inventory.py",
                    "knowledge_layer/schemas/ossie-schema-v0.2.0.dev0.json",
                    "knowledge_layer/rdf_validation.py", "knowledge_layer/schemas/knowledge_shapes.ttl"}
        if not required <= names or not any(name.endswith("licenses/LICENSE") for name in names):
            raise RuntimeError("Wheel is missing platform capabilities, pinned schema or license")
    with tarfile.open(source) as archive:
        names = archive.getnames()
        if not all(any(name.endswith("/" + item) for name in names) for item in (
            "LICENSE", "NOTICE", "README.md", "pyproject.toml", "uv.lock", "Dockerfile", "Dockerfile.postgres", "compose.yaml",
            "scripts/collect_release_notices.py", "scripts/fetch_release_archives.py",
            "scripts/fetch_publisher_notices.py", "docs/publisher-notices.json",
            "scripts/check_release_controls.py", "docs/ga-review-packet.md",
            "scripts/apply_runtime_patches.py", "third_party/runtime-patches/tempfile-rmtree-original.txt",
            "third_party/runtime-patches/CPython-LICENSE.txt", "third_party/runtime-patches/NOTICE.md",
            "src/knowledge_layer/iap.py", "src/knowledge_layer/registry_bootstrap.py",
            "src/knowledge_layer/origin.py", "src/knowledge_layer/local_auth.py",
            "src/knowledge_layer/local_profile.py", "src/knowledge_layer/setup.py",
            "knowledge-layer", "scripts/self_host.py", "scripts/configure_agent.py", "docs/self-host.md",
            "scripts/self_host_recovery.py", "scripts/drill_self_host.py",
            "deploy/self-host/init.sql", "deploy/self-host/worker.override.example.yaml",
            "web/src/SetupAuthGate.tsx", "web/src/SetupGuide.tsx", "web/src/setup.css",
            "docs/iap-cloud-run.md", "docs/source-worker-isolation.md",
            "deploy/gcp/cloud-run.md", "deploy/gcp/cloud-run.env.yaml.example",
            "examples/enterprise_consumer.py", "scripts/start.sh", "web/package-lock.json",
            "scripts/drill_autonomy.py", "docs/autonomy-policies.md",
            "scripts/check_adapter_contract.py", "docs/adapter-conformance.md",
            "examples/native_semantic_adapter.py", "examples/reference_adapter.py",
            "scripts/drill_native_semantic.py", "docs/native-semantic-adapters.md",
            "docs/catalog-agnostic-import.md",
            "docs/inventory-semantic-citations.md", "src/knowledge_layer/inventory_citations.py",
            "docs/inventory-concepts.md", "src/knowledge_layer/inventory_concepts.py",
            "src/knowledge_layer/inventory_concept_search.py",
            "src/knowledge_layer/inventory_concept_schema.py",
            "src/knowledge_layer/rdf_validation.py", "src/knowledge_layer/schemas/knowledge_shapes.ttl",
            "docs/rdf-validation.md",
            "src/knowledge_layer/migrations/versions/v0015_inventory_concepts.py",
            "docs/inventory-retention.md",
            "src/knowledge_layer/inventory_refresh.py", "src/knowledge_layer/connectors/external_inventory.py",
            "src/knowledge_layer/migrations/versions/v0014_inventory_results.py",
            "data/connectable/juniper/products.csv",
        )):
            raise RuntimeError("Source archive is missing release notices or build inputs")
        launchers = [member for member in archive.getmembers() if member.name.endswith("/knowledge-layer")]
        if len(launchers) != 1 or not launchers[0].isfile() or not launchers[0].mode & 0o100:
            raise RuntimeError("Source archive launcher must be one executable regular file")
    with tempfile.TemporaryDirectory(prefix="knowledge-wheel-") as temporary:
        runtime = Path(temporary) / "runtime"
        subprocess.run(["uv", "venv", "--python", "3.14", str(runtime)], check=True, capture_output=True)
        python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(["uv", "pip", "install", "--python", str(python), str(wheel)], check=True, capture_output=True)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("KNOWLEDGE_", "DATABASE_URL", "PYTHONPATH"))}
        result = subprocess.run([str(python), "-I", "-c", BASE_SMOKE], cwd=temporary,
                                env=environment, check=True, capture_output=True, text=True)
        subprocess.run(["uv", "pip", "install", "--python", str(python), "--no-deps", "--force-reinstall", str(source)],
                       check=True, capture_output=True)
        source_result = subprocess.run([str(python), "-I", "-c", BASE_SMOKE], cwd=temporary,
                                       env=environment, check=True, capture_output=True, text=True)
        if source_result.stdout != result.stdout:
            raise RuntimeError("Source and wheel package versions disagree")
        # Qualify optional validation outside the checkout, after proving base-only installs.
        subprocess.run(["uv", "pip", "install", "--python", str(python), "--reinstall-package", "knowledge-layer",
                        f"{wheel}[semantic-validation]"], check=True, capture_output=True)
        subprocess.run([str(python), "-I", "-c", SHACL_SMOKE], cwd=temporary,
                       env=environment, check=True, capture_output=True, text=True)
        subprocess.run(["uv", "pip", "install", "--python", str(python), "--no-deps", "--force-reinstall", str(source)],
                       check=True, capture_output=True)
        subprocess.run([str(python), "-I", "-c", SHACL_SMOKE], cwd=temporary,
                       env=environment, check=True, capture_output=True, text=True)
        retention_help = subprocess.run([str(python), "-I", "-m", "knowledge_layer.inventory_retention_cli", "--help"],
                                        cwd=temporary, env=environment, check=True, capture_output=True, text=True)
        if not all(option in retention_help.stdout for option in ("--created-before", "--apply", "--expected-preview-digest")):
            raise RuntimeError("Installed source retention command help is incomplete")
        extracted = Path(temporary) / "source"
        with tarfile.open(source) as archive:
            archive.extractall(extracted, filter="data")
        examples = list(extracted.glob("*/examples/enterprise_consumer.py"))
        if len(examples) != 1:
            raise RuntimeError("Source archive must contain one enterprise consumer example")
        demonstration = subprocess.run([str(python), "-I", str(examples[0])], cwd=temporary,
                                       env=environment, check=True, capture_output=True, text=True)
        demo = json.loads(demonstration.stdout)
        if demo.get("result") != "passed" or demo.get("model_calls") != 0:
            raise RuntimeError("Installed source consumer example did not pass")
        contributors = list(extracted.glob("*/scripts/check_adapter_contract.py"))
        if len(contributors) != 1:
            raise RuntimeError("Source archive must contain one contributor conformance command")
        response_path = Path(temporary) / "synthetic-adapter-response.json"
        response_path.write_text(json.dumps({"protocol_version": "1.2", "listing_complete": True,
            "assets": [{"name": "Synthetic readings", "pointer": "tables/readings", "schema": [
                {"name": "reading_id", "native_type": "BIGINT", "nullable": None}],
                "observations": {"row_count": 0, "row_count_basis": "exact"}}]}, allow_nan=False))
        contributor = subprocess.run([str(python), "-I", str(contributors[0]), "--input", str(response_path),
            "--provider", "synthetic", "--scope", "readings", "--protocol-version", "1.2"],
            cwd=temporary, env=environment, check=True, capture_output=True, text=True)
        checked = json.loads(contributor.stdout)
        if (checked.get("result") != "passed" or checked.get("counts", {}).get("assets") != 1
                or checked.get("counts", {}).get("columns") != 1
                or checked.get("execution") != {"network_calls": 0, "source_writes": 0, "registry_writes": 0, "model_calls": 0}
                or checked.get("qualification", {}).get("publication") is not False):
            raise RuntimeError("Installed source contributor conformance command did not pass")
        inventory_request, inventory_page = _inventory_fixture()
        request_path = Path(temporary) / "synthetic-inventory-request.json"
        page_path = Path(temporary) / "synthetic-inventory-page.json"
        request_path.write_text(json.dumps(inventory_request, allow_nan=False), encoding="utf-8")
        page_path.write_text(json.dumps(inventory_page, allow_nan=False), encoding="utf-8")
        paged = subprocess.run([str(python), "-I", str(contributors[0]), "--input", str(page_path),
            "--provider", "synthetic", "--scope", "readings", "--protocol-version", "1.3",
            "--inventory-request", str(request_path)],
            cwd=temporary, env=environment, check=True, capture_output=True, text=True, timeout=30)
        checked_page = json.loads(paged.stdout)
        if (checked_page.get("result") != "passed" or checked_page.get("protocol_version") != "1.3"
                or checked_page.get("counts_scope") != "supplied_page"
                or checked_page.get("counts") != {"entities": 1, "evidence": 1}
                or checked_page.get("listing", {}).get("state") != "declared_terminal_page"
                or checked_page.get("listing", {}).get("full_page_validated") is not True
                or checked_page.get("listing", {}).get("inventory_complete_verified") is not False
                or checked_page.get("scan", {}).get("clock_live_validated") is not False
                or checked_page.get("watermark") != {"state": "unknown", "continuation_pin_validated": False,
                                                     "source_verified": False}
                or checked_page.get("execution") != {"network_calls": 0, "source_writes": 0,
                                                      "registry_writes": 0, "model_calls": 0}
                or checked_page.get("qualification") != {"wire_contract": True, "inventory_completion": False,
                    "source_policy": False, "native_acl": False, "vendor_account": False,
                    "business_meaning": False, "publication": False}
                or any(value in paged.stdout for value in ("Complete synthetic page text.", "synthetic.entity",
                                                          "synthetic.proof", "packaged.synthetic.scan"))):
            raise RuntimeError("Installed source inventory page conformance command did not pass")
    checksums = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (wheel, source)}
    receipt = {"schema_version": "distribution-verification-v1", "package_version": result.stdout.strip(),
               "wheel_base_install": "passed", "source_base_install": "passed",
               "wheel_optional_shacl": "passed", "source_optional_shacl": "passed",
               "source_consumer_example": "passed", "checksums": checksums, "signed": False,
               "wheel_adapter_conformance": "passed", "source_adapter_conformance": "passed",
               "wheel_inventory_selection": "passed", "source_inventory_selection": "passed",
               "wheel_inventory_citations": "passed", "source_inventory_citations": "passed",
               "wheel_inventory_concepts": "passed", "source_inventory_concepts": "passed",
               "wheel_inventory_retrieval_receipts": "passed", "source_inventory_retrieval_receipts": "passed",
               "wheel_inventory_retention": "passed", "source_inventory_retention": "passed",
               "source_inventory_retention_cli_help": "passed",
               "source_adapter_conformance_cli": "passed",
               "wheel_inventory_page_conformance": "passed", "source_inventory_page_conformance": "passed",
               "source_inventory_page_conformance_cli": "passed",
               "wheel_native_semantic_inventory": "passed", "source_native_semantic_inventory": "passed",
               "wheel_catalog_export_mapping_cli": "passed", "source_catalog_export_mapping_cli": "passed",
               "published": False, "architecture": "current_runner_only"}
    (args.dist / "verification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    (args.dist / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())))
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
