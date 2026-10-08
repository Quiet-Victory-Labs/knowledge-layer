import type { GraphNode, GraphEdge } from '../platformApi';
export type GraphLabRendererProps = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  onSelect: (id: string) => void;
  onExpand: (id: string) => void;
};
export const graphKindColors: Record<string, string> = { source: '#294d44', asset: '#4773a5', field: '#729bc8', document: '#a97550', concept: '#638566', shared_term: '#b99d4c', metric: '#8058a0', ontology: '#745b91', assertion: '#c58650', evidence: '#87938f' };
