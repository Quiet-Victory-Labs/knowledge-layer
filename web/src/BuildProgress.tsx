import { useEffect, useId, useRef, useState } from 'react';
import { Button, Checkbox, Dialog, Flex, Heading, Select, Separator, Spinner, Text, TextArea, TextField } from './ui';
import {
  cancelKnowledgeBuild,
  getKnowledgeBuild,
  listKnowledgeBuilds,
  startKnowledgeBuild,
  type KnowledgeBuild,
  type KnowledgeBuildStatus,
  type KnowledgeBuildSummary,
  type KnowledgeConnection,
} from './platformApi';
import { reviewableProposals } from './reviewStatus';
import './buildProgress.css';

const ACTIVE_STATUSES = new Set<KnowledgeBuildStatus>(['queued', 'waiting_for_discovery', 'running']);
const STATUS_LABELS: Record<KnowledgeBuildStatus, string> = {
  queued: 'Waiting for an agent',
  waiting_for_discovery: 'Waiting for discovery',
  running: 'Agent work in progress',
  awaiting_review: 'Ready for human review',
  ready: 'Published for agents',
  needs_attention: 'Needs attention',
  cancelled: 'Cancelled',
  budget_exhausted: 'Budget reached',
};
const POLL_WINDOW_MS = 5 * 60 * 1000;
const MAX_STATUS_READS = 60;

type BuildProgressProps = {
  connections: KnowledgeConnection[];
  selectedConnectionId: string;
  totalSources: number;
  disabled: boolean;
  onSelectConnection: (id: string) => void;
  onConnect: () => void;
  onReview: (id: string) => Promise<void>;
};

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : 'The build request could not be completed. Try again.';
}

function displayDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(parsed);
}

function statusDescription(build: KnowledgeBuild): string {
  switch (build.status) {
    case 'queued': return build.execution.worker_configured
      ? 'The run is saved and a worker is configured. Waiting for an agent to claim work.'
      : 'The run is saved. Run the resume prompt in your connected agent to begin work.';
    case 'waiting_for_discovery': return 'The selected sources need discovery before the agent can draft their knowledge. The run will use current evidence when discovery finishes.';
    case 'running': return 'An agent is processing the saved work. Proposed meanings stay drafts until you review them.';
    case 'awaiting_review': return 'The agent work is ready for your review. Inspect the proposed definitions and evidence, then accept the supported claims.';
    case 'ready': return 'The selected source snapshots are currently published for agent use. Retrieve their reviewed knowledge through the same connected agents.';
    case 'needs_attention': return build.counts.abstained > 0 && !build.counts.errors && !build.counts.deferred && !build.counts.stale
      ? 'The agent saved supported drafts and abstained where evidence was insufficient. Review the drafts and the remaining gaps separately.'
      : 'Some work needs more context, valid evidence, or a corrected agent result. Review the uncertainty report before continuing.';
    case 'budget_exhausted': return 'This run reached its work or time budget. Completed drafts remain available for review; start another bounded run for remaining work.';
    case 'cancelled': return 'This run no longer accepts new work. Its saved drafts remain available for review.';
  }
}

