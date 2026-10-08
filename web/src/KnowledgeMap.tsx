import './knowledgeMap.css';
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, ArrowLeft, Database, Focus, LoaderCircle, RefreshCw, Search, Waypoints, Download, List, Network } from 'lucide-react';
import { getWorkspaceGraph, type GraphEdge, type GraphNode, type KnowledgeConnection, type WorkspaceGraph } from './platformApi';

import { knowledgeLenses, projectKnowledgeView, type KnowledgeLens } from './knowledgeGraphViews';

const KnowledgeGraphCanvas = lazy(() => import('./KnowledgeGraphCanvas'));

const kindLabels: Record<GraphNode['kind'], string> = {
  source: 'Connected source', shared_term: 'Possible shared meaning', concept: 'Business concept',
  metric: 'Metric', asset: 'Data asset', document: 'Document', ontology: 'Ontology',
  evidence: 'Evidence', field: 'Source field', assertion: 'Semantic assertion',
};
const possibleStates = new Set(['candidate', 'pending_review', 'possible_shared_term', 'inferred', 'stale']);
const RENDER_NODE_LIMIT = 400;
const RENDER_EDGE_LIMIT = 2000;

function readable(value: string): string {
  return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
}

function isPossible(edge: GraphEdge, nodes: Map<string, GraphNode>): boolean {
  return nodes.get(edge.subject_id)?.kind === 'shared_term' || nodes.get(edge.object_id)?.kind === 'shared_term';
}

function isCitation(edge: GraphEdge, nodes: Map<string, GraphNode>): boolean {
  return [nodes.get(edge.subject_id)?.kind, nodes.get(edge.object_id)?.kind].some((kind) => kind === 'evidence' || kind === 'document')
    || /evidence|citation|cites|supported_by/.test(edge.predicate);
}

function mergeGraphs(current: WorkspaceGraph, page: WorkspaceGraph): WorkspaceGraph {
  const nodes = new Map(current.nodes.map((node) => [node.id, node]));
  for (const node of page.nodes) {
    const previous = nodes.get(node.id);
    if (previous?.members && node.members) {
      const members = new Map([...previous.members, ...node.members].map((member) => [typeof member === 'string' ? member
        : member.node_id ?? member.id ?? `${member.connection_id}/${member.record_id}`, member]));
      const sourceCount = new Set([...members.values()].flatMap((member) => typeof member === 'string' || !member.connection_id ? [] : [member.connection_id])).size;
      nodes.set(node.id, { ...node, members: [...members.values()].slice(0, 100),
        source_count: Math.max(previous.source_count ?? 0, node.source_count ?? 0, sourceCount),
        member_count: Math.max(previous.member_count ?? 0, node.member_count ?? 0, members.size),
        definition_variant_count: Math.max(previous.definition_variant_count ?? 0, node.definition_variant_count ?? 0),
        unit_variants: [...new Set([...(previous.unit_variants ?? []), ...(node.unit_variants ?? [])])],
        conflict: previous.conflict || node.conflict,
      });
    } else nodes.set(node.id, node);
  }
  const connections = new Map([...current.connections, ...page.connections].map((source) => [source.id, source]));
  const renderedNodes = [...nodes.values()].slice(0, RENDER_NODE_LIMIT);
  const renderedIds = new Set(renderedNodes.map((node) => node.id));
  const allEdges = [...new Map([...current.edges, ...page.edges].map((edge) => [edge.id, edge])).values()];
  const visibleEdges = allEdges.filter((edge) => renderedIds.has(edge.subject_id) && renderedIds.has(edge.object_id));
  const renderedEdges = visibleEdges.slice(0, RENDER_EDGE_LIMIT);
  const nodesClipped = Boolean(current.coverage.client_nodes_clipped) || nodes.size > RENDER_NODE_LIMIT;
  const edgesClipped = Boolean(current.coverage.client_edges_clipped) || allEdges.length > renderedEdges.length;
  return { ...page, nodes: renderedNodes, edges: renderedEdges,
    connections: [...connections.values()], loaded_sources: connections.size,
    truncated: current.truncated || page.truncated || nodesClipped || edgesClipped,
    coverage: { ...page.coverage, visible_nodes: renderedNodes.length, visible_edges: renderedEdges.length,
      client_nodes_clipped: nodesClipped, client_edges_clipped: edgesClipped,
      rendered_node_limit: RENDER_NODE_LIMIT, rendered_edge_limit: RENDER_EDGE_LIMIT },
  };
}

