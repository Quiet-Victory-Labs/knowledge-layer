import type { CurationRecord, KnowledgeSnapshot } from './platformApi';

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function RetainedPassage({ item }: { item: Record<string, unknown> }) {
  const locator = object(item.source_locator);
  const page = locator?.page_start;
  const paragraph = locator?.paragraph_start;
  const line = locator?.line_start ?? locator?.source_node_line_start ?? item.line;
  const place = typeof page === 'number' ? `page ${page}`
    : typeof paragraph === 'number' ? `paragraph ${paragraph}`
      : typeof line === 'number' ? `line ${line}` : '';

  return <div>
    <p><strong>Retained source passage</strong>{place ? ` · ${place}` : ''}</p>
    <blockquote>{text(item.excerpt)}</blockquote>
  </div>;
}

export function relationshipTitle(record: CurationRecord, snapshot: KnowledgeSnapshot | null): string | null {
  if (record.proposal_type !== 'relationship_mapping' || !record.subject_id || !record.object_id) return null;
  const names = new Map((snapshot?.concepts ?? []).filter((concept) => concept.id && concept.name)
    .map((concept) => [concept.id, concept.name]));
  const source = names.get(record.subject_id);
  const target = names.get(record.object_id);
  return source && target ? `Possible: ${source} → ${target}` : null;
}

export function EvidenceHighlights({ evidence }: { evidence: CurationRecord }) {
  const details = object(evidence.details);
  const profile = object(details?.profile);
  const signals = object(profile?.document_signals);
  const descriptions = Array.isArray(signals?.candidate_descriptions)
    ? signals.candidate_descriptions.map(object).filter((item): item is Record<string, unknown> => item !== null)
    : [];
  const passages = descriptions.filter((item) => text(item.excerpt));
  const counts = object(profile?.null_counts) ?? object(profile?.missing_counts);
  const fields = counts ? Object.keys(counts) : [];
  const rowCount = profile?.row_count;
  if (!passages.length && !fields.length && typeof rowCount !== 'number') return null;

  return <div className="review-evidence-highlights">
    {fields.length > 0 && <p><strong>Observed fields:</strong> {fields.slice(0, 12).join(', ')}{fields.length > 12 ? `, and ${fields.length - 12} more` : ''}</p>}
    {typeof rowCount === 'number' && <p><strong>Rows:</strong> {rowCount} {profile?.row_count_is_complete === true ? 'observed in this complete file' : 'reported or profiled'}</p>}
    {passages.slice(0, 3).map((item, index) => <RetainedPassage item={item} key={index} />)}
    {passages.length > 3 && <details>
      <summary>Show {passages.length - 3} more retained {passages.length === 4 ? 'passage' : 'passages'}</summary>
      {passages.slice(3).map((item, index) => <RetainedPassage item={item} key={index + 3} />)}
    </details>}
  </div>;
}