export default function BuildProgress({ connections, selectedConnectionId, totalSources, disabled, onSelectConnection, onConnect, onReview }: BuildProgressProps) {
  const fieldId = useId();
  const selectedSource = connections.find((connection) => connection.id === selectedConnectionId) ?? connections[0] ?? null;
  const sourceId = selectedSource?.id ?? '';
  const sourceRef = useRef(sourceId);
  sourceRef.current = sourceId;
  const mounted = useRef(true);
  const mutationPending = useRef(false);
  const [scope, setScope] = useState<{ sourceId: string; ids: string[] }>({ sourceId, ids: sourceId ? [sourceId] : [] });
  const scopedIds = scope.sourceId === sourceId ? scope.ids : sourceId ? [sourceId] : [];
  const [maxItems, setMaxItems] = useState('200');
  const [minutes, setMinutes] = useState('60');
  const [builds, setBuilds] = useState<KnowledgeBuildSummary[]>([]);
  const [runId, setRunId] = useState('');
  const [build, setBuild] = useState<KnowledgeBuild | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [mutation, setMutation] = useState<'start' | 'cancel' | null>(null);
  const [reviewingId, setReviewingId] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [listRevision, setListRevision] = useState(0);
  const [statusRevision, setStatusRevision] = useState(0);
  const [pollPaused, setPollPaused] = useState(false);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [cancelReason, setCancelReason] = useState('');
  const [copied, setCopied] = useState<'prompt' | 'endpoint' | null>(null);
  const [reportLimit, setReportLimit] = useState(12);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const endpointRef = useRef<HTMLInputElement>(null);
  const currentBuild = build?.id === runId && build.connection_ids.includes(sourceId) ? build : null;
  const activeRun = builds.find((item) => ACTIVE_STATUSES.has(item.status) && item.connection_ids.some((id) => scopedIds.includes(id)));
  const locked = disabled || mutation !== null;
  const boundsValid = Number.isInteger(Number(maxItems)) && Number(maxItems) >= 1 && Number(maxItems) <= 500
    && Number.isInteger(Number(minutes)) && Number(minutes) >= 1 && Number(minutes) <= 1440;
  const unfinished = currentBuild?.tasks?.filter((task) => ['abstained', 'deferred', 'error', 'errors', 'stale'].includes(task.status)) ?? [];
  const endpoint = `${window.location.origin}/agent/mcp/`;
  const resumePrompt = currentBuild
    ? currentBuild.status === 'ready'
      ? `Use my configured knowledge-layer MCP to retrieve current published context for these sources: ${JSON.stringify(currentBuild.connection_ids)}. Use resolve_context, get_contract, or get_task_context as appropriate to answer my questions about their data. Cite source and snapshot provenance, retain each claim's trust state, and report unresolved definitions or coverage gaps.`
      : `Use my configured knowledge-layer MCP tool ${currentBuild.resume.mcp_tool} with ${JSON.stringify(currentBuild.resume.arguments)}. Process the claimed work on both meaning and contract tracks using its pinned source evidence. Draft supported metadata, glossary definitions, ontology, grains, joins, and metrics; preserve exact citations and uncertainty. Follow the tool's checkpoint instructions, then claim the next work until this run needs human review, needs attention, or reaches its budget. Record abstentions, deferrals, errors, and coverage gaps. Stop for human acceptance; never approve or publish your own drafts.`
    : '';

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    setScope({ sourceId, ids: sourceId ? [sourceId] : [] });
    setBuilds([]);
    setRunId('');
    setBuild(null);
    setError('');
    setNotice('');
    setCancelOpen(false);
    setDetailsOpen(false);
    setCancelReason('');
    setCopied(null);
    setReportLimit(12);
    if (!sourceId) return;
    const controller = new AbortController();
    setListLoading(true);
    void listKnowledgeBuilds(sourceId, controller.signal).then((items) => {
      if (controller.signal.aborted || sourceRef.current !== sourceId) return;
      setBuilds(items);
      setRunId((items.find((item) => ACTIVE_STATUSES.has(item.status)) ?? items[0])?.id ?? '');
    }).catch((reason) => {
      if (!controller.signal.aborted && sourceRef.current === sourceId) setError(messageOf(reason));
    }).finally(() => {
      if (!controller.signal.aborted && sourceRef.current === sourceId) setListLoading(false);
    });
    return () => { controller.abort(); };
  }, [sourceId, listRevision]);

  useEffect(() => {
    setBuild(null);
    setPollPaused(false);
    setCancelOpen(false);
    setCancelReason('');
    setReportLimit(12);
    setCopied(null);
    if (!runId || !sourceId) return;
    const controller = new AbortController();
    const deadline = Date.now() + POLL_WINDOW_MS;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let reads = 0;
    let failures = 0;
    setDetailLoading(true);
    async function readStatus() {
      if (controller.signal.aborted || sourceRef.current !== sourceId) return;
      if (reads > 0 && (Date.now() >= deadline || reads >= MAX_STATUS_READS)) {
        setPollPaused(true);
        return;
      }
      if (reads > 0 && document.hidden) {
        timer = setTimeout(() => void readStatus(), 10000);
        return;
      }
      if (mutationPending.current) {
        timer = setTimeout(() => void readStatus(), 5000);
        return;
      }
      reads += 1;
      try {
        const result = await getKnowledgeBuild(runId, controller.signal);
        if (controller.signal.aborted || sourceRef.current !== sourceId) return;
        if (mutationPending.current) {
          timer = setTimeout(() => void readStatus(), 5000);
          return;
        }
        if (result.id !== runId || !result.connection_ids.includes(sourceId)) throw new Error('The returned build does not match this source. Refresh the build list.');
        setBuild(result);
        setBuilds((current) => current.map((item) => item.id === result.id ? result : item));
        setError('');
        failures = 0;
        if (ACTIVE_STATUSES.has(result.status)) timer = setTimeout(() => void readStatus(), 5000);
      } catch (reason) {
        if (controller.signal.aborted || sourceRef.current !== sourceId) return;
        setError(messageOf(reason));
        failures += 1;
        if (failures < 3 && Date.now() < deadline) timer = setTimeout(() => void readStatus(), 10000);
        else setPollPaused(true);
      } finally {
        if (!controller.signal.aborted && sourceRef.current === sourceId) setDetailLoading(false);
      }
    }
    void readStatus();
    return () => {
      controller.abort();
      if (timer !== null) clearTimeout(timer);
    };
  }, [sourceId, runId, statusRevision]);

  function toggleSource(id: string) {
    if (locked || id === sourceId) return;
    const next = new Set(scopedIds);
    if (next.has(id)) next.delete(id);
    else if (next.size < 8) next.add(id);
    setScope({ sourceId, ids: [...next] });
  }

  async function startBuild() {
    if (locked || mutationPending.current || listLoading || !boundsValid || !scopedIds.length || activeRun) return;
    mutationPending.current = true;
    const requestedSource = sourceId;
    setMutation('start');
    setError('');
    setNotice('');
    try {
      const result = await startKnowledgeBuild({ connection_ids: scopedIds, max_items: Number(maxItems), max_seconds: Number(minutes) * 60 });
      if (!mounted.current || sourceRef.current !== requestedSource) return;
      setBuilds((current) => [result, ...current.filter((item) => item.id !== result.id)]);
      setRunId(result.id);
      setNotice('Build saved.');
    } catch (reason) {
      if (mounted.current && sourceRef.current === requestedSource) setError(messageOf(reason));
    } finally {
      mutationPending.current = false;
      if (mounted.current) setMutation(null);
    }
  }

  async function cancelBuild() {
    if (locked || mutationPending.current || !currentBuild || cancelReason.trim().length < 8) return;
    const requestedSource = sourceId;
    const requestedRun = currentBuild.id;
    mutationPending.current = true;
    setMutation('cancel');
    setError('');
    try {
      const result = await cancelKnowledgeBuild(requestedRun, cancelReason.trim());
      if (!mounted.current || sourceRef.current !== requestedSource) return;
      setBuild(result);
      setBuilds((current) => current.map((item) => item.id === result.id ? result : item));
      setCancelOpen(false);
      setCancelReason('');
      setNotice('Build cancelled. Saved drafts remain available.');
      setStatusRevision((value) => value + 1);
    } catch (reason) {
      if (mounted.current && sourceRef.current === requestedSource) setError(messageOf(reason));
    } finally {
      mutationPending.current = false;
      if (mounted.current) setMutation(null);
    }
  }

  async function reviewSource(id: string) {
    if (reviewingId || locked) return;
    setReviewingId(id);
    setError('');
    try { await onReview(id); }
    catch (reason) { if (mounted.current) setError(messageOf(reason)); }
    finally { if (mounted.current) setReviewingId(''); }
  }

  async function copyText(kind: 'prompt' | 'endpoint') {
    try {
      await navigator.clipboard.writeText(kind === 'prompt' ? resumePrompt : endpoint);
      if (mounted.current) setCopied(kind);
    } catch {
      const field = kind === 'prompt' ? promptRef.current : endpointRef.current;
      field?.focus();
      field?.select();
    }
  }

  function refreshStatus() {
    setError('');
    if (runId) setStatusRevision((value) => value + 1);
    else setListRevision((value) => value + 1);
  }

  const uncertainty = currentBuild ? [
    currentBuild.counts.abstained ? `${currentBuild.counts.abstained} abstained` : '',
    currentBuild.counts.deferred ? `${currentBuild.counts.deferred} deferred` : '',
    currentBuild.counts.errors ? `${currentBuild.counts.errors} ${currentBuild.counts.errors === 1 ? 'error' : 'errors'}` : '',
    currentBuild.counts.stale ? `${currentBuild.counts.stale} stale items` : '',
  ].filter(Boolean).join(' · ') : '';
  const pendingDraftCount = reviewableProposals(selectedSource?.snapshot).length;
  const hasActiveExecution = Boolean(activeRun) || Boolean(currentBuild && ACTIVE_STATUSES.has(currentBuild.status));
  const emptyCancelledHistory = currentBuild?.status === 'cancelled' && currentBuild.counts.total === 0 && !uncertainty;
  const visibleBuild = currentBuild && !emptyCancelledHistory;
  const sourceIncomplete = selectedSource?.snapshot?.discovery_complete === false || selectedSource?.discovery_complete === false
    || ['partial', 'error'].includes(selectedSource?.snapshot?.status ?? '');
  const statusMayBeStale = pollPaused || Boolean(error);
  const canResume = currentBuild && (ACTIVE_STATUSES.has(currentBuild.status) || currentBuild.status === 'ready');
  const reviewNext = !hasActiveExecution && pendingDraftCount > 0;
  const nextLabel = statusMayBeStale ? 'Refresh status'
    : reviewNext ? 'Review drafts'
    : currentBuild?.status === 'queued' ? 'Resume with agent'
    : currentBuild?.status === 'ready' ? 'Use with agent'
    : currentBuild && ACTIVE_STATUSES.has(currentBuild.status) ? 'Refresh status' : 'View details';

  function nextAction() {
    if (statusMayBeStale || currentBuild && ['running', 'waiting_for_discovery'].includes(currentBuild.status)) refreshStatus();
    else if (reviewNext) void reviewSource(sourceId);
    else setDetailsOpen(true);
  }

  return <section className="knowledge-build-page" aria-labelledby={`${fieldId}-title`}>
    <Flex direction="column" gap="5">
      <Heading as="h1" size="7" id={`${fieldId}-title`}>Build knowledge</Heading>
      {error && <Text as="p" color="red" role="alert">{error}</Text>}
      {notice && <Text as="p" size="2" color="gray" role="status">{notice}</Text>}
      {selectedSource ? <>
        <Flex gap="3" align="end" wrap="wrap">
          <Flex direction="column" gap="2" flexGrow="1" flexBasis="200px" minWidth="0">
            <Text as="label" htmlFor={`${fieldId}-source`} size="2">Source</Text>
            <Select.Root value={sourceId} onValueChange={onSelectConnection} disabled={mutation !== null}>
              <Select.Trigger id={`${fieldId}-source`} />
              <Select.Content>{connections.map((connection) => <Select.Item key={connection.id} value={connection.id}>{connection.name}</Select.Item>)}</Select.Content>
            </Select.Root>
          </Flex>
          {activeRun && activeRun.id !== runId
            ? <Button variant="soft" onClick={() => setRunId(activeRun.id)} disabled={locked}>View active build</Button>
            : <Button variant={reviewNext ? 'soft' : 'solid'} onClick={() => void startBuild()} loading={mutation === 'start'} disabled={locked || listLoading || !boundsValid || scopedIds.length === 0 || Boolean(activeRun)}>Build</Button>}
        </Flex>
        {scopedIds.length > 1 && <Text as="p" size="2" color="gray">{scopedIds.length} sources selected</Text>}
        {!hasActiveExecution && <Text as="p" size="2" color="gray">Your connected agent or worker runs saved builds.</Text>}
        {!boundsValid && <Text as="p" size="2" color="red" role="alert">Set 1–500 work items and 1–1440 minutes in Details.</Text>}
        {(selectedSource.last_error || selectedSource.error) && <Text as="p" size="2" color="red" role="alert">Discovery needs attention: {selectedSource.last_error || selectedSource.error}</Text>}
        {sourceIncomplete && (!visibleBuild || currentBuild?.coverage.complete) && <Text as="p" size="2" color="amber">Source discovery is incomplete.</Text>}
        {Boolean(selectedSource.stale_assertion_count) && <Flex gap="3" align="center" wrap="wrap"><Text as="p" size="2" color="amber">{selectedSource.stale_assertion_count} previously approved {selectedSource.stale_assertion_count === 1 ? 'claim is' : 'claims are'} inactive after the source changed. Retire outdated claims in Review, then start a new build.</Text><Button variant="soft" onClick={() => void reviewSource(sourceId)} disabled={locked || Boolean(reviewingId)}>Resolve source change</Button></Flex>}
        {listLoading || detailLoading && !currentBuild
          ? <Flex align="center" gap="2" role="status"><Spinner size="1" /><Text size="2" color="gray">Loading build…</Text></Flex>
          : visibleBuild && currentBuild ? <Flex direction="column" gap="3">
            <Flex align="center" justify="between" gap="3" wrap="wrap">
              <Text as="p" weight="medium" role="status">{statusMayBeStale ? 'Last known: ' : ''}{STATUS_LABELS[currentBuild.status]}</Text>
              <Button variant={reviewNext ? 'solid' : 'soft'} onClick={nextAction} loading={Boolean(reviewingId) || detailLoading} disabled={locked || Boolean(reviewingId) || listLoading}>{nextLabel}</Button>
            </Flex>
            <Text as="p" size="2" color="gray">{currentBuild.connection_ids.length > 1 ? `${currentBuild.connection_ids.length} sources · ` : ''}{currentBuild.counts.proposed} drafted · {currentBuild.counts.pending} pending</Text>
            {!currentBuild.coverage.complete && <Text as="p" size="2" color="amber">Incomplete coverage{currentBuild.coverage.reason ? `: ${currentBuild.coverage.reason}` : '. Some work remains.'}</Text>}
            {uncertainty && <Text as="p" size="2" color="amber">{uncertainty}{currentBuild.counts.abstained && !currentBuild.counts.errors && !currentBuild.counts.deferred && !currentBuild.counts.stale ? '. These items remain unapproved because evidence was insufficient.' : '. See Details before accepting drafts.'}</Text>}
            {statusMayBeStale && <Text as="p" size="2" color="amber">Status may be stale. Refresh to check.</Text>}
          </Flex>
          : (!currentBuild && runId) || statusMayBeStale ? <Flex align="center" gap="3" wrap="wrap"><Text size="2" color="red">Run status is unavailable.</Text><Button variant="soft" onClick={refreshStatus} disabled={locked || listLoading}>Refresh status</Button></Flex>
          : !hasActiveExecution && pendingDraftCount > 0 && <Flex align="center" justify="between" gap="3" wrap="wrap"><Text size="2">{pendingDraftCount} {pendingDraftCount === 1 ? 'draft is' : 'drafts are'} ready for review.</Text><Button onClick={() => void reviewSource(sourceId)} loading={Boolean(reviewingId)} disabled={locked || Boolean(reviewingId)}>Review drafts</Button></Flex>}
      </> : <Flex direction="column" gap="3" align="start"><Text size="2" color="gray">Connect a source to begin.</Text><Button onClick={onConnect}>Connect source</Button></Flex>}
      <Dialog.Root open={detailsOpen} onOpenChange={setDetailsOpen}>
        <Dialog.Trigger><Button variant="ghost" style={{ alignSelf: 'start' }}>Details</Button></Dialog.Trigger>
        <Dialog.Content maxWidth="600px">
          <Dialog.Title>Build details</Dialog.Title>
          <Dialog.Description size="2">Agent setup, scope, and saved work.</Dialog.Description>
          <Flex direction="column" gap="5" mt="5">
            {error && <Text as="p" color="red" role="alert">{error}</Text>}
            <Flex direction="column" gap="3">
              <Heading as="h2" size="3">Agent setup</Heading>
              <Text as="p" size="2" color="gray">Add this MCP endpoint in Codex, Cursor, or Claude with the access token supplied by your operator. Ask the agent to call get_build_handoff to verify access.</Text>
              <Text as="label" size="2" htmlFor={`${fieldId}-endpoint`}>MCP endpoint</Text>
              <TextField.Root id={`${fieldId}-endpoint`} ref={endpointRef} readOnly value={endpoint} onFocus={(event) => event.currentTarget.select()} />
              <Flex><Button variant="soft" onClick={() => void copyText('endpoint')}>{copied === 'endpoint' ? 'Copied' : 'Copy endpoint'}</Button></Flex>
              <Text as="p" size="2" color="gray">Remote agents need your operator’s public HTTPS URL. Localhost works on this computer.</Text>
              {canResume && <>
                <Text as="label" size="2" htmlFor={`${fieldId}-prompt`}>{currentBuild.status === 'ready' ? 'Use reviewed knowledge' : 'Resume saved work'}</Text>
                <TextArea id={`${fieldId}-prompt`} ref={promptRef} readOnly value={resumePrompt} rows={5} onFocus={(event) => event.currentTarget.select()} />
                <Flex><Button variant="soft" onClick={() => void copyText('prompt')}>{copied === 'prompt' ? 'Copied' : currentBuild.status === 'ready' ? 'Copy use prompt' : 'Copy resume prompt'}</Button></Flex>
              </>}
              {currentBuild && !emptyCancelledHistory && <Text as="p" size="2" color="gray">{statusDescription(currentBuild)}</Text>}
            </Flex>
            {selectedSource && <>
              <Separator size="4" />
              <details className="knowledge-build-settings">
                <summary>Sources and limits</summary>
                <Flex direction="column" gap="3" mt="3">
                <Text as="p" size="2" color="gray">Include up to 8 connected sources. Limits apply to the next build.</Text>
                {connections.map((connection) => <Text as="label" size="2" key={connection.id}>
                  <Flex align="center" gap="2">
                    <Checkbox checked={scopedIds.includes(connection.id)} onCheckedChange={() => toggleSource(connection.id)} disabled={locked || connection.id === sourceId || (!scopedIds.includes(connection.id) && scopedIds.length >= 8)} />
                    {connection.name}{connection.id === sourceId ? ' · selected' : ''}
                  </Flex>
                </Text>)}
                {connections.length < totalSources && <Text as="p" size="2" color="gray">Showing {connections.length} of {totalSources} sources. Load more in Sources to include them.</Text>}
                <Flex gap="3" wrap="wrap">
                  <Flex direction="column" gap="2" flexGrow="1"><Text as="label" size="2" htmlFor={`${fieldId}-items`}>Work items</Text><TextField.Root id={`${fieldId}-items`} type="number" value={maxItems} min={1} max={500} step={1} onChange={(event) => setMaxItems(event.target.value)} disabled={locked} /></Flex>
                  <Flex direction="column" gap="2" flexGrow="1"><Text as="label" size="2" htmlFor={`${fieldId}-minutes`}>Minutes</Text><TextField.Root id={`${fieldId}-minutes`} type="number" value={minutes} min={1} max={1440} step={1} onChange={(event) => setMinutes(event.target.value)} disabled={locked} /></Flex>
                </Flex>
                {!boundsValid && <Text as="p" size="2" color="red">Use 1–500 items and 1–1440 minutes.</Text>}
                </Flex>
              </details>
            </>}
            {(builds.length > 0 || currentBuild) && <details className="knowledge-build-settings">
              <summary>Saved work</summary>
              <Flex direction="column" gap="4" mt="3">
            {builds.length > 0 && <>
              <Separator size="4" />
              <Flex direction="column" gap="3">
                <Heading as="h2" size="3">Saved runs</Heading>
                <Select.Root value={runId} onValueChange={(id) => { setRunId(id); setError(''); setNotice(''); }} disabled={mutation !== null}>
                  <Select.Trigger aria-label="Saved run" />
                  <Select.Content>{builds.map((item) => <Select.Item key={item.id} value={item.id}>{STATUS_LABELS[item.status]} · {displayDate(item.created_at)}</Select.Item>)}</Select.Content>
                </Select.Root>
                <Flex><Button variant="soft" onClick={refreshStatus} loading={detailLoading || listLoading} disabled={locked}>Refresh status</Button></Flex>
              </Flex>
            </>}
            {currentBuild && <>
              <Separator size="4" />
              <Flex direction="column" gap="3">
                <Heading as="h2" size="3">Unfinished work</Heading>
                <Text as="p" size="2" color="gray">{currentBuild.counts.proposed} drafted · {currentBuild.counts.leased} in progress · {currentBuild.counts.pending} pending · {currentBuild.counts.total} work items</Text>
                <Text as="p" size="2" color={currentBuild.coverage.complete ? 'gray' : 'amber'}>{currentBuild.coverage.complete ? 'This run’s work coverage is complete.' : `Incomplete coverage: ${currentBuild.coverage.reason || 'Some work remains.'}`}</Text>
                <Text as="p" size="2" color={uncertainty ? 'amber' : 'gray'}>{uncertainty || 'No abstentions, deferrals, errors, or stale outcomes recorded.'}</Text>
                {unfinished.length > 0 ? <ul className="knowledge-build-outcomes">{unfinished.slice(0, reportLimit).map((task) => <li key={task.id}>
                  <Text as="p" size="2" weight="medium">{task.kind.replaceAll('_', ' ')} · {task.track}</Text>
                  <Text as="p" size="2" color="gray">{connections.find((connection) => connection.id === task.connection_id)?.name ?? task.connection_id} · {task.status.replaceAll('_', ' ')}</Text>
                  <Text as="p" size="2">{task.reason || 'No reason was recorded.'}</Text>
                </li>)}</ul> : uncertainty && <Text as="p" size="2" color="gray">Detailed outcomes were not returned for this run.</Text>}
                {reportLimit < unfinished.length && <Flex><Button variant="soft" onClick={() => setReportLimit((value) => value + 12)}>Show more</Button></Flex>}
              </Flex>
              <Separator size="4" />
              <Flex direction="column" gap="3" className="knowledge-build-details-text">
                <Heading as="h2" size="3">Sources in this run</Heading>
                {currentBuild.sources.map((source) => <Flex direction="column" gap="2" key={source.connection_id}>
                  <Text as="p" size="2" weight="medium">{connections.find((connection) => connection.id === source.connection_id)?.name ?? source.connection_id}</Text>
                  <Text as="p" size="2" color="gray">{source.status.replaceAll('_', ' ')} · Snapshot {source.snapshot_id ?? 'pending'}{source.snapshot_version ? ` · version ${source.snapshot_version}` : ''}</Text>
                  <Flex><Button variant="soft" onClick={() => { setDetailsOpen(false); void reviewSource(source.connection_id); }} loading={reviewingId === source.connection_id} disabled={locked || Boolean(reviewingId)}>Review source</Button></Flex>
                </Flex>)}
                <Text as="p" size="2" color="gray">Run {currentBuild.id}<br />Updated {displayDate(currentBuild.updated_at)}<br />Budget ends {displayDate(currentBuild.expires_at)}</Text>
              </Flex>
              {ACTIVE_STATUSES.has(currentBuild.status) && <Flex><Button variant="ghost" color="red" onClick={() => { setDetailsOpen(false); setCancelOpen(true); }} disabled={locked}>Cancel run</Button></Flex>}
            </>}
              </Flex>
            </details>}
            <Flex justify="end"><Dialog.Close><Button variant="soft">Close</Button></Dialog.Close></Flex>
          </Flex>
        </Dialog.Content>
      </Dialog.Root>
      <Dialog.Root open={cancelOpen} onOpenChange={setCancelOpen}>
        <Dialog.Content maxWidth="450px">
          <Dialog.Title>Cancel this run?</Dialog.Title>
          <Dialog.Description size="2">Further work stops. Saved drafts and source connections remain available.</Dialog.Description>
          <Flex direction="column" gap="3" mt="4">
            {error && <Text as="p" color="red" role="alert">{error}</Text>}
            <Text as="label" size="2" htmlFor={`${fieldId}-cancel`}>Reason</Text>
            <TextArea id={`${fieldId}-cancel`} value={cancelReason} onChange={(event) => setCancelReason(event.target.value)} minLength={8} maxLength={1000} rows={2} disabled={locked} placeholder="At least 8 characters" />
            <Flex gap="3" justify="end" wrap="wrap">
              <Dialog.Close><Button variant="soft" disabled={locked}>Keep run</Button></Dialog.Close>
              <Button color="red" onClick={() => void cancelBuild()} loading={mutation === 'cancel'} disabled={locked || cancelReason.trim().length < 8}>Cancel run</Button>
            </Flex>
          </Flex>
        </Dialog.Content>
      </Dialog.Root>
    </Flex>
  </section>;
}
