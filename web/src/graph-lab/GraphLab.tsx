import { Component, lazy, Suspense, useEffect, useMemo, useState, type ReactNode } from 'react';
import { getWorkspaceGraph, type GraphEdge, type WorkspaceGraph } from '../platformApi';
import { graphKindColors } from './types';
import './graphLab.css';
const CytoscapeLab = lazy(() => import('./CytoscapeLab'));
const G6Lab = lazy(() => import('./G6Lab'));
const ReactodiaLab = lazy(() => import('./ReactodiaLab'));
const ZazukoLab = lazy(() => import('./ZazukoLab'));
type Engine = 'cytoscape' | 'g6' | 'reactodia' | 'zazuko' | 'aws' | 'skosmos';
const options: Array<{ id: Engine; name: string; category: string; description: string; license: string; url: string }> = [
  { id: 'cytoscape', name: 'Cytoscape + fCoSE', category: 'Network exploration', description: 'Compound source groups, constrained layouts, paths and graph algorithms.', license: 'MIT', url: 'https://github.com/iVis-at-Bilkent/cytoscape.js-fcose' },
  { id: 'g6', name: 'AntV G6', category: 'Visual graph framework', description: 'Multiple layouts, source combos, minimap and interactive visual exploration.', license: 'MIT', url: 'https://github.com/antvis/G6' },
  { id: 'reactodia', name: 'Reactodia Workspace', category: 'Semantic diagram workspace', description: 'Native search, classes, connections, diagram tools and annotations.', license: 'LGPL-2.1-or-later', url: 'https://github.com/reactodia/reactodia-workspace' },
  { id: 'zazuko', name: 'Zazuko Graph Explorer', category: 'RDF diagram workspace', description: 'Native class tree, connection navigation, saved diagrams and exports.', license: 'LGPL-2.1-or-later', url: 'https://github.com/zazuko/graph-explorer' },
  { id: 'aws', name: 'AWS Graph Explorer', category: 'Complete graph application', description: 'Native graph, data and schema explorers, query editor and saved views.', license: 'Apache-2.0', url: 'https://github.com/aws/graph-explorer' },
  { id: 'skosmos', name: 'Skosmos', category: 'Vocabulary application', description: 'Native term search, alphabetical index, vocabulary groups and concept properties.', license: 'MIT', url: 'https://github.com/NatLibFi/Skosmos' },
];
const human = (s: string) => s.replaceAll('_', ' ');
function download(name: string, body: string, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([body], { type }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
class RendererBoundary extends Component<{ children: ReactNode }, { error: string }> {
  state = { error: '' };
  static getDerivedStateFromError(error: Error) { return { error: error.message }; }
  render() { return this.state.error ? <div role="alert" className="lab-error">This renderer could not load: {this.state.error}</div> : this.props.children; }
}
export default function GraphLab() {
  const [engine, setEngine] = useState<Engine>('cytoscape');
  const [query, setQuery] = useState('cost_actual_usd');
  const [committedQuery, setCommittedQuery] = useState('cost_actual_usd');
  const [source, setSource] = useState('');
  const [sources, setSources] = useState<WorkspaceGraph['connections']>([]);
  const [graph, setGraph] = useState<WorkspaceGraph | null>(null);
  const [focus, setFocus] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('');
  const [kind, setKind] = useState('');
  const [status, setStatus] = useState('');
  const [view, setView] = useState<'graph' | 'records' | 'relationships' | 'schema'>('graph');
  const [reload, setReload] = useState(0);
  const [saved, setSaved] = useState<Array<{ name: string; query: string; source: string; focus: string; engine: Engine }>>(() => {
    try { return JSON.parse(localStorage.getItem('kl-graph-lab-views') ?? '[]'); } catch { return []; }
  });
  const [viewName, setViewName] = useState('');
  const [nativeInfo, setNativeInfo] = useState<{ nodes: number; edges: number; terms: number; exported_at: string } | null>(null);
  useEffect(() => {
    const abort = new AbortController(); setLoading(true); setError('');
    void getWorkspaceGraph({ query: focus ? undefined : committedQuery, connectionId: source || undefined, focusId: focus || undefined, depth: 2, limit: 400, sourceLimit: 100, signal: abort.signal }).then(data => {
      if (!abort.signal.aborted) { setGraph(data); setSources(previous => [...new Map([...previous, ...data.connections].map(c => [c.id, c])).values()]); setLoading(false); }
    }).catch(err => { if (!abort.signal.aborted) { setError(err.message); setLoading(false); } });
    return () => abort.abort();
  }, [committedQuery, source, focus, reload]);
  useEffect(() => { void fetch('http://127.0.0.1:4182/lab/info').then(r => r.ok ? r.json() : null).then(setNativeInfo).catch(() => setNativeInfo(null)); }, [engine, reload]);
  const current = options.find(o => o.id === engine)!;
  const native = engine === 'aws' || engine === 'skosmos';
  const nodes = useMemo(() => (graph?.nodes ?? []).filter(n => (!kind || n.kind === kind) && (!status || n.status === status || n.definition_status === status)), [graph, kind, status]);
  const edges = useMemo(() => { const ids = new Set(nodes.map(n => n.id)); return (graph?.edges ?? []).filter(e => ids.has(e.subject_id) && ids.has(e.object_id)); }, [graph, nodes]);
  const byId = useMemo(() => new Map((graph?.nodes ?? []).map(n => [n.id, n])), [graph]);
  const selectedNode = byId.get(selected);
  const selectedEdge = graph?.edges.find(e => e.id === selected);
  function explore(id: string) { setFocus(id); setSelected(id); setKind(''); setStatus(''); }
  function preset(q: string) { setQuery(q); setCommittedQuery(q); setFocus(''); setKind(''); setStatus(''); setSelected(''); }
  function save() {
    const name = viewName.trim(); if (!name) return;
    const next = [...saved.filter(v => v.name !== name), { name, query: committedQuery, source, focus, engine }].slice(-20);
    setSaved(next); localStorage.setItem('kl-graph-lab-views', JSON.stringify(next)); setViewName('');
  }
  function edgeRows(items: GraphEdge[]) { return items.map(e => <tr key={e.id} onClick={() => setSelected(e.id)}><td><button onClick={() => setSelected(e.subject_id)}>{byId.get(e.subject_id)?.label ?? e.subject_id}</button></td><td><button onClick={() => setSelected(e.id)}>{human(e.predicate)}</button></td><td><button onClick={() => setSelected(e.object_id)}>{byId.get(e.object_id)?.label ?? e.object_id}</button></td><td>{human(e.status)}</td><td>{e.evidence_ids?.length ?? 0}</td></tr>); }
  const props = { nodes, edges, onSelect: setSelected, onExpand: explore };
  return <section className="graph-lab" aria-labelledby="graph-lab-title">
    <header className="lab-header"><div><div className="eyebrow">INTERACTIVE COMPARISON</div><h1 id="graph-lab-title">Graph lab</h1><p>Try six real knowledge interfaces. Compare their native tools, using your connected knowledge.</p></div><button onClick={() => setReload(x => x + 1)}>Reload view</button></header>
    <div className="lab-options" aria-label="Graph library choices">{options.map(o => <button key={o.id} className={engine === o.id ? 'selected' : ''} aria-pressed={engine === o.id} onClick={() => { setEngine(o.id); setView('graph'); }}><span>{o.category}</span><strong>{o.name}</strong><small>{o.description}</small><em>{o.license}</em></button>)}</div>
    <div className="lab-engine-heading"><div><h2>{current.name}</h2><p>{native ? 'Authentic standalone application, connected to an isolated read-only RDF copy of the knowledge layer.' : 'Authentic library integration. Native diagram tools operate on this workspace view; the inspector retains source and review context.'}</p></div><a href={current.url} target="_blank" rel="noreferrer">Upstream project ↗</a></div>
    {native ? <>
      <div className="lab-native-note">{nativeInfo ? `${nativeInfo.nodes.toLocaleString()} records · ${nativeInfo.edges.toLocaleString()} relationships · ${nativeInfo.terms.toLocaleString()} scoped vocabulary terms · exported ${new Date(nativeInfo.exported_at).toLocaleString()}` : 'Connecting to the local comparison runtime…'}<br />These apps browse a retained comparison copy. Search below uses their own native controls. Diagram edits and queries affect the copy or browser only. Pending claims remain labelled; source grouping is not an ontology hierarchy.</div>
      {!nativeInfo ? <div className="lab-error" role="alert">The standalone comparison runtime is unavailable. Use Reload view after it starts.</div> : <><div className="lab-native-tools"><a href={engine === 'aws' ? 'http://127.0.0.1:4182/aws/explorer/' : 'http://127.0.0.1:4183/fieldwork/en/'} target="_blank" rel="noreferrer">Open {current.name} at full width ↗</a><a href="http://127.0.0.1:4182/lab/knowledge.ttl" download>Download comparison RDF</a>{engine === 'aws' && <a href="http://127.0.0.1:4182/lab/aws-styles.json" download>Download readable-label styles</a>}</div><iframe key={`${engine}-${reload}`} title={`${current.name} native application`} src={engine === 'aws' ? 'http://127.0.0.1:4182/aws/explorer/' : 'http://127.0.0.1:4183/fieldwork/en/'} className="lab-native-frame" /></>}
    </> : <>
      <form className="lab-filters" onSubmit={e => { e.preventDefault(); setCommittedQuery(query.trim()); setFocus(''); setSelected(''); }}><label>Search connected knowledge<input aria-label="Search graph lab" value={query} onChange={e => setQuery(e.target.value)} placeholder="Term, field, document or metric" maxLength={200} /></label><button type="submit">Search</button><label>Source<select aria-label="Graph lab source" value={source} onChange={e => { setSource(e.target.value); setFocus(''); }}><option value="">All connected sources</option>{sources.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label><label>Type<select aria-label="Graph lab type" value={kind} onChange={e => setKind(e.target.value)}><option value="">All types</option>{[...new Set(graph?.nodes.map(n => n.kind))].sort().map(k => <option key={k}>{k}</option>)}</select></label><label>Review state<select aria-label="Graph lab review state" value={status} onChange={e => setStatus(e.target.value)}><option value="">All states</option>{[...new Set(graph?.nodes.flatMap(n => [n.status, ...(n.definition_status ? [n.definition_status] : [])]))].sort().map(s => <option key={s}>{s}</option>)}</select></label></form>
      <div className="lab-presets"><span>Try a real question:</span><button onClick={() => preset('cost_actual_usd')}>Cost → definition → evidence</button><button onClick={() => preset('projected_points')}>Projection meaning</button><button onClick={() => preset('snapshot_id')}>Conflicting snapshot IDs</button><button onClick={() => preset('')}>Workspace overview</button>{focus && <button onClick={() => setFocus('')}>Back to search</button>}</div>
      <div className="lab-viewbar"><div role="tablist" aria-label="Comparison data views">{(['graph', 'records', 'relationships', 'schema'] as const).map(v => <button key={v} role="tab" aria-selected={view === v} onClick={() => setView(v)}>{human(v)}</button>)}</div><span aria-live="polite">{loading ? 'Loading…' : `${nodes.length} records · ${edges.length} links · ${graph?.loaded_sources ?? 0} sources`}</span><button onClick={() => download('knowledge-comparison.json', JSON.stringify({ ...graph, nodes, edges }, null, 2))} disabled={!graph}>Export JSON</button></div>
      {error && <div className="lab-error" role="alert">{error}</div>}
      <div className="lab-workspace"><div className="lab-main">
        {loading ? <div className="lab-loading">Loading knowledge…</div> : view === 'graph' ? <RendererBoundary key={engine}><Suspense fallback={<div className="lab-loading">Loading native renderer…</div>}>{engine === 'cytoscape' ? <CytoscapeLab {...props} /> : engine === 'g6' ? <G6Lab {...props} /> : engine === 'reactodia' ? <ReactodiaLab {...props} /> : <ZazukoLab {...props} />}</Suspense></RendererBoundary> : view === 'records' ? <div className="lab-table-wrap"><table><thead><tr><th>Record</th><th>Type</th><th>Source</th><th>Record state</th><th>Definition state</th></tr></thead><tbody>{nodes.map(n => <tr key={n.id}><td><button onClick={() => setSelected(n.id)}>{n.label}</button></td><td>{n.kind}</td><td>{n.source_name}</td><td>{human(n.status)}</td><td>{n.definition_status && human(n.definition_status)}</td></tr>)}</tbody></table></div> : view === 'relationships' ? <div className="lab-table-wrap"><table><thead><tr><th>From</th><th>Relationship</th><th>To</th><th>State</th><th>Citations</th></tr></thead><tbody>{edgeRows(edges)}</tbody></table></div> : <div className="lab-schema">{Object.entries(graphKindColors).filter(([k]) => nodes.some(n => n.kind === k)).map(([k, color]) => <section key={k}><h3><i style={{ background: color }} />{human(k)} <span>{nodes.filter(n => n.kind === k).length}</span></h3><p>{[...new Set(edges.filter(e => byId.get(e.subject_id)?.kind === k).map(e => human(e.predicate)))].join(' · ') || 'No outgoing relationships in this view'}</p><p>Properties: {[...new Set(nodes.filter(n => n.kind === k).flatMap(n => Object.entries(n).filter(([, v]) => v !== null && v !== undefined).map(([key]) => key)))].join(', ')}</p></section>)}</div>}
      </div><aside className="lab-inspector" aria-label="Graph lab inspector">
        {selectedNode ? <><small>{human(selectedNode.kind)}</small><h3>{selectedNode.label}</h3><p className="lab-status">Record: {human(selectedNode.status)}{selectedNode.definition_status && <><br />Definition: {human(selectedNode.definition_status)}</>}</p><p>{selectedNode.description || 'No definition recorded for this item.'}</p>{selectedNode.kind === 'shared_term' && <p className="lab-warning">Matching labels are candidates for review. They do not establish equivalent meanings.</p>}<button onClick={() => explore(selectedNode.id)}>Explore 2-hop neighborhood</button><details open><summary>Source and provenance</summary><dl>{[['Source', selectedNode.source_name], ['Connection', selectedNode.connection_id], ['Snapshot', selectedNode.snapshot_id], ['Source record', selectedNode.record_id], ['Native type', selectedNode.native_type], ['Trust', selectedNode.trust_state]].filter(([, v]) => v).map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl></details><h4>Relationships and evidence</h4>{(graph?.edges ?? []).filter(e => e.subject_id === selected || e.object_id === selected).map(e => <button className="lab-related" key={e.id} onClick={() => setSelected(e.id)}><span>{human(e.predicate)} · {human(e.status)}</span><strong>{byId.get(e.subject_id === selected ? e.object_id : e.subject_id)?.label}</strong></button>)}{selectedNode.evidence_ids.map(id => <button className="lab-related" key={id} onClick={() => byId.has(id) ? setSelected(id) : explore(id)}>Evidence: {byId.get(id)?.label ?? 'Load cited record'}</button>)}<details><summary>All record properties</summary><pre>{JSON.stringify(selectedNode, null, 2)}</pre></details></> : selectedEdge ? <><small>Relationship</small><h3>{human(selectedEdge.predicate)}</h3><p className="lab-status">{human(selectedEdge.status)} · {human(selectedEdge.trust_state)}</p><button onClick={() => setSelected(selectedEdge.subject_id)}>{byId.get(selectedEdge.subject_id)?.label ?? 'Subject'}</button><p>→</p><button onClick={() => setSelected(selectedEdge.object_id)}>{byId.get(selectedEdge.object_id)?.label ?? 'Object'}</button><h4>Supporting evidence</h4>{selectedEdge.evidence_ids?.map(id => <button className="lab-related" key={id} onClick={() => byId.has(id) ? setSelected(id) : explore(id)}>{byId.get(id)?.label ?? 'Load cited evidence'}</button>)}<pre>{JSON.stringify(selectedEdge, null, 2)}</pre></> : <><h3>Select a record or link</h3><p>Inspect exact source identity, review state, definitions and evidence. Use the Records and Relationships tabs for accessible selection.</p><p>Double-click a node to load its neighborhood from the knowledge API.</p></>}
      </aside></div>
      <div className="lab-legend">{Object.entries(graphKindColors).filter(([k]) => nodes.some(n => n.kind === k)).map(([k, color]) => <span key={k}><i style={{ background: color }} />{human(k)}</span>)}</div><p className="lab-coverage">{graph?.truncated ? 'This is a bounded view of up to 400 records. Search or expand a neighborhood to inspect other knowledge.' : 'Coverage reflects retained source snapshots.'} Native library search covers the loaded view. Diagram edits do not approve claims or change source records.</p>
      <details className="lab-saved"><summary>Saved comparison views ({saved.length})</summary><div><input aria-label="Comparison view name" placeholder="Name this comparison view" value={viewName} onChange={e => setViewName(e.target.value)} /><button disabled={!viewName.trim()} onClick={save}>Save view</button></div>{saved.map(v => <button key={v.name} onClick={() => { setQuery(v.query); setCommittedQuery(v.query); setSource(v.source); setFocus(v.focus); setEngine(v.engine); }}>{v.name} · {options.find(o => o.id === v.engine)?.name}</button>)}</details>
    </>}
    <details className="lab-feature-guide"><summary>What to compare across the six options</summary><table><thead><tr><th>Option</th><th>Native strengths to try</th><th>Scope of this integration</th></tr></thead><tbody><tr><td>Cytoscape + fCoSE</td><td>Source groups, layout constraints, graph paths, image export</td><td>Existing knowledge renderer extended with genuine fCoSE and native Cytoscape algorithms</td></tr><tr><td>G6</td><td>Source combos, layout alternatives, minimap, highlighting</td><td>G6 graph engine with platform data and inspector</td></tr><tr><td>Reactodia</td><td>Class/search panels, connections, annotations, diagram history</td><td>Native workspace; custom bounded knowledge provider</td></tr><tr><td>Zazuko</td><td>Class tree, connections menu, native diagram and image export</td><td>Native workspace; custom bounded knowledge provider</td></tr><tr><td>AWS Graph Explorer</td><td>Graph, data table, schema, query editor, saved views</td><td>Complete upstream app; read-only SPARQL comparison copy. Neptune-specific engines are unavailable</td></tr><tr><td>Skosmos</td><td>Vocabulary search, alphabetical browsing, source groups, term properties</td><td>Complete upstream app; SKOS vocabulary projection. Hierarchy requires actual broader/narrower assertions</td></tr></tbody></table></details>
  </section>;
}
