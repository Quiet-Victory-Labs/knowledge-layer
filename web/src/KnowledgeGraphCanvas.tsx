import { useEffect, useMemo, useRef, useState } from 'react';
import cytoscape, { type Core, type ElementDefinition } from 'cytoscape';
import fcose from 'cytoscape-fcose';
import type { GraphEdge, GraphNode } from './platformApi';
import './knowledgeGraphCanvas.css';

cytoscape.use(fcose);

export type KnowledgeGraphCanvasProps = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  onSelect: (id: string) => void;
  onExpand: (id: string) => void;
  selectedId?: string;
  focusId?: string;
  variant?: 'map' | 'lab';
  collapseOverview?: boolean;
};

type LayoutName = 'fcose' | 'breadthfirst' | 'concentric' | 'circle' | 'grid';
type LabelMode = 'auto' | 'all' | 'none';
type Group = { id: string; label: string; representative: string };
type SourceCard = { connectionId: string; label: string; representative: string; items: number; pending: number };
const layouts: Array<[LayoutName, string]> = [
  ['fcose', 'Source map'], ['breadthfirst', 'Flow'], ['concentric', 'Connections'],
  ['circle', 'Circle'], ['grid', 'Grid'],
];
const colors: Record<GraphNode['kind'], string> = {
  source: '#294d44', asset: '#4773a5', field: '#729bc8', document: '#a97550',
  concept: '#638566', shared_term: '#b99d4c', metric: '#8058a0',
  ontology: '#745b91', assertion: '#c58650', evidence: '#87938f',
};
const fills: Record<GraphNode['kind'], string> = {
  source: '#294d44', asset: '#edf3fa', field: '#f1f6fb', document: '#faf1e8',
  concept: '#edf4e9', shared_term: '#fcf4d8', metric: '#f3edf8',
  ontology: '#f1edf8', assertion: '#fff3e5', evidence: '#eef1ec',
};
const priority: Record<GraphNode['kind'], number> = {
  source: 0, shared_term: 1, concept: 2, metric: 3, ontology: 4,
  document: 5, asset: 6, field: 7, evidence: 8, assertion: 9,
};
const pendingStates = new Set([
  'candidate', 'proposed', 'pending', 'pending_review', 'unreviewed',
  'possible_shared_term', 'inferred', 'stale',
]);

export function automaticLabelIds(nodes: GraphNode[], limit = 120): Set<string> {
  return new Set([...nodes].sort((a, b) => priority[a.kind] - priority[b.kind])
    .slice(0, limit).map(node => node.id));
}

// One path through the loaded view; enumerating equal-length paths can explode.
export function boundedConnectivityPath(
  nodes: Array<{ id: string }>, edges: Array<{ source: string; target: string }>,
  from: string, to: string,
): string[] {
  const neighbors = new Map(nodes.map(node => [node.id, [] as string[]]));
  if (!neighbors.has(from) || !neighbors.has(to)) return [];
  for (const edge of edges) {
    neighbors.get(edge.source)?.push(edge.target);
    neighbors.get(edge.target)?.push(edge.source);
  }
  const previous = new Map<string, string | null>([[from, null]]);
  const queue = [from];
  for (let index = 0; index < queue.length && !previous.has(to); index++) {
    for (const next of neighbors.get(queue[index]) || []) {
      if (neighbors.has(next) && !previous.has(next)) {
        previous.set(next, queue[index]);
        queue.push(next);
      }
    }
  }
  if (!previous.has(to)) return [];
  const path: string[] = [];
  let cursor: string | null = to;
  while (cursor !== null) {
    path.push(cursor);
    cursor = previous.get(cursor) ?? null;
  }
  return path.reverse();
}

