import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { Button, Checkbox, Dialog, Flex, Text, TextArea } from './ui';
import {
  acceptReviewBatch,
  previewReviewBatch,
  type CurationRecord,
  type KnowledgeConnection,
  type KnowledgeSnapshot,
  type ReviewBatchPreview,
  type ReviewBatchResult,
} from './platformApi';
import { EvidenceHighlights, relationshipTitle } from './reviewEvidence';
import './reviewBatch.css';

type SelectionControls = {
  selectedIds: Set<string>;
  selectionDisabled: boolean;
  toggleSelection: (id: string) => void;
};

type ReviewBatchProps = {
  connection: KnowledgeConnection;
  snapshot: KnowledgeSnapshot | null;
  proposals: CurationRecord[];
  canSteward: boolean;
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
  onAccepted: (connectionId: string, result: ReviewBatchResult) => Promise<void>;
  children: (controls: SelectionControls) => ReactNode;
};

const REVIEW_ACKNOWLEDGEMENT = 'Reviewed the selected claims and their cited evidence.';

function readable(value: string): string {
  return value.replaceAll('_', ' ');
}

function titleFor(record: CurationRecord, snapshot: KnowledgeSnapshot | null): string {
  const relationship = relationshipTitle(record, snapshot);
  if (relationship) return relationship;
  const candidate = [...(snapshot?.concepts ?? []), ...(snapshot?.metrics ?? []), ...(snapshot?.relationships ?? [])]
    .find((item) => item.id === (record.target_concept_id ?? record.candidate_id));
  return readable(String(record.title ?? record.name ?? record.label ?? candidate?.name
    ?? record.assertion_kind ?? record.proposal_type ?? 'Suggested knowledge'));
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : 'The review could not be completed. Try again.';
}

function selectionKey(ids: Iterable<string>): string {
  return JSON.stringify([...ids].sort());
}

function ExactValue({ value }: { value: unknown }) {
  if (value === undefined) return null;
  if (Array.isArray(value)) return value.length
    ? <ul className="review-values">{value.map((item, index) => <li key={index}><ExactValue value={item} /></li>)}</ul>
    : <span>None</span>;
  if (value !== null && typeof value === 'object') return <dl className="review-fields">
    {Object.entries(value).filter(([, item]) => item !== undefined).map(([key, item]) => <div key={key}><dt>{readable(key)}</dt><dd><ExactValue value={item} /></dd></div>)}
  </dl>;
  return <span>{value === null ? 'Not specified' : String(value)}</span>;
}

function cautionsFor(record: CurationRecord): string[] {
  const values = ['uncertainty', 'uncertainties', 'limitations', 'warnings'].flatMap((key) => {
    const value = record[key];
    return typeof value === 'string' ? [value] : Array.isArray(value)
      ? value.filter((item): item is string => typeof item === 'string') : [];
  });
  for (const key of ['status', 'trust_state']) {
    const value = record[key];
    if (typeof value === 'string' && /stale|unsupported|unverified/.test(value)) values.push(readable(value));
  }
  return [...new Set(values.filter(Boolean))];
}

