import type { GraphEdge, GraphNode } from './platformApi';

export type KnowledgeLens = 'all' | 'ontology' | 'vocabulary' | 'sources' | 'evidence';
export const knowledgeLenses: Array<{ id: KnowledgeLens; label: string; description: string }> = [
  { id: 'all', label: 'All knowledge', description: 'Meaning, source structure and evidence in one connected view.' },
  { id: 'ontology', label: 'Ontology', description: 'Concepts, metrics and semantic assertions, with their source bindings. Only recorded relationships appear.' },
  { id: 'vocabulary', label: 'Vocabulary', description: 'Source-scoped terms and definitions. Possible shared meanings remain candidates for review.' },
  { id: 'sources', label: 'Source structure', description: 'Connected sources, datasets, fields and documents, using their recorded structural relationships.' },
  { id: 'evidence', label: 'Evidence', description: 'Documents, supporting records and assertions, together with the knowledge they support.' },
];
const terms = new Set<GraphNode['kind']>(['concept', 'metric', 'ontology', 'shared_term']);
const seeds: Record<KnowledgeLens, Set<GraphNode['kind']>> = {
  all: new Set(), ontology: new Set([...terms, 'assertion']), vocabulary: terms,
  sources: new Set(['source', 'asset', 'field', 'document', 'ontology']),
  evidence: new Set(['evidence', 'document', 'assertion']),
};

// Lenses project the supplied, authorized graph; they never synthesize identity,
// taxonomy or relationships. Ontology/evidence include one-hop grounding context.
export function projectKnowledgeView(nodes: GraphNode[], edges: GraphEdge[], lens: KnowledgeLens, reviewState = '') {
  const available = new Set(nodes.map(node => node.id));
  const validEdges = edges.filter(edge => available.has(edge.subject_id) && available.has(edge.object_id));
  const primary = new Set(nodes.filter(node => lens === 'all' || seeds[lens].has(node.kind)).map(node => node.id));
  const scoped = new Set(primary);
  if (lens === 'ontology' || lens === 'evidence') {
    for (const edge of validEdges) if (primary.has(edge.subject_id) || primary.has(edge.object_id)) {
      scoped.add(edge.subject_id); scoped.add(edge.object_id);
    }
  }
  const lensEdges = validEdges.filter(edge => scoped.has(edge.subject_id) && scoped.has(edge.object_id));
  const matches = new Set(nodes.filter(node => scoped.has(node.id) && (!reviewState || node.status === reviewState || node.definition_status === reviewState)).map(node => node.id));
  const matchingEdges = reviewState ? lensEdges.filter(edge => edge.status === reviewState) : lensEdges;
  const visible = new Set(matches);
  for (const edge of matchingEdges) { visible.add(edge.subject_id); visible.add(edge.object_id); }
  return { nodes: nodes.filter(node => visible.has(node.id)), edges: matchingEdges,
    primaryIds: primary, matchingIds: matches, contextCount: [...visible].filter(id => !matches.has(id)).length };
}