function styles(): cytoscape.StylesheetStyle[] {
  return [
    { selector: 'node[kind]', style: {
      shape: 'roundrectangle', width: 'data(width)', height: 'data(height)',
      'background-color': 'data(fill)', 'border-color': 'data(color)',
      'border-width': 1.5, color: '#354a3a', label: 'data(displayLabel)',
      'font-size': 10, 'font-family': 'system-ui', 'text-valign': 'center',
      'text-halign': 'center', 'text-wrap': 'ellipsis', 'text-max-width': '106px',
      'overlay-opacity': 0,
    } },
    { selector: 'node[kind = "source"]', style: {
      color: '#fff', 'font-weight': 600, 'text-max-width': '125px',
    } },
    { selector: 'node[kind = "concept"], node[kind = "shared_term"], node[kind = "ontology"]',
      style: { shape: 'ellipse' } },
    { selector: 'node[kind = "metric"]', style: { shape: 'diamond' } },
    { selector: 'node[?pending]', style: {
      'border-style': 'dashed', 'border-color': '#b59957',
    } },
    { selector: 'node[?approved]', style: { 'border-color': '#42754d' } },
    { selector: 'node[?isGroup]', style: {
      shape: 'roundrectangle', width: 166, height: 64, 'min-width': '166px',
      'min-height': '64px', padding: '20px',
      'background-color': '#e8f0e8', 'background-opacity': 0.55,
      'border-color': '#b9cdbb', 'border-width': 1.5, label: 'data(label)',
      color: '#365747', 'font-size': 12, 'font-weight': 600,
      'font-family': 'system-ui', 'text-valign': 'top', 'text-halign': 'center',
      'text-margin-y': -7, 'text-wrap': 'ellipsis', 'text-max-width': '155px',
    } },
    { selector: 'node[?isGroup].kg-collapsed', style: {
      'background-opacity': 1, 'border-color': '#8bad94',
      'text-valign': 'center', 'text-margin-y': 0,
    } },
    { selector: 'node[?isGroup][?pending]', style: {
      'border-style': 'dashed', 'border-color': '#b59957',
    } },
    { selector: 'edge', style: {
      width: 1.25, 'line-color': '#a4b4a1',
      'target-arrow-color': '#a4b4a1', 'target-arrow-shape': 'triangle',
      'curve-style': 'bezier', label: 'data(displayLabel)', 'font-size': 9,
      'font-family': 'system-ui', color: '#657769', 'text-rotation': 'autorotate',
      'text-background-color': '#fff', 'text-background-opacity': 0.9,
      'text-background-padding': '2px',
    } },
    { selector: 'edge[?pending]', style: {
      'line-style': 'dashed', 'line-color': '#b59957',
      'target-arrow-color': '#b59957',
    } },
    { selector: '.kg-muted', style: { opacity: 0.14 } },
    { selector: 'node.kg-highlight', style: {
      'border-width': 3, 'border-color': '#42754d',
    } },
    { selector: 'edge.kg-highlight', style: {
      width: 3, 'line-color': '#42754d',
      'target-arrow-color': '#42754d', 'z-index': 5,
    } },
    { selector: 'node.kg-selected', style: {
      'border-width': 3, 'border-color': '#294d44',
      'overlay-color': '#8cc09a', 'overlay-opacity': 0.16,
      'overlay-padding': 5,
    } },
    { selector: 'edge.kg-selected', style: {
      width: 3, 'line-color': '#294d44', 'target-arrow-color': '#294d44',
    } },
  ];
}

function layoutOptions(name: LayoutName, overview: boolean): cytoscape.LayoutOptions {
  if (overview && name === 'fcose') {
    return { name: 'grid', animate: false, fit: true, padding: 24,
      condense: false, avoidOverlap: true } as cytoscape.LayoutOptions;
  }
  if (name === 'fcose') {
    return { name, quality: 'default', randomize: true, animate: false,
      numIter: 650, nodeRepulsion: () => 6500, idealEdgeLength: () => 115,
      packComponents: false, tile: true, fit: true, padding: 32 } as cytoscape.LayoutOptions;
  }
  return { name, animate: false, fit: true, padding: 32,
    avoidOverlap: true, spacingFactor: 1.3,
    directed: name === 'breadthfirst' } as cytoscape.LayoutOptions;
}

