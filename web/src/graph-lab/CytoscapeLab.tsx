import { useEffect, useRef, useState } from 'react';
import cytoscape from 'cytoscape';
import fcose from 'cytoscape-fcose';
import { graphKindColors, type GraphLabRendererProps } from './types';
import './renderers.css';

cytoscape.use(fcose);
type LayoutName = 'fcose' | 'concentric' | 'breadthfirst' | 'circle' | 'grid';
const layouts: Array<[LayoutName, string]> = [['fcose', 'fCoSE · compound force'], ['concentric', 'Concentric · connectivity'], ['breadthfirst', 'Breadth first · directed'], ['circle', 'Circle'], ['grid', 'Grid']];
function downloadImage(url: string) {
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'knowledge-cytoscape.png'; document.body.append(anchor); anchor.click(); anchor.remove();
}

export default function CytoscapeLab({ nodes, edges, onSelect, onExpand }: GraphLabRendererProps) {
  const container = useRef<HTMLDivElement>(null);
  const engine = useRef<cytoscape.Core | null>(null);
  const callbacks = useRef({ onSelect, onExpand }); callbacks.current = { onSelect, onExpand };
  const [layout, setLayout] = useState<LayoutName>('fcose');
  const layoutRef = useRef(layout); layoutRef.current = layout;
  const [grouped, setGrouped] = useState(true);
  const [nodeLabels, setNodeLabels] = useState(true);
  const [edgeLabels, setEdgeLabels] = useState(false);
  const labels = useRef({ nodeLabels, edgeLabels }); labels.current = { nodeLabels, edgeLabels };
  const [selected, setSelected] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [message, setMessage] = useState('Drag to pan or move records. Double-click a record to load its neighborhood.');
  const [error, setError] = useState('');

  function runLayout(name = layoutRef.current) {
    const cy = engine.current; if (!cy) return;
    try {
      cy.stop();
      const options = name === 'fcose'
        ? { name, quality: 'default', randomize: true, animate: false, numIter: 650, nodeRepulsion: () => 6500, idealEdgeLength: () => 110, packComponents: false, tile: true, fit: true, padding: 35 }
        : { name, animate: false, fit: true, padding: 35, avoidOverlap: true, spacingFactor: 1.4, directed: name === 'breadthfirst' };
      cy.layout(options as cytoscape.LayoutOptions).run();
      setMessage(`${layouts.find(([id]) => id === name)?.[1]} layout complete · ${nodes.length} records.`);
      setError('');
    } catch (err) { setError(err instanceof Error ? err.message : 'Layout failed'); }
  }
  function highlight(elements: cytoscape.CollectionReturnValue, text: string) {
    const cy = engine.current; if (!cy) return;
    const visible = elements.union(elements.nodes().ancestors());
    cy.batch(() => { cy.elements().removeClass('lab-highlight lab-faded'); cy.elements().difference(visible).addClass('lab-faded'); visible.addClass('lab-highlight'); });
    setMessage(text);
  }
  function resetHighlight() {
    engine.current?.elements().removeClass('lab-highlight lab-faded');
    setMessage('Highlights cleared. Paths show connectivity in the loaded view.');
  }
  function neighbors() {
    const cy = engine.current; if (!cy || !selected) return;
    const node = cy.getElementById(selected);
    if (node.empty() || !node.isNode()) return;
    highlight(node.closedNeighborhood(), `Highlighted ${node.neighborhood().nodes().length} adjacent records.`);
  }
  function shortestPath() {
    const cy = engine.current; if (!cy || !from || !to) return;
    const source = cy.getElementById(from), target = cy.getElementById(to);
    if (source.empty() || target.empty()) return;
    // Compound boxes are visual source groups, never intermediate semantic records.
    const records = cy.elements().filter(element => !element.isNode() || !element.data('isGroup'));
    const result = records.dijkstra({ root: source, directed: false, weight: () => 1 });
    if (!Number.isFinite(result.distanceTo(target))) { resetHighlight(); setMessage('No path connects those records in this loaded view.'); return; }
    highlight(result.pathTo(target), `Undirected connectivity path · ${result.distanceTo(target)} links. This does not establish an approved join or equivalent meaning.`);
  }

  useEffect(() => {
    if (!container.current) return;
    const groups = new Map<string, { label: string; representative: string }>();
    if (grouped) for (const node of nodes) if (node.connection_id) {
      const prior = groups.get(node.connection_id);
      if (!prior || node.kind === 'source') groups.set(node.connection_id, { label: node.source_name || node.label, representative: node.id });
    }
    const elements: cytoscape.ElementDefinition[] = [
      ...[...groups].map(([id, group]) => ({ data: { id: `cy-source:${id}`, label: group.label, representative: group.representative, isGroup: true } })),
      ...nodes.map(node => ({ data: { ...node, color: graphKindColors[node.kind] || '#638566', parent: grouped && node.connection_id ? `cy-source:${node.connection_id}` : undefined } })),
      ...edges.filter(edge => nodes.some(n => n.id === edge.subject_id) && nodes.some(n => n.id === edge.object_id)).map(edge => ({ data: { ...edge, source: edge.subject_id, target: edge.object_id, label: edge.predicate.replaceAll('_', ' '), candidate: ['candidate', 'proposed', 'pending', 'pending_review', 'unreviewed'].includes(edge.status) || edge.trust_state === 'unreviewed' } })),
    ];
    const cy = cytoscape({ container: container.current, elements, layout: { name: 'preset' }, minZoom: 0.03, maxZoom: 4, wheelSensitivity: 0.22, style: [
      { selector: 'node', style: { shape: 'roundrectangle', width: 116, height: 44, label: labels.current.nodeLabels ? 'data(label)' : '', 'background-color': '#f6f8f0', 'border-color': 'data(color)', 'border-width': 1.5, color: '#354a3a', 'font-size': 10, 'font-family': 'system-ui', 'text-valign': 'center', 'text-wrap': 'ellipsis', 'text-max-width': '103px' } },
      { selector: 'node[kind = "source"]', style: { 'background-color': '#294d44', color: '#fff', width: 136, height: 52 } },
      { selector: 'node[kind = "field"]', style: { width: 108, height: 34 } },
      { selector: 'node[kind = "shared_term"]', style: { shape: 'ellipse', 'background-color': '#fcf4d8', 'border-style': 'dashed', width: 125, height: 50 } },
      { selector: 'node[kind = "metric"]', style: { shape: 'diamond', width: 138, height: 70 } },
      { selector: 'node[kind = "evidence"]', style: { width: 90, height: 32, 'background-color': '#f0f3ee' } },
      { selector: ':parent', style: { shape: 'roundrectangle', 'background-color': '#d6e3cb', 'background-opacity': 0.2, 'border-color': '#b1c4a7', 'border-width': 1, padding: '24px', label: 'data(label)', 'text-valign': 'top', 'text-halign': 'center', 'font-size': 11, 'text-margin-y': -8, 'text-max-width': '240px' } },
      { selector: 'edge', style: { width: 1.2, 'line-color': '#a4b4a1', 'target-arrow-color': '#a4b4a1', 'target-arrow-shape': 'triangle', 'curve-style': 'bezier', label: labels.current.edgeLabels ? 'data(label)' : '', 'font-size': 9, color: '#6f7c64', 'text-background-color': '#fff', 'text-background-opacity': 0.85, 'text-background-padding': '2px', 'text-rotation': 'autorotate' } },
      { selector: 'edge[?candidate]', style: { 'line-style': 'dashed', 'line-color': '#baa259', 'target-arrow-color': '#baa259' } },
      { selector: '.lab-faded', style: { opacity: 0.16 } },
      { selector: 'node.lab-highlight', style: { 'border-width': 3, 'border-color': '#42754d' } },
      { selector: 'edge.lab-highlight', style: { width: 3, 'line-color': '#42754d', 'target-arrow-color': '#42754d', 'z-index': 5 } },
      { selector: ':selected', style: { 'overlay-color': '#568164', 'overlay-opacity': 0.12, 'overlay-padding': 6 } },
    ] });
    engine.current = cy; setSelected(''); setFrom(''); setTo('');
    cy.on('tap', 'node', event => {
      const node = event.target as cytoscape.NodeSingular;
      const id = node.data('isGroup') ? node.data('representative') as string : node.id();
      setSelected(id); setFrom(id); callbacks.current.onSelect(id);
    });
    cy.on('tap', 'edge', event => { callbacks.current.onSelect(event.target.id()); });
    cy.on('dbltap', 'node', event => {
      const node = event.target as cytoscape.NodeSingular;
      callbacks.current.onExpand(node.data('isGroup') ? node.data('representative') as string : node.id());
    });
    runLayout();
    const observer = new ResizeObserver(() => { if (!cy.destroyed()) cy.resize(); }); observer.observe(container.current);
    return () => { observer.disconnect(); cy.destroy(); if (engine.current === cy) engine.current = null; };
  }, [nodes, edges, grouped]);
  useEffect(() => {
    const cy = engine.current; if (!cy) return;
    cy.style().selector('node:childless').style('label', nodeLabels ? 'data(label)' : '').selector('edge').style('label', edgeLabels ? 'data(label)' : '').update();
  }, [nodeLabels, edgeLabels]);

  return <div className="lab-renderer">
    <div className="lab-renderer-toolbar" aria-label="Cytoscape controls">
      <label>Layout<select aria-label="Cytoscape layout" value={layout} onChange={event => { const next = event.target.value as LayoutName; setLayout(next); runLayout(next); }}>{layouts.map(([id, label]) => <option value={id} key={id}>{label}</option>)}</select></label>
      <button onClick={() => runLayout()}>Rearrange</button><button onClick={() => engine.current?.fit(undefined, 35)}>Fit</button>
      <button aria-label="Cytoscape zoom in" onClick={() => { const cy = engine.current; if (cy) cy.zoom({ level: cy.zoom() * 1.3, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } }); }}>+</button>
      <button aria-label="Cytoscape zoom out" onClick={() => { const cy = engine.current; if (cy) cy.zoom({ level: cy.zoom() / 1.3, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } }); }}>−</button>
      <label><input type="checkbox" checked={grouped} onChange={event => setGrouped(event.target.checked)} />Source groups</label>
      <label><input type="checkbox" checked={nodeLabels} onChange={event => setNodeLabels(event.target.checked)} />Record labels</label>
      <label><input type="checkbox" checked={edgeLabels} onChange={event => setEdgeLabels(event.target.checked)} />Link labels</label>
      <button onClick={neighbors} disabled={!selected}>Neighbors</button><button onClick={resetHighlight}>Clear highlights</button>
      <button onClick={() => { const cy = engine.current; if (cy) downloadImage(cy.png({ bg: '#fff', full: true, maxWidth: 2400, maxHeight: 1800 })); }} disabled={!nodes.length}>Save PNG</button>
    </div>
    <div className="lab-renderer-path"><label>Path from<select aria-label="Cytoscape path start" value={from} onChange={event => setFrom(event.target.value)}><option value="">Choose a record</option>{nodes.map(node => <option key={node.id} value={node.id}>{node.label} · {node.source_name || node.kind}</option>)}</select></label><label>to<select aria-label="Cytoscape path end" value={to} onChange={event => setTo(event.target.value)}><option value="">Choose a record</option>{nodes.map(node => <option key={node.id} value={node.id}>{node.label} · {node.source_name || node.kind}</option>)}</select></label><button disabled={!from || !to} onClick={shortestPath}>Highlight shortest path</button></div>
    <div className="lab-renderer-canvas" ref={container} aria-label="Cytoscape knowledge graph" />
    <div role={error ? 'alert' : 'status'} className={`lab-renderer-message${error ? ' lab-renderer-error' : ''}`}>{error || message}</div>
  </div>;
}
