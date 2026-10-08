# Collective knowledge graph

The Knowledge map opens across the workspace. Source selection filters this shared view; it does not select a separate knowledge layer. Tables, observed fields, documents, scoped concepts, imported ontology/glossary records, metrics, semantic proposals and supporting evidence use qualified stable identities.

The graph combines three kinds of connections:

- Observed structure and provenance: a source contains an asset, a table has fields, and evidence was observed in an asset.
- Explicit semantic/evidence bindings: source relationships, proposal citations, and reviewed field/asset-to-term bindings retain their source revisions and review states.
- Possible shared meaning: matching labels across sources attach to a shared-term cluster. The original definitions remain separate, including differing units and descriptions. These candidate clusters never establish identity, formula equivalence or a join.

The main Knowledge map uses AntV G6. All knowledge, Ontology, Vocabulary, Source structure and Evidence are projections of the same authorized graph, with stable identities and recorded relationship/review metadata. Vocabulary opens as a term table; every view also supports graph, record and relationship displays. Ontology and evidence views include one-hop source/evidence context and do not invent hierarchy or equivalence. Review focus includes matching record/definition states and matching relationship endpoints, preserving each endpoint's own state.

Search and node neighborhoods let users move from a business term to its data bindings, definitions, evidence and source review. The main UI requests at most 400 nodes across a page of at most 100 authorized sources; API defaults remain 180/40. Source pagination, visible record counts and truncation remain explicit. Filtering one source also includes its accessible explicit citation dependencies. No upstream data query or source write occurs.

The shared G6 canvas provides source-group collapse/expand, native layouts, automatic/all/off labels, minimap, neighbor highlighting, bounded connectivity paths and PNG export. JSON export retains exact record identities, states and source coverage. Paths show connectivity within the loaded view; they do not certify joins. The inspector opens exact pending assertions in the existing curation workflow, and returning to the map restores the search/view/selection. Graph lab remains a secondary comparison and uses the same G6 canvas.

## Agents and APIs

Both compact and detailed MCP expose `get_workspace_graph` and `get_workspace_ontology`. HTTP equivalents are `GET /api/knowledge/workspace/graph` and `/workspace/ontology` for the browser, and `/agent/api/workspace/graph` and `/workspace/ontology` for authenticated agents. They accept optional `connection_id`, `q`, `focus_id`, `depth`, `limit`, `source_limit` and source-page `cursor`.

`propose_contract` accepts `term_binding` through the existing evidence/decision ledger. This binds a source asset or observed field to an exact current document or imported semantic concept. Payload: `target_connection_id`, `target_snapshot_id`, `target_concept_id`, `relationship` and optional `field`. Relationships are `represents`, `measures`, `mentions` or `related_to`. One exact target concept evidence reference and local source evidence are required. This does not infer a physical table join.

A draft can be created before target meaning review. Owner approval and continued effectiveness require the target's exact current local definition review. Target snapshot changes, withdrawn meaning, source drift and lost grants suppress effective context. Source publication remains a separate step. Matching labels are not automatically approved as term bindings.

## RDF validation

Workspace exports use `knowledge-workspace-rdf-shacl-v1` and the bundled workspace shapes. Single-source exports retain their existing snapshot schema. Validate a saved collective export with:

```sh
python -m knowledge_layer.rdf_validation --schema workspace --input /path/to/workspace.ttl
```

SHACL checks structure and separate candidate/review states. It does not prove source truth, freshness, completeness or analytical execution. The collective graph currently projects versioned semantic snapshots; separately staged inventory-only protocol 1.3 observations require semantic snapshot curation before appearing as full knowledge records.