export default function KnowledgeGraphCanvas({
  nodes, edges, onSelect, onExpand, selectedId, focusId,
  variant = 'map', collapseOverview = false,
}: KnowledgeGraphCanvasProps) {
  const container = useRef<HTMLDivElement>(null);
  const engine = useRef<Core | null>(null);
  const callbacks = useRef({ onSelect, onExpand });
  callbacks.current = { onSelect, onExpand };
  const groups = useRef(new Map<string, Group>());
  const recordGroup = useRef(new Map<string, string>());
  const collapsed = useRef(new Set<string>());
  const revealedStandalone = useRef(new Set<string>());
  const nodeDefinitions = useRef(new Map<string, ElementDefinition>());
  const edgeDefinitions = useRef(new Map<string, ElementDefinition>());
  const selectionRevealed = useRef<{ cy: Core | null; id: string }>({ cy: null, id: '' });
  const focusRevealed = useRef<{ cy: Core | null; id: string }>({ cy: null, id: '' });
  const [layout, setLayout] = useState<LayoutName>('fcose');
  const layoutRef = useRef(layout); layoutRef.current = layout;
  const [grouped, setGrouped] = useState(true);
  const [overviewSource, setOverviewSource] = useState<string | null>(null);
  const [labelMode, setLabelMode] = useState<LabelMode>(variant === 'map' ? 'auto' : 'all');
  const [edgeLabels, setEdgeLabels] = useState(false);
  const [localSelection, setLocalSelection] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [ready, setReady] = useState(false);
  const [generation, setGeneration] = useState(0);
  const [message, setMessage] = useState('Select a record to inspect its connections.');
  const [error, setError] = useState('');
  const selected = selectedId ?? localSelection;
  const labeledIds = useMemo(() => automaticLabelIds(nodes), [nodes]);
  const recordIds = useMemo(() => new Set(nodes.map(node => node.id)), [nodes]);
  const viewEdges = useMemo(() =>
    edges.filter(edge => recordIds.has(edge.subject_id) && recordIds.has(edge.object_id)),
  [edges, recordIds]);
  const sourceCards = useMemo(() => {
    const cards = new Map<string, SourceCard>();
    for (const node of nodes) {
      if (!node.connection_id) continue;
      let card = cards.get(node.connection_id);
      if (!card) {
        card = { connectionId: node.connection_id, label: node.source_name || node.label,
          representative: node.id, items: 0, pending: 0 };
        cards.set(node.connection_id, card);
      }
      if (node.kind === 'source') {
        card.label = node.source_name || node.label;
        card.representative = node.id;
      } else card.items++;
      if (pendingStates.has(node.status) || pendingStates.has(node.trust_state)) card.pending++;
    }
    return [...cards.values()].sort((a, b) => a.label.localeCompare(b.label));
  }, [nodes]);
  const showCards = collapseOverview && sourceCards.length > 0 && !overviewSource;

  useEffect(() => {
    if (!collapseOverview) setOverviewSource(null);
  }, [collapseOverview]);
  useEffect(() => {
    if (overviewSource && !sourceCards.some(card => card.connectionId === overviewSource)) {
      setOverviewSource(null);
    }
  }, [overviewSource, sourceCards]);

  function perform(action: (cy: Core) => void) {
    const cy = engine.current;
    if (!cy || cy.destroyed()) return;
    try { action(cy); setError(''); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Graph operation failed'); }
  }
  function allCollapsed() {
    return groups.current.size > 0 && collapsed.current.size === groups.current.size;
  }
  function runLayout(name = layoutRef.current, announce = true) {
    perform(cy => {
      cy.stop();
      cy.layout(layoutOptions(name, allCollapsed())).run();
      if (announce) setMessage((layouts.find(([id]) => id === name)?.[1] || 'Graph') + ' layout ready.');
    });
  }
  function syncElements(cy: Core) {
    // Collapsed groups contain no child elements at all. This makes a large
    // source overview legible and keeps its fit independent of hidden records.
    cy.batch(() => {
      cy.elements().removeClass('kg-highlight kg-muted');
      for (const [id, group] of groups.current) {
        cy.getElementById(group.id).toggleClass('kg-collapsed', collapsed.current.has(id));
      }
      for (const [id] of edgeDefinitions.current) {
        const element = cy.getElementById(id);
        if (element.nonempty()) element.remove();
      }
      for (const [id] of nodeDefinitions.current) {
        const sourceId = recordGroup.current.get(id);
        const element = cy.getElementById(id);
        const hidden = sourceId ? collapsed.current.has(sourceId) :
          Boolean(overviewSource && grouped && !revealedStandalone.current.has(id));
        if (hidden && element.nonempty()) element.remove();
      }
      for (const [id, definition] of nodeDefinitions.current) {
        const sourceId = recordGroup.current.get(id);
        const shown = sourceId ? !collapsed.current.has(sourceId) :
          !overviewSource || !grouped || revealedStandalone.current.has(id);
        if (shown && cy.getElementById(id).empty()) {
          cy.add(definition);
        }
      }
      for (const definition of edgeDefinitions.current.values()) {
        const data = definition.data as { source: string; target: string };
        if (cy.getElementById(data.source).nonempty() && cy.getElementById(data.target).nonempty()) {
          cy.add(definition);
        }
      }
    });
    setGeneration(value => value + 1);
  }
  function reveal(cy: Core, ids: Iterable<string>) {
    let changed = false;
    for (const id of ids) {
      const sourceId = recordGroup.current.get(id);
      if (sourceId && collapsed.current.delete(sourceId)) changed = true;
      if (!sourceId && overviewSource && grouped && nodeDefinitions.current.has(id) &&
          !revealedStandalone.current.has(id)) {
        revealedStandalone.current.add(id);
        changed = true;
      }
    }
    if (changed) syncElements(cy);
    return changed;
  }
  function changeGroups(value: boolean) {
    perform(cy => {
      if (overviewSource && grouped) {
        if (value) revealedStandalone.current.clear();
        else for (const id of nodeDefinitions.current.keys()) {
          if (!recordGroup.current.has(id)) revealedStandalone.current.add(id);
        }
      }
      for (const id of groups.current.keys()) {
        if (value) collapsed.current.add(id);
        else collapsed.current.delete(id);
      }
      syncElements(cy);
      runLayout(layoutRef.current, false);
      setMessage(value ? 'Source overview. Double-click a source to see its records.' : 'Source records shown.');
    });
  }
  function highlight(cy: Core, nodeIds: Set<string>, edgeIds: Set<string>, text: string) {
    const active = cy.elements().filter(element =>
      element.isNode() ? nodeIds.has(element.id()) : edgeIds.has(element.id()));
    const shown = active.union(active.nodes().ancestors());
    cy.batch(() => {
      cy.elements().removeClass('kg-highlight kg-muted');
      cy.elements().difference(shown).addClass('kg-muted');
      shown.addClass('kg-highlight');
    });
    setMessage(text);
  }
  function clearHighlights() {
    perform(cy => {
      cy.elements().removeClass('kg-highlight kg-muted');
      setMessage('Highlights cleared.');
    });
  }
  function neighbors() {
    if (!recordIds.has(selected)) return;
    perform(cy => {
      const adjacent = new Set<string>([selected]);
      const linked = new Set<string>();
      for (const edge of viewEdges) {
        if (edge.subject_id === selected || edge.object_id === selected) {
          adjacent.add(edge.subject_id); adjacent.add(edge.object_id);
          linked.add(edge.id);
        }
      }
      if (reveal(cy, adjacent)) runLayout(layoutRef.current, false);
      highlight(cy, adjacent, linked,
        'Highlighted ' + (adjacent.size - 1) + ' adjacent records.');
    });
  }
  function path() {
    if (!from || !to) return;
    perform(cy => {
      const ids = boundedConnectivityPath(nodes,
        viewEdges.map(edge => ({ source: edge.subject_id, target: edge.object_id })), from, to);
      if (!ids.length) {
        cy.elements().removeClass('kg-highlight kg-muted');
        setMessage('No path connects those records in this loaded view.');
        return;
      }
      const linked = new Set<string>();
      for (let index = 1; index < ids.length; index++) {
        const edge = viewEdges.find(item =>
          (item.subject_id === ids[index - 1] && item.object_id === ids[index]) ||
          (item.object_id === ids[index - 1] && item.subject_id === ids[index]));
        if (edge) linked.add(edge.id);
      }
      if (reveal(cy, ids)) runLayout(layoutRef.current, false);
      highlight(cy, new Set(ids), linked,
        'Undirected path · ' + (ids.length - 1) +
        ' links. Connectivity does not establish an approved join or equivalent meaning.');
    });
  }

  useEffect(() => {
    const element = container.current;
    if (!element || showCards) return;
    let active = true;
    const records = [...new Map(nodes.map(node => [node.id, node])).values()];
    const ids = new Set(records.map(node => node.id));
    const links = edges.filter(edge => ids.has(edge.subject_id) && ids.has(edge.object_id));
    const reserved = new Set([...ids, ...links.map(edge => edge.id)]);
    const byConnection = new Map<string, Group>();
    const groupOf = new Map<string, string>();
    if (grouped) {
      for (const node of records) {
        if (!node.connection_id) continue;
        const prior = byConnection.get(node.connection_id);
        if (!prior || node.kind === 'source') byConnection.set(node.connection_id,
          { id: '', label: node.source_name || node.label, representative: node.id });
      }
      for (const [connectionId, group] of byConnection) {
        let id = 'source-group:' + connectionId;
        while (reserved.has(id)) id = ':' + id;
        reserved.add(id);
        group.id = id;
      }
      for (const node of records) {
        if (node.connection_id && byConnection.has(node.connection_id)) {
          groupOf.set(node.id, node.connection_id);
        }
      }
    }
    groups.current = byConnection;
    recordGroup.current = groupOf;
    collapsed.current = new Set(collapseOverview
      ? [...byConnection.keys()].filter(id => id !== overviewSource) : []);
    revealedStandalone.current = new Set(overviewSource && grouped ? [] :
      records.filter(node => !groupOf.has(node.id)).map(node => node.id));
    const pendingGroups = new Set(records.filter(node =>
      pendingStates.has(node.status) || pendingStates.has(node.trust_state))
      .map(node => groupOf.get(node.id)).filter((id): id is string => Boolean(id)));
    const columns = Math.max(1, Math.ceil(Math.sqrt(records.length)));
    const nodeDefs = new Map<string, ElementDefinition>(records.map((node, index) => [node.id, {
      // New records need distinct coordinates when syncElements adds them,
      // before the compound source layout computes their final positions.
      position: { x: (index % columns) * 220, y: Math.floor(index / columns) * 140 },
      data: {
        id: node.id, kind: node.kind, label: node.label, displayLabel: node.label,
        color: colors[node.kind] || '#638566', fill: fills[node.kind] || '#edf4e9',
        width: node.kind === 'metric' ? 138 : node.kind === 'source' ? 136 :
          node.kind === 'field' ? 108 : 116,
        height: node.kind === 'metric' ? 70 : node.kind === 'source' ? 52 :
          node.kind === 'field' ? 34 : 44,
        pending: pendingStates.has(node.status) || pendingStates.has(node.trust_state),
        approved: ['accepted', 'approved', 'published'].includes(node.status),
        parent: groupOf.has(node.id) ? byConnection.get(groupOf.get(node.id)!)?.id : undefined,
      },
    }]));
    const edgeDefs = new Map<string, ElementDefinition>(links.map(edge => [edge.id, {
      data: {
        id: edge.id, source: edge.subject_id, target: edge.object_id,
        label: edge.predicate.replaceAll('_', ' '), displayLabel: '',
        pending: pendingStates.has(edge.status) || pendingStates.has(edge.trust_state),
      },
    }]));
    nodeDefinitions.current = nodeDefs;
    edgeDefinitions.current = edgeDefs;
    const elements: ElementDefinition[] = [...byConnection].map(([connectionId, group]) => ({
      data: { id: group.id, label: group.label, representative: group.representative,
        isGroup: true, pending: pendingGroups.has(connectionId) },
    }));
    let cy: Core;
    try {
      // Lay out source containers before their individually positioned records.
      cy = cytoscape({ container: element, elements,
        layout: { name: 'grid', animate: false, avoidOverlap: true, fit: false },
        minZoom: 0.03, maxZoom: 4, style: styles() });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Graph renderer failed');
      return;
    }
    engine.current = cy;
    setReady(false);
    setLocalSelection(previous => reserved.has(previous) ? previous : '');
    setFrom(previous => ids.has(previous) ? previous : '');
    setTo(previous => ids.has(previous) ? previous : '');
    cy.on('tap', 'node', event => {
      if (!active) return;
      const node = event.target as cytoscape.NodeSingular;
      const id = node.data('isGroup') ? node.data('representative') as string : node.id();
      if (!id) return;
      setLocalSelection(id);
      setFrom(id);
      callbacks.current.onSelect(id);
    });
    cy.on('tap', 'edge', event => {
      if (!active) return;
      const id = (event.target as cytoscape.EdgeSingular).id();
      setLocalSelection(id);
      callbacks.current.onSelect(id);
    });
    cy.on('dbltap', 'node', event => {
      if (!active) return;
      const node = event.target as cytoscape.NodeSingular;
      if (!node.data('isGroup')) { callbacks.current.onExpand(node.id()); return; }
      const sourceId = [...byConnection].find(([, group]) => group.id === node.id())?.[0];
      if (!sourceId) return;
      if (collapsed.current.has(sourceId)) collapsed.current.delete(sourceId);
      else collapsed.current.add(sourceId);
      syncElements(cy);
      runLayout(layoutRef.current, false);
      setMessage(collapsed.current.has(sourceId)
        ? 'Source group collapsed.' : 'Source group open. Double-click a record for its neighborhood.');
    });
    try {
      syncElements(cy);
      cy.layout(layoutOptions(layoutRef.current, allCollapsed())).run();
      if (overviewSource) {
        const opened = byConnection.get(overviewSource);
        if (opened) cy.fit(cy.getElementById(opened.id), 36);
      }
      setReady(true);
      setError('');
      setMessage(overviewSource
        ? 'Showing this source. Select a record, then use Neighbors to reveal shared terms.'
        : 'Select a record to inspect its connections.');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Graph renderer failed');
    }
    const resize = () => {
      if (active && !cy.destroyed()) {
        cy.resize();
        if (overviewSource) {
          const opened = byConnection.get(overviewSource);
          if (opened) cy.fit(cy.getElementById(opened.id), 36);
        } else if (allCollapsed()) cy.fit(undefined, 24);
      }
    };
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(resize);
    observer?.observe(element);
    if (!observer) window.addEventListener('resize', resize);
    return () => {
      active = false;
      observer?.disconnect();
      if (!observer) window.removeEventListener('resize', resize);
      cy.stop(); cy.destroy();
      if (engine.current === cy) engine.current = null;
    };
  }, [nodes, edges, grouped, collapseOverview, overviewSource, showCards]);

  useEffect(() => {
    if (!ready) return;
    perform(cy => {
      const visibleRecordCount = cy.nodes('[kind]').length;
      if (selected && (selectionRevealed.current.cy !== cy || selectionRevealed.current.id !== selected)) {
        selectionRevealed.current = { cy, id: selected };
        const selectedEdge = edgeDefinitions.current.get(selected);
        const endpoints = selectedEdge?.data as { source: string; target: string } | undefined;
        const selectedNode = nodeDefinitions.current.get(selected);
        const isSource = (selectedNode?.data as { kind?: string } | undefined)?.kind === 'source';
        if ((endpoints && reveal(cy, [endpoints.source, endpoints.target])) ||
            (selectedNode && !isSource && reveal(cy, [selected]))) {
          runLayout(layoutRef.current, false);
        }
      }
      cy.batch(() => {
        cy.nodes('[kind]').forEach(node => {
          const id = node.id();
          const show = labelMode === 'all' ||
            (labelMode === 'auto' && (visibleRecordCount <= 120 || labeledIds.has(id))) ||
            id === selected || id === focusId;
          node.data('displayLabel', show ? node.data('label') as string : '');
        });
        cy.edges().forEach(edge => {
          edge.data('displayLabel', edgeLabels ? edge.data('label') as string : '');
        });
        cy.elements().removeClass('kg-selected');
        if (selected) {
          const item = cy.getElementById(selected);
          if (item.nonempty()) item.addClass('kg-selected');
          else {
            const sourceId = recordGroup.current.get(selected);
            if (sourceId) cy.getElementById(groups.current.get(sourceId)?.id || '')
              .addClass('kg-selected');
          }
        }
      });
    });
  }, [selected, focusId, labelMode, edgeLabels, labeledIds, ready, generation]);

  useEffect(() => {
    if (!ready || !focusId) return;
    perform(cy => {
      if (focusRevealed.current.cy === cy && focusRevealed.current.id === focusId) return;
      focusRevealed.current = { cy, id: focusId };
      const edge = edgeDefinitions.current.get(focusId);
      const data = edge?.data as { source: string; target: string } | undefined;
      const endpointIds = data ? [data.source, data.target] : [focusId];
      if (reveal(cy, endpointIds)) runLayout(layoutRef.current, false);
      const target = cy.getElementById(focusId);
      if (target.nonempty()) cy.center(target);
    });
  }, [focusId, ready, generation]);

  const prefix = variant === 'lab' ? 'Graph lab' : 'Knowledge graph';
  const layoutSelect = <label>Layout
    <select aria-label={prefix + ' layout'} value={layout} disabled={!ready}
      onChange={event => { const next = event.target.value as LayoutName;
        setLayout(next); runLayout(next); }}>
      {layouts.map(([id, title]) => <option value={id} key={id}>{title}</option>)}
    </select>
  </label>;
  const pathControls = <div className="kg-path">
    <label>Path from
      <select aria-label={prefix + ' path start'} value={from} onChange={event => setFrom(event.target.value)}>
        <option value="">Choose a record</option>
        {nodes.map(node => <option key={node.id} value={node.id}>
          {node.label} · {node.source_name || node.kind}</option>)}
      </select>
    </label>
    <label>to
      <select aria-label={prefix + ' path end'} value={to} onChange={event => setTo(event.target.value)}>
        <option value="">Choose a record</option>
        {nodes.map(node => <option key={node.id} value={node.id}>
          {node.label} · {node.source_name || node.kind}</option>)}
      </select>
    </label>
    <button type="button" disabled={!ready || !from || !to} onClick={path}>Show path</button>
  </div>;
  if (showCards) {
    const selectedSource = nodes.find(node => node.id === selected)?.connection_id;
    return <div className={'knowledge-graph-canvas knowledge-graph-canvas--' + variant}>
      <div className="kg-overview-heading">
        <p>Choose a source to explore its records and links.</p>
        <span>{sourceCards.length} sources</span>
      </div>
      <div className="kg-source-grid" role="group" aria-label="Connected sources">
        {sourceCards.map(card => <button type="button" className="kg-source-card"
          key={card.connectionId} aria-pressed={selectedSource === card.connectionId}
          onClick={() => {
            setGrouped(true);
            setOverviewSource(card.connectionId);
            setLocalSelection(card.representative);
            setFrom(card.representative);
            callbacks.current.onSelect(card.representative);
          }}>
          <strong>{card.label}</strong>
          <span>{card.items} {card.items === 1 ? 'item' : 'items'}</span>
          {card.pending > 0 && <small>{card.pending} need review</small>}
        </button>)}
      </div>
    </div>;
  }
  return <div className={'knowledge-graph-canvas knowledge-graph-canvas--' + variant}>
    <div className="kg-controls kg-controls--primary" aria-label={prefix + ' controls'}>
      {collapseOverview && overviewSource && <button type="button" className="kg-back"
        onClick={() => setOverviewSource(null)}>← All sources</button>}
      {variant === 'lab' && layoutSelect}
      <button type="button" disabled={!ready}
        onClick={() => perform(cy => cy.fit(undefined, 32))}>Fit</button>
      <button type="button" aria-label={prefix + ' zoom in'} disabled={!ready}
        onClick={() => perform(cy => cy.zoom({ level: cy.zoom() * 1.3,
          renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } }))}>+</button>
      <button type="button" aria-label={prefix + ' zoom out'} disabled={!ready}
        onClick={() => perform(cy => cy.zoom({ level: cy.zoom() / 1.3,
          renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } }))}>−</button>
      {variant === 'map' && <span className="kg-hint">Dashed = needs review · drag to move</span>}
    </div>
    <details className="kg-advanced">
      <summary>More graph tools</summary>
      <div className="kg-controls kg-controls--secondary">
        {variant === 'map' && layoutSelect}
        <label><input type="checkbox" checked={grouped}
          onChange={event => setGrouped(event.target.checked)} />Source groups</label>
        <button type="button" disabled={!ready || !grouped}
          onClick={() => changeGroups(true)}>Collapse groups</button>
        <button type="button" disabled={!ready || !grouped}
          onClick={() => changeGroups(false)}>Expand groups</button>
        <label>Labels
          <select aria-label={prefix + ' labels'} value={labelMode}
            onChange={event => setLabelMode(event.target.value as LabelMode)}>
            <option value="auto">Automatic</option>
            <option value="all">All</option>
            <option value="none">Off</option>
          </select>
        </label>
        <label><input type="checkbox" checked={edgeLabels}
          onChange={event => setEdgeLabels(event.target.checked)} />Link labels</label>
        <button type="button" disabled={!ready} onClick={() => runLayout()}>Rearrange</button>
        <button type="button" disabled={!ready || !recordIds.has(selected)}
          onClick={neighbors}>Neighbors</button>
        <button type="button" disabled={!ready} onClick={clearHighlights}>Clear highlight</button>
        <button type="button" disabled={!ready || !nodes.length}
          onClick={() => perform(cy => {
            const anchor = document.createElement('a');
            anchor.href = cy.png({ bg: '#fff', full: true, maxWidth: 2400, maxHeight: 1800 });
            anchor.download = 'knowledge-graph.png';
            document.body.append(anchor); anchor.click(); anchor.remove();
          })}>Save image</button>
      </div>
      {pathControls}
      <p>Dashed links need review. Paths show connectivity, not an approved join or shared meaning.</p>
    </details>
    <div className="kg-surface" ref={container}
      aria-label={variant === 'lab' ? 'Graph lab canvas' : 'Knowledge graph'} />
    <div role={error ? 'alert' : 'status'}
      className={'kg-message' + (error ? ' kg-message--error' : '')}>
      {error || (!ready ? 'Preparing knowledge graph…' : message)}
    </div>
  </div>;
}