export default function ReviewBatch({ connection, snapshot, proposals, canSteward, disabled, onBusyChange, onAccepted, children }: ReviewBatchProps) {
  const panelId = useId();
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<ReviewBatchPreview | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [rationale, setRationale] = useState('');
  const [busy, setBusy] = useState<'preview' | 'accept' | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const requestGeneration = useRef(0);
  const requestInFlight = useRef(false);
  const mounted = useRef(true);
  const snapshotId = snapshot?.id ?? snapshot?.snapshot_id ?? '';
  const snapshotVersion = snapshot?.version;
  const context = JSON.stringify([connection.id, snapshotId, snapshotVersion]);
  const proposalFingerprint = JSON.stringify(proposals);
  const contextRef = useRef(context);
  contextRef.current = context;
  const connectionIdRef = useRef(connection.id);
  connectionIdRef.current = connection.id;
  const busyCallbackRef = useRef(onBusyChange);
  busyCallbackRef.current = onBusyChange;
  const locked = disabled || busy !== null;
  const previewCurrent = preview !== null
    && preview.connection_id === connection.id
    && preview.snapshot_id === snapshotId
    && preview.snapshot_version === snapshotVersion
    && selectionKey(preview.proposals.map((proposal) => String(proposal.id))) === selectionKey(selectedIds);
  const hasSnapshot = Boolean(snapshotId && Number.isSafeInteger(snapshotVersion) && Number(snapshotVersion) > 0);
  const reviewNote = rationale.trim();
  const noteIsValid = reviewNote.length === 0 || (reviewNote.length >= 16 && reviewNote.length <= 1000);
  const canAccept = previewCurrent && reviewed && noteIsValid && !locked;
  const dialogOpen = reviewOpen && previewCurrent;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      requestGeneration.current += 1;
    };
  }, []);

  useEffect(() => {
    requestGeneration.current += 1;
    setSelectedIds(new Set());
    setPreview(null);
    setReviewOpen(false);
    setReviewed(false);
    setRationale('');
    setError('');
  }, [context]);

  useEffect(() => {
    requestGeneration.current += 1;
    const currentIds = new Set(proposals.map((proposal) => proposal.id));
    setSelectedIds((current) => new Set([...current].filter((id) => currentIds.has(id))));
    setPreview(null);
    setReviewOpen(false);
    setReviewed(false);
  }, [proposalFingerprint]);

  useEffect(() => { setNotice(''); }, [connection.id]);

  function toggleSelection(id: string) {
    if (locked) return;
    requestGeneration.current += 1;
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else if (next.size < 100) next.add(id);
      return next;
    });
    setPreview(null);
    setReviewed(false);
    setError('');
    setNotice('');
  }

  function clearSelection() {
    if (locked) return;
    requestGeneration.current += 1;
    setSelectedIds(new Set());
    setPreview(null);
    setReviewed(false);
    setError('');
  }

  async function reviewSelected() {
    if (locked || requestInFlight.current || !hasSnapshot || selectedIds.size === 0) return;
    const requestedIds = [...selectedIds].sort();
    const requestedContext = context;
    const generation = ++requestGeneration.current;
    requestInFlight.current = true;
    setBusy('preview');
    onBusyChange(true);
    setError('');
    setNotice('');
    setPreview(null);
    setReviewed(false);
    try {
      const result = await previewReviewBatch(connection.id, {
        proposal_ids: requestedIds,
        expected_snapshot_id: snapshotId,
        expected_snapshot_version: snapshotVersion!,
      });
      if (!mounted.current || contextRef.current !== requestedContext || requestGeneration.current !== generation) return;
      if (result.connection_id !== connection.id || result.snapshot_id !== snapshotId
        || result.snapshot_version !== snapshotVersion || !result.review_digest
        || result.proposals.length !== requestedIds.length
        || selectionKey(result.proposals.map((proposal) => String(proposal.id))) !== selectionKey(requestedIds)) {
        throw new Error('These suggestions changed. Reload the source and review again.');
      }
      setPreview(result);
      setReviewOpen(true);
    } catch (reason) {
      if (mounted.current && contextRef.current === requestedContext && requestGeneration.current === generation) setError(messageOf(reason));
    } finally {
      requestInFlight.current = false;
      if (mounted.current) setBusy(null);
      busyCallbackRef.current(false);
    }
  }

  async function acceptSelected(publish: boolean) {
    if (!canAccept || requestInFlight.current || !preview || (publish && !canSteward)) return;
    const requestedContext = context;
    requestInFlight.current = true;
    setBusy('accept');
    onBusyChange(true);
    setError('');
    setNotice('');
    let result: ReviewBatchResult | null = null;
    try {
      result = await acceptReviewBatch(connection.id, {
        proposal_ids: [...selectedIds].sort(),
        expected_snapshot_id: preview.snapshot_id,
        expected_snapshot_version: preview.snapshot_version,
        expected_review_digest: preview.review_digest,
        rationale: reviewNote || REVIEW_ACKNOWLEDGEMENT,
        publish,
      });
      // The approval remains committed if publication is blocked; refresh either way.
      await onAccepted(connection.id, result);
      if (!mounted.current || connectionIdRef.current !== connection.id) return;
      setSelectedIds(new Set());
      setPreview(null);
      setReviewOpen(false);
      setReviewed(false);
      setRationale('');
      const remaining = result.remaining_pending_count ? ` ${result.remaining_pending_count} still need review.` : '';
      setNotice(result.status === 'published'
        ? `Accepted and published.${remaining}`
        : result.status === 'accepted_publication_blocked'
          ? `Accepted. Publishing is blocked: ${(result.publication_blockers ?? []).map(readable).join(', ') || result.publication_error || 'the source is not ready'}.${remaining}`
          : `Accepted.${remaining}`);
    } catch (reason) {
      if (mounted.current && (result || contextRef.current === requestedContext)) {
        setPreview(null);
        setReviewOpen(false);
        setReviewed(false);
        if (result) {
          setSelectedIds(new Set());
          setError(`Accepted, but the source could not be reloaded. Reload before another decision. ${messageOf(reason)}`);
        } else setError(messageOf(reason));
      }
    } finally {
      requestInFlight.current = false;
      if (mounted.current) setBusy(null);
      busyCallbackRef.current(false);
    }
  }

  function closeReview() {
    if (busy !== null) return;
    requestGeneration.current += 1;
    setReviewOpen(false);
    setPreview(null);
    setReviewed(false);
  }

  return <div className="review-batch">
    {children({ selectedIds, selectionDisabled: locked, toggleSelection })}
    {proposals.length > 0 && <Flex className="review-batch-toolbar" align="center" justify="between" gap="3" wrap="wrap">
      <Text size="2" color="gray">{selectedIds.size ? `${selectedIds.size} selected` : 'Select suggestions to review.'}</Text>
      <Flex align="center" gap="3">
        {selectedIds.size > 0 && <Button type="button" variant="ghost" color="gray" size="2" onClick={clearSelection} disabled={locked}>Clear</Button>}
        <Button type="button" size="2" onClick={() => void reviewSelected()} loading={busy === 'preview'} disabled={locked || !hasSnapshot || selectedIds.size === 0}>Review selected</Button>
      </Flex>
      {!hasSnapshot && <Text size="2" color="gray">Refresh this source before reviewing.</Text>}
      {selectedIds.size === 100 && <Text size="2" color="gray">Review up to 100 suggestions at a time.</Text>}
    </Flex>}
    {error && !dialogOpen && <Text as="p" size="2" color="red" role="alert">{error}</Text>}
    {notice && <Text as="p" size="2" role="status">{notice}</Text>}
    <Dialog.Root open={dialogOpen} onOpenChange={(open) => { if (!open) closeReview(); }}>
      <Dialog.Content className="review-batch-dialog" maxWidth="640px"
        onEscapeKeyDown={(event) => { if (busy !== null) event.preventDefault(); }}
        onInteractOutside={(event) => { if (busy !== null) event.preventDefault(); }}>
        <Dialog.Title size="5">Review selected knowledge</Dialog.Title>
        <Dialog.Description size="2" color="gray">{connection.name} · {preview?.proposals.length} {preview?.proposals.length === 1 ? 'suggestion' : 'suggestions'}
          {preview && preview.remaining_pending_count > 0 ? ` · ${preview.remaining_pending_count} will still need review` : ''}</Dialog.Description>
        {preview && <div className="review-batch-claims">
          {preview.proposals.map((proposal, index) => {
            const evidence = proposal.evidence_ids?.map((id) => preview.evidence.find((item) => item.id === id));
            const cautions = cautionsFor(proposal);
            const evidenceCautions = [...new Set((evidence ?? []).flatMap((item) => item ? cautionsFor(item) : []))];
            const mapping = proposal.proposed_mapping;
            return <article className="review-batch-claim" key={proposal.id ?? index}>
              <Text as="p" size="3" weight="medium">{titleFor(proposal, snapshot)}</Text>
              {proposal.proposed_definition && <Text as="p" size="2">{proposal.proposed_definition}</Text>}
              {typeof proposal.description === 'string' && proposal.description !== proposal.proposed_definition && <Text as="p" size="2">{proposal.description}</Text>}
              {proposal.payload && <ExactValue value={proposal.payload} />}
              {mapping != null && <ExactValue value={mapping} />}
              {!proposal.payload && mapping == null && Boolean(proposal.subject_id?.trim() && proposal.predicate?.trim() && proposal.object_id?.trim()) && <ExactValue value={{ subject: proposal.subject_id, relationship: proposal.predicate, object: proposal.object_id }} />}
              {proposal.rationale && <Text as="p" size="2" color="gray">{proposal.rationale}</Text>}
              {cautions.map((caution) => <Text as="p" size="2" color="orange" key={caution}>{caution}</Text>)}
              {evidenceCautions.map((caution) => <Text as="p" size="2" color="orange" key={`evidence-${caution}`}>Evidence: {caution}</Text>)}
              {!proposal.evidence_ids?.length && !proposal.evidence_refs?.length && <Text as="p" size="2" color="red">No supporting evidence is cited.</Text>}
              {evidence?.some((item) => !item) && <Text as="p" size="2" color="red">Some supporting evidence is unavailable.</Text>}
              <details className="review-batch-details">
                <summary>Evidence and details</summary>
                {evidence?.map((item, evidenceIndex) => item ? <div className="review-evidence" key={item.id ?? evidenceIndex}>
                  <Text as="p" size="2">{String(item.summary ?? item.description ?? 'No evidence summary supplied.')}</Text>
                  {typeof item.source_pointer === 'string' && <Text as="p" size="1" color="gray">{item.source_pointer}</Text>}
                  {typeof item.trust_state === 'string' && <Text as="p" size="1" color={/stale|unsupported|unverified|inferred/.test(item.trust_state) ? 'orange' : 'gray'}>{readable(item.trust_state)}</Text>}
                  <EvidenceHighlights evidence={item} />
                  <details><summary>Full evidence</summary><pre>{JSON.stringify(item, null, 2)}</pre></details>
                </div> : <Text as="p" size="2" color="red" key={evidenceIndex}>Missing evidence: {proposal.evidence_ids?.[evidenceIndex]}</Text>)}
                {proposal.evidence_refs?.map((reference, referenceIndex) => <div className="review-evidence" key={referenceIndex}>
                  <Text as="p" size="2">Source citation</Text>
                  <dl className="review-fields"><div><dt>Source</dt><dd>{reference.connection_id}</dd></div><div><dt>Snapshot</dt><dd>{reference.snapshot_id}</dd></div><div><dt>Evidence</dt><dd>{reference.evidence_id}</dd></div></dl>
                  <Text as="p" size="1" color="gray">Review this evidence in its referenced source.</Text>
                </div>)}
                <details><summary>Exact proposed record</summary><Text as="p" size="1" color="gray">Snapshot {preview.snapshot_version}: {preview.snapshot_id}</Text><pre>{JSON.stringify(proposal, null, 2)}</pre></details>
              </details>
            </article>;
          })}
          <details className="review-batch-details">
            <summary>Add a review note</summary>
            <div className="review-batch-rationale">
              <TextArea id={`${panelId}-rationale`} aria-label="Review note" value={rationale} onChange={(event) => setRationale(event.target.value)} disabled={locked}
                minLength={16} maxLength={1000} rows={3} placeholder="Why are these claims supported?" aria-invalid={!noteIsValid}
                aria-describedby={`${panelId}-note-hint${!noteIsValid ? ` ${panelId}-note-error` : ''}`} />
              <Text id={`${panelId}-note-hint`} size="1" color="gray">Optional. If you add a note, use 16–1,000 characters.</Text>
            </div>
          </details>
          {!noteIsValid && <Text as="p" size="2" color="red" id={`${panelId}-note-error`}>Use 16–1,000 characters for your review note, or leave it blank.</Text>}
          <Text as="label" size="2" className="review-batch-confirm">
            <Checkbox checked={reviewed} onCheckedChange={(checked) => setReviewed(checked === true)} disabled={locked} />
            {REVIEW_ACKNOWLEDGEMENT}
          </Text>
          {canSteward && <Text as="p" size="2" color="gray">Publishing makes accepted knowledge available to your agents. Unselected suggestions stay in review.</Text>}
          {error && <Text as="p" size="2" color="red" role="alert">{error}</Text>}
          <Flex align="center" justify="end" gap="3" wrap="wrap">
            <Button type="button" variant="soft" color="gray" onClick={closeReview} disabled={busy !== null}>Cancel</Button>
            {canSteward && <Button type="button" variant="ghost" color="gray" onClick={() => void acceptSelected(false)} disabled={!canAccept}>Accept only</Button>}
            <Button type="button" onClick={() => void acceptSelected(canSteward)} loading={busy === 'accept'} disabled={!canAccept}>{canSteward ? 'Accept and publish' : 'Accept'}</Button>
          </Flex>
        </div>}
      </Dialog.Content>
    </Dialog.Root>
  </div>;
}
