import type { GraphEdge, GraphNode } from '../platformApi';

export const graphTypeIri = (kind: string) => `urn:knowledge-layer:kind:${kind}`;
export const graphPropertyIri = (name: string) => `urn:knowledge-layer:property:${name}`;
// These are display types, not new source predicates. Preserve the actual predicate
// as a property and separate pending/effective assertions in native diagram links.
export const graphLinkIri = (edge: GraphEdge) => `urn:knowledge-layer:display-relation:${encodeURIComponent(edge.predicate)}:${encodeURIComponent(edge.status)}:${encodeURIComponent(edge.trust_state)}`;
export const readableProperty = (iri: string) => iri.startsWith('urn:knowledge-layer:property:')
  ? iri.slice('urn:knowledge-layer:property:'.length).replaceAll('_', ' ')
  : iri.split(/[#/]/).pop() || iri;

export class BoundedGraphIndex {
  readonly nodes: Map<string, GraphNode>;
  readonly edges: GraphEdge[];

  constructor(nodes: GraphNode[], edges: GraphEdge[]) {
    this.nodes = new Map(nodes.map(node => [node.id, node]));
    this.edges = edges.filter(edge => this.nodes.has(edge.subject_id) && this.nodes.has(edge.object_id));
  }

  classes() {
    const counts = new Map<string, number>();
    for (const node of this.nodes.values()) counts.set(node.kind, (counts.get(node.kind) || 0) + 1);
    return [...counts].map(([kind, count]) => ({ id: graphTypeIri(kind), label: kind.replaceAll('_', ' '), count }));
  }

  linkTypes() {
    const types = new Map<string, { id: string; label: string; count: number }>();
    for (const edge of this.edges) {
      const id = graphLinkIri(edge);
      const current = types.get(id);
      if (current) current.count++;
      else types.set(id, { id, label: `${edge.predicate} · ${edge.status}`, count: 1 });
    }
    return [...types.values()];
  }

  stats(id: string) {
    const types = new Map<string, { id: string; incoming: Set<string>; outgoing: Set<string> }>();
    for (const edge of this.edges) {
      if (edge.subject_id !== id && edge.object_id !== id) continue;
      const type = graphLinkIri(edge);
      const stat = types.get(type) || { id: type, incoming: new Set<string>(), outgoing: new Set<string>() };
      if (edge.subject_id === id) stat.outgoing.add(edge.object_id);
      if (edge.object_id === id) stat.incoming.add(edge.subject_id);
      types.set(type, stat);
    }
    return [...types.values()].map(stat => ({ id: stat.id, inCount: stat.incoming.size, outCount: stat.outgoing.size }));
  }

  lookup(params: { elementTypeId?: string; text?: string; refElementId?: string; refElementLinkId?: string; linkDirection?: 'in' | 'out'; limit?: number | null; offset?: number }) {
    const query = (params.text || '').trim().toLocaleLowerCase();
    const matches = [...this.nodes.values()].flatMap(node => {
      if (params.elementTypeId && graphTypeIri(node.kind) !== params.elementTypeId) return [];
      if (query && ![node.label, node.description, node.source_name, node.id, node.kind, node.status].filter(Boolean).join(' ').toLocaleLowerCase().includes(query)) return [];
      const connected = params.refElementId ? this.edges.filter(edge =>
        (!params.refElementLinkId || graphLinkIri(edge) === params.refElementLinkId)
        && ((!params.linkDirection || params.linkDirection === 'out') && edge.subject_id === params.refElementId && edge.object_id === node.id
          || (!params.linkDirection || params.linkDirection === 'in') && edge.object_id === params.refElementId && edge.subject_id === node.id)) : [];
      if (params.refElementId && !connected.length) return [];
      return [{ node, inLinks: new Set(connected.filter(edge => edge.subject_id === node.id).map(graphLinkIri)), outLinks: new Set(connected.filter(edge => edge.object_id === node.id).map(graphLinkIri)) }];
    }).sort((a, b) => a.node.label.localeCompare(b.node.label) || a.node.id.localeCompare(b.node.id));
    const offset = params.offset || 0;
    return matches.slice(offset, params.limit == null ? undefined : offset + params.limit);
  }

  nodeProperties(node: GraphNode): Record<string, string[]> {
    const properties: Record<string, string[]> = {};
    for (const [key, value] of Object.entries(node)) {
      if (key === 'label' || value == null) continue;
      properties[graphPropertyIri(key)] = Array.isArray(value)
        ? value.map(item => typeof item === 'object' ? JSON.stringify(item) : String(item))
        : [typeof value === 'object' ? JSON.stringify(value) : String(value)];
    }
    return properties;
  }

  linkProperties(edges: GraphEdge[]): Record<string, string[]> {
    return {
      [graphPropertyIri('record_id')]: edges.map(edge => edge.id),
      [graphPropertyIri('predicate')]: [...new Set(edges.map(edge => edge.predicate))],
      [graphPropertyIri('status')]: [...new Set(edges.map(edge => edge.status))],
      [graphPropertyIri('trust_state')]: [...new Set(edges.map(edge => edge.trust_state))],
      [graphPropertyIri('evidence_ids')]: [...new Set(edges.flatMap(edge => edge.evidence_ids))],
      [graphPropertyIri('confidence')]: [...new Set(edges.flatMap(edge => edge.confidence == null ? [] : [String(edge.confidence)]))],
    };
  }

  groupedEdges() {
    const groups = new Map<string, GraphEdge[]>();
    for (const edge of this.edges) {
      const key = JSON.stringify([edge.subject_id, graphLinkIri(edge), edge.object_id]);
      const group = groups.get(key) || [];
      group.push(edge);
      groups.set(key, group);
    }
    return [...groups.values()];
  }
}

export function downloadDiagram(name: string, diagram: unknown) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(diagram, null, 2)], { type: 'application/json' }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  document.body.append(anchor); anchor.click(); anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// A layout file supplies presentation only. It cannot introduce source records or
// relationships that are absent from the currently authorized bounded graph.
export function validateDiagram(value: unknown, index: BoundedGraphIndex): asserts value is { '@type': 'Diagram'; layoutData: { elements: unknown[]; links: unknown[] } } {
  if (!value || typeof value !== 'object' || (value as Record<string, unknown>)['@type'] !== 'Diagram') throw new Error('Choose a native diagram JSON file.');
  const layout = (value as { layoutData?: { elements?: unknown; links?: unknown } }).layoutData;
  if (!layout || !Array.isArray(layout.elements) || !Array.isArray(layout.links)) throw new Error('The diagram has no valid layout.');
  const check = (item: unknown): void => {
    if (!item || typeof item !== 'object') return;
    for (const [key, field] of Object.entries(item)) {
      if (['iri', 'sourceIri', 'targetIri'].includes(key) && typeof field === 'string' && !index.nodes.has(field)) throw new Error('This diagram references records outside the current graph. Load those sources first.');
      if (key === 'property' && typeof field === 'string' && !index.linkTypes().some(type => type.id === field)) throw new Error('This diagram references relationship types outside the current graph.');
      if (Array.isArray(field)) field.forEach(check);
      else if (field && typeof field === 'object') check(field);
    }
  };
  check(value);
}

export function pruneDiagram<T extends { layoutData: { elements: readonly unknown[]; links: readonly unknown[] } }>(diagram: T, index: BoundedGraphIndex): T {
  const types = new Set(index.linkTypes().map(type => type.id));
  const containsOnlyCurrentIds = (item: unknown): boolean => {
    if (!item || typeof item !== 'object') return true;
    return Object.entries(item).every(([key, value]) =>
      ['iri', 'sourceIri', 'targetIri'].includes(key) && typeof value === 'string' ? index.nodes.has(value)
        : Array.isArray(value) ? value.every(containsOnlyCurrentIds)
          : value && typeof value === 'object' ? containsOnlyCurrentIds(value) : true);
  };
  const elements = diagram.layoutData.elements.filter(containsOnlyCurrentIds);
  const ids = new Set(elements.map(item => (item as Record<string, unknown>)['@id']));
  const links = diagram.layoutData.links.filter(item => {
    const link = item as { property?: string; source?: { '@id'?: string }; target?: { '@id'?: string } };
    return ids.has(link.source?.['@id']) && ids.has(link.target?.['@id']) && (!link.property || types.has(link.property)) && containsOnlyCurrentIds(link);
  });
  const options = (diagram as T & { linkTypeOptions?: Array<{ property: string }> }).linkTypeOptions;
  return { ...diagram, layoutData: { ...diagram.layoutData, elements, links }, linkTypeOptions: options?.filter(option => types.has(option.property)) } as T;
}
