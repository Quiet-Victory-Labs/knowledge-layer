import assert from 'node:assert/strict';
import test from 'node:test';
import { projectKnowledgeView } from '../src/knowledgeGraphViews.ts';
import type { GraphNode, GraphEdge } from '../src/platformApi.ts';

const record = (id: string, kind: GraphNode['kind'], extra: Partial<GraphNode> = {}): GraphNode => ({ id, kind, label: id, description: null, status: 'observed', trust_state: 'observed_metadata', evidence_ids: [], ...extra });
const link = (id: string, subject_id: string, object_id: string, extra: Partial<GraphEdge> = {}): GraphEdge => ({ id, subject_id, object_id, predicate: 'supported_by', status: 'observed', trust_state: 'observed_metadata', evidence_ids: [], confidence: null, ...extra });
const nodes = [record('source-a', 'source'), record('source-b', 'source'),
  record('meaning-a', 'concept', { label: 'Snapshot ID', connection_id: 'source-a', description: 'Text snapshot key', definition_status: 'pending_review' }),
  record('meaning-b', 'concept', { label: 'Snapshot ID', connection_id: 'source-b', description: 'Integer snapshot identifier' }),
  record('field-a', 'field'), record('table-a', 'asset'), record('document-a', 'document'), record('evidence-a', 'evidence'),
  record('proposal-a', 'assertion', { status: 'pending_review', trust_state: 'candidate' }), record('unrelated-field', 'field')];
const edges = [link('binding-a', 'meaning-a', 'field-a', { predicate: 'describes_field', status: 'pending_review', trust_state: 'candidate' }),
  link('definition-a', 'proposal-a', 'meaning-a', { predicate: 'defines', status: 'pending_review' }),
  link('citation-a', 'meaning-a', 'evidence-a', { status: 'pending_review', evidence_ids: ['evidence-a'] }),
  link('field-structure', 'table-a', 'field-a', { predicate: 'has_field' }),
  link('source-structure', 'source-a', 'table-a', { predicate: 'contains' }),
  link('broken-link', 'meaning-a', 'not-authorized-or-not-loaded')];

test('lenses keep exact source-scoped meanings and never manufacture equivalence or taxonomy', () => {
  const view = projectKnowledgeView(nodes, edges, 'vocabulary');
  assert.deepEqual(view.nodes.map(n => n.id), ['meaning-a', 'meaning-b']);
  assert.equal(view.nodes[0], nodes[2]);
  assert.equal(view.nodes[1].description, 'Integer snapshot identifier');
  assert.deepEqual(view.edges, []);
  assert.equal(nodes[2].definition_status, 'pending_review');
});
test('ontology shows only recorded assertions and one-hop source bindings', () => {
  const view = projectKnowledgeView(nodes, edges, 'ontology');
  assert.ok(view.nodes.some(n => n.id === 'field-a'));
  assert.ok(view.nodes.some(n => n.id === 'evidence-a'));
  assert.ok(!view.nodes.some(n => n.id === 'unrelated-field'));
  assert.ok(!view.edges.some(e => e.id === 'broken-link'));
  for (const edge of view.edges) assert.equal(edge, edges.find(e => e.id === edge.id));
});
test('review focus includes pending definitions and pending claim endpoints without promoting observations', () => {
  const view = projectKnowledgeView(nodes, edges, 'all', 'pending_review');
  assert.ok(view.matchingIds.has('meaning-a'));
  assert.ok(view.matchingIds.has('proposal-a'));
  assert.equal(view.contextCount, 2);
  assert.equal(view.nodes.find(n => n.id === 'field-a')?.status, 'observed');
  assert.ok(view.edges.every(e => e.status === 'pending_review'));
  assert.ok(view.nodes.every(n => n.id !== 'meaning-b'));
});
test('source and evidence views preserve endpoint closure within loaded authorization scope', () => {
  const structure = projectKnowledgeView(nodes, edges, 'sources');
  assert.deepEqual(structure.edges.map(e => e.id), ['field-structure', 'source-structure']);
  const evidence = projectKnowledgeView(nodes, edges, 'evidence');
  assert.ok(evidence.nodes.some(n => n.id === 'meaning-a'));
  assert.ok(!evidence.nodes.some(n => n.id === 'unrelated-field'));
  for (const lens of ['all', 'ontology', 'vocabulary', 'sources', 'evidence'] as const) {
    const view = projectKnowledgeView(nodes, edges, lens);
    const ids = new Set(view.nodes.map(n => n.id));
    assert.ok(view.edges.every(e => ids.has(e.subject_id) && ids.has(e.object_id)));
  }
});