function navigationScope(kind: GraphNode['kind'] | undefined, connectionId: string | undefined, currentSourceId: string, cursor: string) {
  return kind === 'shared_term'
    ? { sourceId: currentSourceId, cursor: currentSourceId ? '' : cursor }
    : { sourceId: connectionId ?? currentSourceId, cursor: '' };
}

export type KnowledgeMapLocation = { sourceFilter: string; query: string; focusId: string; focusSourceId: string; focusCursor: string; depth: 1 | 2; selectedId: string; lens: KnowledgeLens; view: 'graph' | 'records' | 'relationships'; reviewState: string };

export default function KnowledgeMap({ connections, totalSources, onConnect, onReviewSource, initialLocation }: {
  connections: KnowledgeConnection[];
  totalSources: number;
  onConnect: () => void;
  onReviewSource: (id: string, proposalId?: string, location?: KnowledgeMapLocation) => void;
  initialLocation?: KnowledgeMapLocation;
}) {
  const [graph, setGraph] = useState<WorkspaceGraph | null>(null);
  const [sourceFilter, setSourceFilter] = useState(initialLocation?.sourceFilter ?? '');
  const [query, setQuery] = useState(initialLocation?.query ?? '');
  const [searchQuery, setSearchQuery] = useState(initialLocation?.query.trim() ?? '');
  const [focusId, setFocusId] = useState(initialLocation?.focusId ?? '');
  const [focusSourceId, setFocusSourceId] = useState(initialLocation?.focusSourceId ?? '');
  const [focusCursor, setFocusCursor] = useState(initialLocation?.focusCursor ?? '');
  const [depth, setDepth] = useState<1 | 2>(initialLocation?.depth ?? 2);
  const [selectedId, setSelectedId] = useState(initialLocation?.selectedId ?? '');
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const [lens, setLens] = useState<KnowledgeLens>(initialLocation?.lens ?? 'all');
  const [view, setView] = useState<'graph' | 'records' | 'relationships'>(() => initialLocation?.view
    ?? (window.matchMedia('(max-width: 640px)').matches ? 'records' : 'graph'));
  const [reviewState, setReviewState] = useState(initialLocation?.reviewState ?? '');
  const requestGeneration = useRef(0);
  const nodeCursors = useRef(new Map<string, string>());
  const sourceRevision = connections.map((source) => `${source.id}:${source.updated_at ?? ''}:${source.snapshot?.id ?? ''}:${source.snapshot?.version ?? ''}`).join('|');
  const nodeById = useMemo(() => new Map(graph?.nodes.map((node) => [node.id, node]) ?? []), [graph]);
  const selectedNode = nodeById.get(selectedId) ?? null;
  const selectedEdge = graph?.edges.find(edge => edge.id === selectedId) ?? null;
  const visible = useMemo(() => projectKnowledgeView(graph?.nodes ?? [], graph?.edges ?? [], lens, reviewState), [graph, lens, reviewState]);
  const currentLens = knowledgeLenses.find(item => item.id === lens)!;
  const reviewStates = useMemo(() => [...new Set(graph?.nodes.flatMap(node => [node.status, ...(node.definition_status ? [node.definition_status] : [])]).concat(graph.edges.map(edge => edge.status)) ?? [])].sort(), [graph]);
  const selectedOutsideView = Boolean(selectedNode && !visible.nodes.some(node => node.id === selectedId) || selectedEdge && !visible.edges.some(edge => edge.id === selectedId));
  const relatedEdges = useMemo(() => graph?.edges.filter((edge) => edge.subject_id === selectedId || edge.object_id === selectedId) ?? [], [graph, selectedId]);
  const possibleEdges = relatedEdges.filter((edge) => isPossible(edge, nodeById));
  const citationEdges = relatedEdges.filter((edge) => !isPossible(edge, nodeById) && isCitation(edge, nodeById));
  const sourceEdges = relatedEdges.filter((edge) => !isPossible(edge, nodeById) && !isCitation(edge, nodeById));
  const sourceChoices = useMemo(() => [...new Map([...connections, ...(graph?.connections ?? [])].map((source) => [source.id, { id: source.id, name: source.name }])).values()], [connections, graph]);
  const loadedSources = graph?.loaded_sources ?? 0;
  const scopedTotal = graph?.total_sources ?? totalSources;
  const filteredScope = graph?.coverage.source_scope === 'filtered_source_and_authorized_citations';

  useEffect(() => {
    const timer = window.setTimeout(() => setSearchQuery(query.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    for (const id of nodeCursors.current.keys()) if (!nodeById.has(id)) nodeCursors.current.delete(id);
  }, [nodeById]);

  useEffect(() => {
    const generation = ++requestGeneration.current;
    const controller = new AbortController();
    setLoading(true);
    setLoadingMore(false);
    setError('');
    setGraph(null);
    void getWorkspaceGraph({ connectionId: (focusId ? focusSourceId : sourceFilter) || undefined, query: searchQuery || undefined,
      focusId: focusId || undefined, cursor: focusCursor || undefined, depth, limit: RENDER_NODE_LIMIT, sourceLimit: 100, signal: controller.signal }).then((result) => {
      if (generation !== requestGeneration.current) return;
      nodeCursors.current = new Map(result.nodes.map((node) => [node.id, focusCursor]));
      setGraph(result);
      setSelectedId((current) => result.nodes.some((node) => node.id === current) || result.edges.some(edge => edge.id === current) ? current : '');
    }).catch((reason: unknown) => {
      if (generation !== requestGeneration.current || controller.signal.aborted) return;
      setError(reason instanceof Error ? reason.message : 'The knowledge map could not be loaded.');
    }).finally(() => {
      if (generation === requestGeneration.current) setLoading(false);
    });
    return () => { controller.abort(); requestGeneration.current++; };
  }, [sourceFilter, searchQuery, focusId, focusSourceId, focusCursor, depth, reloadKey, sourceRevision]);

  async function loadMoreSources() {
    if (!graph?.next_cursor || loadingMore) return;
    const generation = requestGeneration.current;
    setLoadingMore(true);
    setError('');
    try {
      const cursor = graph.next_cursor;
      const result = await getWorkspaceGraph({ connectionId: (focusId ? focusSourceId : sourceFilter) || undefined, query: searchQuery || undefined,
        focusId: focusId || undefined, depth, cursor, limit: RENDER_NODE_LIMIT, sourceLimit: 100 });
      if (generation === requestGeneration.current) {
        for (const node of result.nodes) if (!nodeCursors.current.has(node.id)) nodeCursors.current.set(node.id, cursor);
        setGraph((current) => current ? mergeGraphs(current, result) : result);
      }
    } catch (reason: unknown) {
      if (generation === requestGeneration.current) setError(reason instanceof Error ? reason.message : 'More sources could not be loaded.');
    } finally {
      if (generation === requestGeneration.current) setLoadingMore(false);
    }
  }

  function showOverview() { setFocusId(''); setFocusSourceId(''); setFocusCursor(''); setSelectedId(''); setDepth(2); }
  function explore(id: string, connectionId?: string, kind?: GraphNode['kind']) {
    const node = nodeById.get(id);
    const scope = navigationScope(node?.kind ?? kind, node?.connection_id ?? connectionId,
      focusId ? focusSourceId : sourceFilter, nodeCursors.current.get(id) ?? '');
    setSelectedId(id);
    setFocusSourceId(scope.sourceId);
    setFocusCursor(scope.cursor);
    setFocusId(id);
  }
  function selectLens(next: KnowledgeLens) {
    setLens(next);
    if (next === 'vocabulary') setView('records');
  }
  function exportView() {
    if (!graph) return;
    const payload = { view: lens, review_state: reviewState || null, query: searchQuery || null,
      source_filter: sourceFilter || null, focus_id: focusId || null, connections: graph.connections,
      coverage: graph.coverage, truncated: graph.truncated, nodes: visible.nodes, edges: visible.edges };
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = `knowledge-${lens}.json`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function openEvidence(id: string, connectionId?: string) {
    if (nodeById.has(id)) setSelectedId(id); else explore(id, connectionId, 'evidence');
  }
  function reviewNode(node: GraphNode) {
    const proposal = node.kind === 'assertion' ? node : nodeById.get(node.definition_assertion_id ?? '');
    if (node.connection_id) onReviewSource(node.connection_id, proposal && possibleStates.has(proposal.status) ? proposal.record_id : undefined,
      { sourceFilter, query, focusId, focusSourceId, focusCursor, depth, selectedId, lens, view, reviewState });
  }
  function edgeEvidenceIds(edge: GraphEdge) {
    return [...new Set([...edge.evidence_ids, ...[edge.subject_id, edge.object_id].filter(id => ['evidence', 'document'].includes(nodeById.get(id)?.kind ?? ''))])];
  }
  function edgeList(edges: GraphEdge[], possible = false) {
    return edges.length ? <div className="inspector-links">{edges.map((edge) => {
      const otherId = edge.subject_id === selectedId ? edge.object_id : edge.subject_id;
      const other = nodeById.get(otherId);
      return <button type="button" key={edge.id} onClick={() => setSelectedId(edge.id)}>
        <span>{possible ? 'Possible matching meaning · requires review' : `${readable(edge.predicate)} · ${readable(edge.status)}`}</span>
        <strong>{other?.label ?? 'Item outside this bounded view'}</strong>
        {other?.source_name && <small>{other.source_name}</small>}
      </button>;
    })}</div> : <p className="inspector-muted">None in this bounded view.</p>;
  }

  return <section className="knowledge-map-page" aria-labelledby="map-title">
    <header className="knowledge-map-header">
      <div><h1 id="map-title">Knowledge map</h1>
        <p>One connected layer of terms, data, documents and evidence.</p></div>
      <label className="knowledge-map-source"><span>Source filter</span><select value={sourceFilter} onChange={(event) => {
        setSourceFilter(event.target.value); showOverview();
      }}><option value="">All connected sources</option>{sourceChoices.map((source) => <option key={source.id} value={source.id}>{source.name}</option>)}</select></label>
    </header>
    <div className="knowledge-map-filters">
      <label className="knowledge-map-search"><Search size={16} aria-hidden="true" /><input aria-label="Search knowledge map" placeholder="Search concepts, fields, documents or metrics" maxLength={200} value={query} onChange={(event) => { setQuery(event.target.value); showOverview(); }} /></label>
      <span className="knowledge-map-coverage" aria-live="polite">{loading ? 'Loading connected knowledge…' : `${filteredScope ? `Filtered source${loadedSources > 1 ? ` + ${loadedSources - 1} cited source${loadedSources === 2 ? '' : 's'}` : ''}` : `${loadedSources} of ${scopedTotal} sources loaded`} · ${graph?.nodes.length ?? 0} items in view`}</span>
    </div>
    <nav className="knowledge-map-lenses" aria-label="Knowledge views">{knowledgeLenses.map(item => <button type="button" key={item.id} aria-pressed={lens === item.id} onClick={() => selectLens(item.id)}>{item.label}</button>)}</nav>
    <div className="knowledge-map-lens-description"><p>{currentLens.description}</p><label>Review focus<select aria-label="Knowledge review focus" value={reviewState} onChange={event => setReviewState(event.target.value)}><option value="">All review states</option>{[...new Set([...reviewStates, ...(reviewState ? [reviewState] : [])])].map(state => <option key={state} value={state}>{readable(state)}</option>)}</select></label></div>
    {!loading && !error && totalSources === 0 ? <div className="knowledge-map-empty"><Database size={28} /><h2>Connect a source to start your map</h2><p>Your connected sources will share one map of concepts, documents and evidence.</p><button type="button" onClick={onConnect}>Connect a source</button></div>
      : <div className="knowledge-map-shell">
        <div className="knowledge-map-workspace">
          <div className="knowledge-map-toolbar">
            <div className="knowledge-map-title"><Waypoints size={16} /><strong>{focusId ? 'Relationship neighborhood' : currentLens.label}</strong></div>
            <div className="knowledge-map-actions">
              {focusId && <><button type="button" onClick={showOverview}><ArrowLeft size={14} /> Overview</button>
                <button type="button" aria-pressed={depth === 1} onClick={() => setDepth(1)}>1 hop</button><button type="button" aria-pressed={depth === 2} onClick={() => setDepth(2)}>2 hops</button></>}
              <button type="button" aria-label="Export knowledge view as JSON" disabled={!graph} onClick={exportView}><Download size={14} /> Export</button>
              <button type="button" aria-label="Reload knowledge map" onClick={() => setReloadKey((key) => key + 1)}><RefreshCw size={14} /> Refresh</button>
            </div>
          </div>
          <div className="knowledge-map-viewbar"><div role="tablist" aria-label="Knowledge display">{(['graph', 'records', 'relationships'] as const).map(mode => <button type="button" key={mode} role="tab" id={`knowledge-tab-${mode}`} aria-controls="knowledge-view-panel" aria-selected={view === mode} onClick={() => setView(mode)}>{mode === 'graph' ? <Network size={13} /> : <List size={13} />}{mode === 'records' && lens === 'vocabulary' ? 'Terms' : readable(mode)}</button>)}</div><span aria-live="polite">{visible.nodes.length} items · {visible.edges.length} links</span></div>
          {reviewState && <p className="knowledge-map-context-note">Record, definition or relationship state: {readable(reviewState)}.{visible.contextCount > 0 && ` ${visible.contextCount} endpoint records provide relationship context; their individual states appear in the inspector.`}</p>}
          {selectedOutsideView && <div className="knowledge-map-selection-note">The selected item is outside this view. <button type="button" onClick={() => { setLens('all'); setReviewState(''); }}>Show in all knowledge</button></div>}
          <div className="knowledge-map-canvas-wrap" id="knowledge-view-panel" role="tabpanel" aria-labelledby={`knowledge-tab-${view}`}>
            {loading && <div className="knowledge-map-overlay"><LoaderCircle size={21} className="spin" /> Loading connected knowledge…</div>}
            {error && !graph && <div className="knowledge-map-overlay knowledge-map-error" role="alert"><AlertCircle size={22} />{error}</div>}
            {!loading && !error && !visible.nodes.length && <div className="knowledge-map-overlay"><Waypoints size={24} />{searchQuery ? 'No items matched this search and view in the loaded source scope.' : 'No records match this view in the loaded source snapshots.'}<button type="button" onClick={() => { setLens('all'); setReviewState(''); setQuery(''); showOverview(); }}>Show all knowledge</button></div>}
            {!loading && visible.nodes.length > 0 && (view === 'graph' ? <Suspense fallback={<div className="knowledge-map-overlay"><LoaderCircle size={21} className="spin" /> Loading knowledge canvas…</div>}><KnowledgeGraphCanvas nodes={visible.nodes} edges={visible.edges} onSelect={setSelectedId} onExpand={id => explore(id)} selectedId={selectedId} focusId={focusId} collapseOverview={!searchQuery && !focusId && !sourceFilter && lens === 'all'} /></Suspense> : <div className="knowledge-map-table-wrap"><table><caption className="sr-only">{view === 'records' ? 'Knowledge records with their source and review states' : 'Recorded knowledge relationships with review states'}</caption><thead>{view === 'records' ? <tr><th>{lens === 'vocabulary' ? 'Term and definition' : 'Knowledge item'}</th><th>Type / source</th><th>Review state</th></tr> : <tr><th>From / to</th><th>Relationship</th><th>Review state</th></tr>}</thead><tbody>{view === 'records' ? visible.nodes.map(node => <tr key={node.id} className={selectedId === node.id ? 'is-selected' : ''}><td><button type="button" onClick={() => setSelectedId(node.id)}>{node.label}</button><p>{node.description || 'Definition not established'}</p></td><td>{kindLabels[node.kind]}<small>{node.source_name ?? (node.kind === 'shared_term' ? 'Multiple scoped sources' : '')}</small></td><td><span>Record · {readable(node.status)}</span>{node.definition_status && <small>Definition · {readable(node.definition_status)}</small>}</td></tr>) : visible.edges.map(edge => <tr key={edge.id} className={selectedId === edge.id ? 'is-selected' : ''}><td><button type="button" onClick={() => setSelectedId(edge.subject_id)}>{nodeById.get(edge.subject_id)?.label}</button><small>→</small><button type="button" onClick={() => setSelectedId(edge.object_id)}>{nodeById.get(edge.object_id)?.label}</button></td><td><button type="button" onClick={() => setSelectedId(edge.id)}>{isPossible(edge, nodeById) ? 'Possible shared meaning' : readable(edge.predicate)}</button><small>{edgeEvidenceIds(edge).length} evidence records</small></td><td>{readable(edge.status)}<small>{readable(edge.trust_state)}</small></td></tr>)}</tbody></table>{view === 'relationships' && !visible.edges.length && <p className="knowledge-map-context-note">No recorded relationships match this view.</p>}</div>)}
          </div>
          <div className="knowledge-map-legend"><span><i className="legend-source" /> Source</span><span><i className="legend-shared" /> Possible shared meaning</span><span><i className="legend-concept" /> Concept</span><span><i className="legend-asset" /> Asset / document</span><span><i className="legend-metric" /> Metric</span><span><i className="legend-evidence" /> Evidence</span></div>
          <details className="knowledge-map-list"><summary>Browse map items ({visible.nodes.length})</summary><div>{visible.nodes.map((node) => <button type="button" key={node.id} aria-pressed={selectedId === node.id} onClick={() => setSelectedId(node.id)}><span>{node.label}{node.source_name && <small>{node.source_name}</small>}</span><small>{kindLabels[node.kind]}</small></button>)}</div></details>
        </div>
        <aside className="knowledge-map-inspector" aria-label="Selected knowledge item">
          {selectedNode ? <><div className="inspector-kicker">{kindLabels[selectedNode.kind]}</div><h2>{selectedNode.label}</h2>
            {selectedNode.source_name && <p className="inspector-muted">{selectedNode.source_name}</p>}
            <div className="inspector-status"><span className={possibleStates.has(selectedNode.status) || selectedNode.kind === 'shared_term' ? 'is-candidate' : ''}>{selectedNode.kind === 'shared_term' ? 'Possible matching meaning' : `Record · ${readable(selectedNode.status)}`}</span><span>{readable(selectedNode.trust_state)}</span></div>
            {selectedNode.definition_status && <div className="inspector-definition-review"><strong>Definition · {readable(selectedNode.definition_status)}</strong>
              {selectedNode.definition_trust_state && <span>{readable(selectedNode.definition_trust_state)}</span>}
              {selectedNode.definition_assertion_id && <button type="button" onClick={() => {
                const id = selectedNode.definition_assertion_id!;
                if (nodeById.has(id)) setSelectedId(id); else explore(id, selectedNode.connection_id, 'assertion');
              }}>Inspect definition review</button>}
            </div>}
            {selectedNode.kind === 'shared_term' && <p className="inspector-caution">Matching labels suggest a place to investigate. Each source keeps its own meaning and review state.</p>}
            {selectedNode.conflict && <p className="inspector-conflict"><AlertCircle size={15} />{typeof selectedNode.conflict === 'string' ? selectedNode.conflict : 'Scoped definitions or units differ; compare their source meanings.'}</p>}
            <p className="inspector-description">{selectedNode.description || 'A definition has not been established for this item.'}</p>
            <button className="inspector-focus" type="button" onClick={() => explore(selectedNode.id)}><Focus size={14} /> Explore neighborhood</button>
            {selectedNode.connection_id && <button className="inspector-review" type="button" onClick={() => reviewNode(selectedNode)}>{selectedNode.kind === 'assertion' && possibleStates.has(selectedNode.status) ? 'Review this assertion' : 'Review this source'}</button>}
            <details className="inspector-provenance"><summary>Provenance</summary><dl>
              <dt>Source</dt><dd>{selectedNode.source_name ?? graph?.connections.find((source) => source.id === selectedNode.connection_id)?.name ?? (selectedNode.kind === 'shared_term' ? 'Multiple scoped sources' : 'Not supplied')}</dd>
              {selectedNode.snapshot_id && <><dt>Snapshot</dt><dd>{selectedNode.snapshot_id}</dd></>}
              {selectedNode.record_id && <><dt>Source record</dt><dd>{selectedNode.record_id}</dd></>}
              {selectedNode.semantic_type && <><dt>Semantic type</dt><dd>{readable(selectedNode.semantic_type)}</dd></>}
              {selectedNode.asset_name && <><dt>Dataset</dt><dd>{selectedNode.asset_name}</dd></>}
              {selectedNode.source_pointer && <><dt>Source location</dt><dd>{selectedNode.source_pointer}</dd></>}
              {selectedNode.unit && <><dt>Declared unit</dt><dd>{selectedNode.unit}</dd></>}
              {selectedNode.native_type && <><dt>Source type</dt><dd>{selectedNode.native_type}</dd></>}
              {'nullable' in selectedNode && <><dt>Missing values allowed</dt><dd>{selectedNode.nullable === null ? 'Unknown' : selectedNode.nullable ? 'Yes' : 'No'}</dd></>}
              {selectedNode.definition_variant_count !== undefined && <><dt>Scoped definitions</dt><dd>{selectedNode.definition_variant_count}</dd></>}
              {selectedNode.unit_variants?.length ? <><dt>Declared units</dt><dd>{selectedNode.unit_variants.join(', ')}</dd></> : null}
            </dl></details>
            {selectedNode.members && selectedNode.kind === 'shared_term' && <><div className="inspector-rule" /><h3>Scoped source meanings <span>{selectedNode.members.length}</span></h3><div className="inspector-links">{selectedNode.members.map((member) => {
              const id = typeof member === 'string' ? member : member.id ?? member.node_id;
              const node = id ? nodeById.get(id) : undefined;
              const source = typeof member === 'string' ? undefined : graph?.connections.find((connection) => connection.id === member.connection_id)?.name;
              return <button type="button" key={id} disabled={!id} onClick={() => { if (id) { if (node) setSelectedId(id); else explore(id, typeof member === 'string' ? undefined : member.connection_id, typeof member === 'string' ? undefined : member.kind); } }}><span>{node?.source_name ?? source ?? 'Scoped member'}</span><strong>{node?.description ?? node?.label ?? 'Explore this source meaning'}</strong></button>;
            })}</div></>}
            <div className="inspector-rule" /><h3>Source relationships <span>{sourceEdges.length}</span></h3>{edgeList(sourceEdges)}
            <div className="inspector-rule" /><h3>Evidence and document citations <span>{citationEdges.length}</span></h3>{edgeList(citationEdges)}
            {(selectedNode.evidence_ids?.length ?? 0) > 0 && <div className="inspector-links inspector-evidence-handles">{selectedNode.evidence_ids.map((id) => {
              const evidence = nodeById.get(id);
              return evidence ? <button type="button" key={id} onClick={() => setSelectedId(evidence.id)}><span>Supporting evidence</span><strong>{evidence.description ?? evidence.label}</strong></button>
                : <button type="button" key={id} onClick={() => explore(id, selectedNode.connection_id, 'evidence')}><span>Evidence outside this bounded view</span><strong>Explore supporting record</strong></button>;
            })}</div>}
            {possibleEdges.length > 0 && <><div className="inspector-rule" /><h3>Possible shared meanings <span>{possibleEdges.length}</span></h3>{edgeList(possibleEdges, true)}</>}
          </> : selectedEdge ? <><div className="inspector-kicker">Recorded relationship</div><h2>{isPossible(selectedEdge, nodeById) ? 'Possible shared meaning' : readable(selectedEdge.predicate)}</h2>
            <div className="inspector-status"><span className={possibleStates.has(selectedEdge.status) ? 'is-candidate' : ''}>{readable(selectedEdge.status)}</span><span>{readable(selectedEdge.trust_state)}</span></div>
            {isPossible(selectedEdge, nodeById) && <p className="inspector-caution">Matching labels suggest a review candidate. They do not establish equivalent meanings or an approved join.</p>}
            <div className="inspector-endpoints">{[selectedEdge.subject_id, selectedEdge.object_id].map((id, index) => <div key={`${id}-${index}`}><small>{index === 0 ? 'From' : 'To'}</small><button type="button" onClick={() => setSelectedId(id)}>{nodeById.get(id)?.label ?? id}</button><span>{nodeById.get(id)?.source_name}</span></div>)}</div>
            {selectedEdge.assertion_id && <button className="inspector-focus" type="button" onClick={() => { if (nodeById.has(selectedEdge.assertion_id!)) setSelectedId(selectedEdge.assertion_id!); else explore(selectedEdge.assertion_id!, nodeById.get(selectedEdge.subject_id)?.connection_id, 'assertion'); }}>Inspect relationship review</button>}
            <div className="inspector-rule" /><h3>Supporting evidence <span>{edgeEvidenceIds(selectedEdge).length}</span></h3><div className="inspector-links">{edgeEvidenceIds(selectedEdge).map(id => <button type="button" key={id} onClick={() => openEvidence(id, nodeById.get(selectedEdge.subject_id)?.connection_id)}><strong>{nodeById.get(id)?.description || nodeById.get(id)?.label || 'Explore supporting record'}</strong><small>{nodeById.get(id)?.source_name ?? 'Evidence outside this bounded view'}</small></button>)}</div>
            {!edgeEvidenceIds(selectedEdge).length && <p className="inspector-muted">No supporting evidence is attached to this relationship. Inspect its endpoints for source and snapshot context.</p>}
            <details className="inspector-provenance"><summary>Relationship properties</summary><dl><dt>Relationship identity</dt><dd>{selectedEdge.id}</dd><dt>Predicate</dt><dd>{selectedEdge.predicate}</dd>{selectedEdge.confidence !== null && <><dt>Recorded confidence</dt><dd>{selectedEdge.confidence}</dd></>}</dl></details>
          </> : <div className="inspector-empty"><Waypoints size={24} /><h2>Follow a connection</h2><p>Search a term, then select a record or relationship to inspect its meaning, source, review state and evidence.</p><p>Double-click a record to explore its neighborhood.</p></div>}

        </aside>
      </div>}
    {graph && <div className="knowledge-map-footer"><p className="knowledge-map-limit">{graph.coverage.client_nodes_clipped || graph.coverage.client_edges_clipped ? 'The rendered map is capped at 400 items and 2,000 links. More source pages still add source filter choices; select a source, search or explore its neighborhood to inspect records.' : graph.truncated ? 'This map is bounded. Search or explore a neighborhood to inspect more records.' : 'Coverage reflects the loaded source snapshots.'} Dashed links show possible or pending meaning.</p>
      {graph.next_cursor && <button type="button" className="knowledge-map-load-more" disabled={loadingMore} onClick={() => void loadMoreSources()}>{loadingMore ? <LoaderCircle size={15} className="spin" /> : <Database size={15} />}{loadingMore ? 'Loading sources…' : 'Load more sources'}</button>}
    </div>}
    {error && graph && <p className="knowledge-map-pagination-error" role="alert">{error}</p>}
  </section>;
}
