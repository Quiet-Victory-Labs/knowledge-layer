import type { KnowledgeMapLocation } from './KnowledgeMap';
import ReviewBatch from './ReviewBatch';
import BuildProgress from './BuildProgress';
import SetupAuthGate from './SetupAuthGate';
import SetupGuide from './SetupGuide';
import { EvidenceHighlights, relationshipTitle } from './reviewEvidence';
import { reviewableProposals, staleApprovedContractProposals } from './reviewStatus';
import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { ChevronDown, Layers3, LoaderCircle, Plus } from 'lucide-react';
import { Button, Callout, Checkbox, Dialog, DropdownMenu, Flex, Heading, IconButton, Select, Text, TextArea, TextField } from './ui';
import {
  createConnection,
  archiveProposals,
  decideProposal,
  getConnection,
  getConnectionPage,
  getConnectorTypes,
  getPublicationPreview,
  getStewardRole,
  PlatformApiError,
  publishSource,
  refreshConnection,
  signOut,
  withdrawPublication,
  type ConfigField,
  type AuthStatus,
  type ConnectorType,
  type CurationRecord,
  type KnowledgeConnection,
  type KnowledgeSnapshot,
  type PublicationPreview,
  type ReviewBatchResult,
} from './platformApi';

const KnowledgeMap = lazy(() => import('./KnowledgeMap'));

type Section = 'sources' | 'build' | 'curation' | 'map';
type FieldValues = Record<string, string>;

const ADVANCED_FIELDS = new Set(['include', 'max_files', 'max_file_bytes', 'max_total_bytes', 'profile_rows', 'max_columns', 'max_tables', 'max_objects', 'max_assets', 'connect_timeout', 'retain_semantic_samples']);

function fieldLabel(field: ConfigField): string {
  if (field.label) return field.label;
  const labels: Record<string, string> = {
    path: 'Dataset folder',
    catalog: 'Catalog',
    namespace: 'Dataset namespace',
    schema: 'Dataset schema',
    tables: 'Tables to include',
    secret_ref: 'Access reference',
    provider: 'Database or warehouse',
    prefix: 'Folder or object prefix',
    max_tables: 'Maximum tables',
    max_objects: 'Maximum objects',
    max_assets: 'Maximum assets',
    scope: 'Source area',
    include: 'Files to include',
    max_files: 'Maximum files to scan',
    max_file_bytes: 'Maximum file size',
    max_total_bytes: 'Maximum total data scanned',
    profile_rows: 'Rows to profile per table',
    max_columns: 'Maximum columns per table',
    connect_timeout: 'Connection timeout',
  };
  return labels[field.name] ?? field.name.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
}

function fieldHelp(field: ConfigField, kind: string): string | undefined {
  if (field.description) return field.description;
  if (field.name === 'path') return 'Choose a folder within the configured source files.';
  if (field.name === 'secret_ref') return 'Enter the name of a server-side access reference. Do not enter a password.';
  if (field.name === 'tables') return 'Optional. Leave blank to include all tables in this scope.';
  if (field.name === 'namespace') return 'The dataset area inside this catalog.';
  if (field.name === 'schema' && kind === 'postgres') return 'The database area to include. Defaults to public.';
  if (field.name === 'schema' && kind === 'sql_catalog') return 'Optional. Leave blank for the source default schema.';
  if (field.name === 'prefix') return 'Only objects under this prefix are listed. Object contents stay at the source.';
  if (field.name === 'scope') return 'Choose an area your administrator enabled for this source.';
  if (field.type === 'environment_secret_reference') return 'Enter a reference name only. Secret values stay on the server.';
  return undefined;
}

function initialValues(fields: ConfigField[]): FieldValues {
  return Object.fromEntries(fields.map((field) => {
    const options = field.options ?? [];
    const defaultValue = field.default === undefined || field.default === null ? '' : String(field.default);
    const hasDefault = options.some((option) => (typeof option === 'string' ? option : option.value) === defaultValue);
    const optionValue = options.length && !hasDefault
      ? (typeof options[0] === 'string' ? options[0] : options[0].value)
      : defaultValue;
    return [field.name, optionValue];
  }));
}

function suggestedConnectionName(type: ConnectorType, values: FieldValues): string {
  if (type.id === 'local_files') {
    const folder = type.config_fields.find((field) => field.name === 'path');
    const option = folder?.options?.find((item) => (typeof item === 'string' ? item : item.value) === values.path);
    if (option && typeof option !== 'string' && option.label) return option.label;
  }
  return type.name;
}

function convertConfig(fields: ConfigField[], values: FieldValues): Record<string, unknown> {
  const config: Record<string, unknown> = {};
  for (const field of fields) {
    const raw = values[field.name]?.trim() ?? '';
    if (!raw) continue;
    if (field.type === 'integer') {
      config[field.name] = Number.parseInt(raw, 10);
    } else if (field.type === 'boolean') {
      config[field.name] = raw === 'true';
    } else if (field.type === 'identifier_list') {
      config[field.name] = raw.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
    } else {
      config[field.name] = raw;
    }
  }
  return config;
}

function prettyKind(kind: string, connectorTypes: ConnectorType[]): string {
  return connectorTypes.find((item) => item.id === kind)?.name ?? kind.replaceAll('_', ' ');
}

function prettyStatus(connection: KnowledgeConnection): { label: string; tone: 'good' | 'pending' | 'attention' } {
  if (connection.refresh_job) return { label: connection.refresh_job.status === 'retry_wait' ? 'Retrying discovery' : 'Discovering', tone: 'pending' };
  if (connection.status === 'error' || connection.last_error || connection.error) return { label: 'Needs attention', tone: 'attention' };
  if ((connection.stale_assertion_count ?? 0) > 0) return { label: 'Needs attention', tone: 'attention' };
  if (connection.agent_ready === true || connection.snapshot?.agent_ready === true) {
    const pending = connection.needs_attention_count ?? reviewableProposals(connection.snapshot).length;
    return pending > 0
      ? { label: `Published · ${pending} ${pending === 1 ? 'draft' : 'drafts'} to review`, tone: 'pending' }
      : { label: 'Published knowledge', tone: 'good' };
  }
  if (connection.status === 'inventory_complete') return { label: 'Inventory ready', tone: 'pending' };
  if (connection.status === 'partial') return { label: 'Partly discovered', tone: 'pending' };
  if (connection.status === 'discovery_complete' || connection.discovery_complete || connection.snapshot?.discovery_complete || ['candidate_review_required', 'needs_context'].includes(String(connection.curation_status ?? connection.snapshot?.curation_status ?? ''))) return { label: 'Review needed', tone: 'pending' };
  if (lastSync(connection)) return { label: 'Synced', tone: 'pending' };
  return { label: 'Connected', tone: 'pending' };
}

function formatDate(value?: string | null): string {
  if (!value) return 'Not synced yet';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function lastSync(connection: KnowledgeConnection): string | null {
  return connection.last_refreshed_at ?? connection.last_synced_at ?? null;
}

function recordTitle(record: CurationRecord, snapshot: KnowledgeSnapshot | null): string {
  const title = [record.title, record.name, record.label].find((value) => typeof value === 'string' && value.trim());
  if (typeof title === 'string') return title;
  const relationship = relationshipTitle(record, snapshot);
  if (relationship) return relationship;
  const target = [...(snapshot?.concepts ?? []), ...(snapshot?.metrics ?? []), ...(snapshot?.relationships ?? [])]
    .find((item) => item.id === (record.target_concept_id ?? record.candidate_id));
  if (typeof target?.name === 'string') return target.name;
  if (record.assertion_kind) {
    const field = record.payload?.field_name ?? record.payload?.field;
    return `${record.assertion_kind.replaceAll('_', ' ')}${typeof field === 'string' ? ` · ${field}` : ''}`;
  }
  const kind = String(record.proposal_type ?? record.type ?? '').toLowerCase();
  if (kind === 'relationship_mapping') return 'Possible relationship';
  if (kind === 'metric_definition') return 'Metric needs a definition';
  if (kind === 'semantic_correction') return 'Business meaning update';
  return 'Suggested meaning';
}

function recordDescription(record: CurationRecord): string {
  if (typeof record.proposed_definition === 'string' && record.proposed_definition.trim()) return record.proposed_definition;
  if (typeof record.payload?.description === 'string' && record.payload.description.trim()) return record.payload.description;
  if (typeof record.description === 'string' && record.description.trim()) return record.description;
  if (typeof record.rationale === 'string' && record.rationale.trim()) return record.rationale;
  const relationship = [record.subject_id, record.predicate, record.object_id].filter((value) => typeof value === 'string' && value.trim());
  if (relationship.length) return relationship.join(' · ');
  return 'This suggestion was created from the connected source and is waiting for review.';
}

function evidenceFor(proposal: CurationRecord, snapshot: KnowledgeSnapshot | null): CurationRecord[] {
  const ids = new Set(Array.isArray(proposal.evidence_ids) ? proposal.evidence_ids : []);
  return (snapshot?.evidence ?? []).filter((item) => item.id && ids.has(item.id));
}

function FieldControl({
  field,
  value,
  onChange,
  kind,
  invalid,
}: {
  field: ConfigField;
  value: string;
  onChange: (value: string) => void;
  kind: string;
  invalid: boolean;
}) {
  const options = (field.options ?? []).map((option) => typeof option === 'string'
    ? { value: option, label: option }
    : { value: option.value, label: option.label ?? option.value });
  const inputType = field.type === 'integer' ? 'number' : 'text';
  const label = fieldLabel(field);
  const help = fieldHelp(field, kind);
  const placeholder = field.placeholder
    ?? (field.name === 'path' ? '.'
      : field.name === 'secret_ref' ? 'WAREHOUSE_READONLY'
        : field.type === 'identifier_list' ? 'sales, customers'
          : field.name === 'namespace' ? 'analytics'
            : field.name === 'catalog' ? 'main'
              : undefined);

  return (
    <label className="platform-field">
      <Text size="2" weight="medium">{label}{field.required ? ' *' : ''}</Text>
      {field.type === 'boolean' ? (
        <Select.Root value={value} onValueChange={onChange}>
          <Select.Trigger aria-label={label} aria-invalid={invalid} />
          <Select.Content>
            <Select.Item value="false">{field.name === 'retain_inventory_payloads' ? 'Decline complete payload retention' : 'Off · metadata only'}</Select.Item>
            <Select.Item value="true">{field.name === 'retain_inventory_payloads' ? 'Allow complete payload retention' : 'On · retain bounded source samples'}</Select.Item>
          </Select.Content>
        </Select.Root>
      ) : options.length ? (
        <Select.Root value={value || '__default__'} onValueChange={(next) => onChange(next === '__default__' ? '' : next)}>
          <Select.Trigger aria-label={label} aria-invalid={invalid} />
          <Select.Content>
            {!field.required && <Select.Item value="__default__">All available</Select.Item>}
            {options.map((option) => <Select.Item key={option.value} value={option.value}>{option.label}</Select.Item>)}
          </Select.Content>
        </Select.Root>
      ) : (
        <TextField.Root type={inputType} value={value} onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder} min={field.minimum} max={field.maximum} aria-invalid={invalid}
          aria-label={label} autoComplete="off" inputMode={field.type === 'integer' ? 'numeric' : undefined} />
      )}
      {help && <Text size="1" color="gray">{help}</Text>}
    </label>
  );
}

function PlatformWorkspace({ authStatus, firstRun }: { authStatus: AuthStatus; firstRun: boolean }) {
  const [connectorTypes, setConnectorTypes] = useState<ConnectorType[]>([]);
  const [connections, setConnections] = useState<KnowledgeConnection[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [totalSources, setTotalSources] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [selectedTypeId, setSelectedTypeId] = useState('');
  const [selectedConnectionId, setSelectedConnectionId] = useState('');
  const [connectionName, setConnectionName] = useState('');
  const [fieldValues, setFieldValues] = useState<FieldValues>({});
  const [loading, setLoading] = useState(true);
  const [loadingError, setLoadingError] = useState('');
  const [formError, setFormError] = useState('');
  const [actionMessage, setActionMessage] = useState('');
  const [workingId, setWorkingId] = useState('');
  const [connecting, setConnecting] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [decisionWorkingId, setDecisionWorkingId] = useState('');
  const [batchReviewWorking, setBatchReviewWorking] = useState(false);
  const [expandedProposalId, setExpandedProposalId] = useState('');
  const [mapLocation, setMapLocation] = useState<KnowledgeMapLocation | undefined>();
  const [mapReviewTarget, setMapReviewTarget] = useState<{ connectionId: string; proposalId: string } | null>(null);
  const [reviewLimit, setReviewLimit] = useState(8);
  const [decisionRationale, setDecisionRationale] = useState('');
  const [decisionError, setDecisionError] = useState('');
  const [canSteward, setCanSteward] = useState(false);
  const [publicationPreview, setPublicationPreview] = useState<PublicationPreview | null>(null);
  const [publicationRationale, setPublicationRationale] = useState('');
  const [publicationWorking, setPublicationWorking] = useState(false);
  const [publicationError, setPublicationError] = useState('');
  const [retiringProposal, setRetiringProposal] = useState<CurationRecord | null>(null);
  const [retireReason, setRetireReason] = useState('');
  const [retireWorking, setRetireWorking] = useState(false);
  const [retireError, setRetireError] = useState('');
  const [fieldSubmitted, setFieldSubmitted] = useState(false);
  const [section, setSection] = useState<Section>(window.location.hash.startsWith('#map') ? 'map' : window.location.hash.startsWith('#sources') ? 'sources' : window.location.hash.startsWith('#review') ? 'curation' : 'build');
  const [connectOpen, setConnectOpen] = useState(false);
  const [setupOpen, setSetupOpen] = useState(firstRun);
  const [signingOut, setSigningOut] = useState(false);
  const [sourceFilter, setSourceFilter] = useState('all');
  const hasActiveRefresh = connections.some((connection) => Boolean(connection.refresh_job));
  const activeRefreshIds = connections.filter((connection) => connection.refresh_job).map((connection) => connection.id).sort().join(',');

  const selectedType = connectorTypes.find((item) => item.id === selectedTypeId) ?? null;
  const fields = (selectedType?.config_fields ?? []).filter((field) => !field.discovery_modes ||
    field.discovery_modes.includes(fieldValues.discovery_mode === 'inventory' ? 'inventory' : 'snapshot'));
  const selectedConnection = connections.find((item) => item.id === selectedConnectionId) ?? connections[0] ?? null;
  const selectedSnapshot = selectedConnection?.snapshot ?? null;
  const reviewMutationBusy = Boolean(batchReviewWorking || decisionWorkingId || publicationWorking || retireWorking || workingId || connecting || selectedConnection?.refresh_job);
  const scopedConnections = sourceFilter === 'all'
    ? connections
    : connections.filter((connection) => {
      if (sourceFilter === 'review') return prettyStatus(connection).label === 'Review needed' || prettyStatus(connection).label === 'Partly discovered';
      if (sourceFilter === 'attention') return prettyStatus(connection).tone === 'attention';
      if (sourceFilter === 'unsynced') return !lastSync(connection);
      if (sourceFilter === 'agent-ready') return (connection.stale_assertion_count ?? 0) === 0 && (connection.agent_ready === true || connection.snapshot?.agent_ready === true);
      return false;
    });
  const proposals = reviewableProposals(selectedSnapshot);
  const staleAssertionCount = selectedConnection?.stale_assertion_count ?? 0;
  const staleContracts = staleApprovedContractProposals(selectedSnapshot);
  const fieldsWithValues = useMemo(() => convertConfig(fields, fieldValues), [fields, fieldValues]);
  const invalidFields = fieldSubmitted ? fields.filter((field) => field.required && !String(fieldValues[field.name] ?? '').trim()).map((field) => field.name) : [];
  const advancedFields = fields.filter((field) => ADVANCED_FIELDS.has(field.name));
  const primaryFields = fields.filter((field) => !ADVANCED_FIELDS.has(field.name));

  useEffect(() => {
    document.title = 'Knowledge layer';
  }, []);

  useEffect(() => {
    setReviewLimit(8);
    setExpandedProposalId('');
    setDecisionRationale('');
    setRetiringProposal(null);
    setRetireReason('');
    setRetireError('');
  }, [selectedConnectionId]);

  useEffect(() => {
    if (!mapReviewTarget || selectedConnectionId !== mapReviewTarget.connectionId || !selectedSnapshot || section !== 'curation') return;
    const items = reviewableProposals(selectedSnapshot);
    const index = items.findIndex(item => item.id === mapReviewTarget.proposalId);
    if (index >= 0) {
      setReviewLimit(Math.max(8, index + 1));
      setExpandedProposalId(mapReviewTarget.proposalId);
      setDecisionRationale('');
      window.requestAnimationFrame(() => document.getElementById(`proposal-review-${mapReviewTarget.proposalId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
    }
    setMapReviewTarget(null);
  }, [mapReviewTarget, selectedConnectionId, selectedSnapshot, section]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      const [typesResult, connectionsResult, roleResult] = await Promise.allSettled([
        getConnectorTypes(), getConnectionPage(), getStewardRole(),
      ]);
      if (cancelled) return;
      if (typesResult.status === 'fulfilled') {
        setConnectorTypes(typesResult.value);
        setSelectedTypeId((current) => current || typesResult.value[0]?.id || '');
      }
      if (connectionsResult.status === 'fulfilled') {
        setConnections(connectionsResult.value.connections);
        setNextCursor(connectionsResult.value.next_cursor);
        setTotalSources(connectionsResult.value.total);
        setSelectedConnectionId((current) => current || connectionsResult.value.connections[0]?.id || '');
        if (connectionsResult.value.total === 0) {
          setSection('sources');
          setSetupOpen(true);
        }
      }
      if (roleResult.status === 'fulfilled') setCanSteward(roleResult.value);
      const errors = [typesResult, connectionsResult]
        .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
        .map((result) => result.reason instanceof Error ? result.reason.message : 'The service returned an unexpected response.');
      setLoadingError([...new Set(errors)].join(' '));
      setLoading(false);
    }
    void load();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!hasActiveRefresh) return;
    let cancelled = false;
    let polling = false;
    const activeIds = activeRefreshIds.split(',').filter(Boolean);
    const timer = window.setInterval(() => {
      if (polling) return;
      polling = true;
      void Promise.allSettled(activeIds.map((id) => getConnection(id))).then((results) => {
        if (cancelled) return;
        const latest = new Map(results.flatMap((result) => result.status === 'fulfilled'
          ? [[result.value.connection.id, result.value] as const] : []));
        const completed = connections.find((row) => row.refresh_job && latest.has(row.id)
          && !latest.get(row.id)?.connection.refresh_job);
        if (completed) setActionMessage(latest.get(completed.id)?.connection.status === 'error'
          ? `${completed.name} needs attention after discovery.`
          : `${completed.name} discovery finished. Review its knowledge layer.`);
        setConnections((current) => current.map((row) => {
          const detail = latest.get(row.id);
          return detail ? { ...detail.connection, snapshot: detail.snapshot } : row;
        }));
      }).catch(() => {
        // Keep the last known status; the next poll can recover.
      }).finally(() => { polling = false; });
    }, 3000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [activeRefreshIds, hasActiveRefresh]);

  async function handleLoadMore() {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await getConnectionPage(nextCursor);
      setConnections((current) => {
        const known = new Set(current.map((item) => item.id));
        return [...current, ...page.connections.filter((item) => !known.has(item.id))];
      });
      setNextCursor(page.next_cursor);
      setTotalSources(page.total);
    } catch (error) {
      setLoadingError(messageOf(error));
    } finally {
      setLoadingMore(false);
    }
  }

  useEffect(() => {
    if (!selectedConnectionId) return;
    const cached = connections.find((item) => item.id === selectedConnectionId);
    if (!cached || cached.snapshot !== undefined) return;
    let cancelled = false;
    setDetailLoading(true);
    void getConnection(selectedConnectionId).then((detail) => {
      if (!cancelled) updateConnection({ ...detail.connection, snapshot: detail.snapshot });
    }).catch((error) => {
      if (!cancelled) setDecisionError(`Could not load the selected source details. ${messageOf(error)}`);
    }).finally(() => {
      if (!cancelled) setDetailLoading(false);
    });
    return () => { cancelled = true; };
  }, [selectedConnectionId, connections]);

  useEffect(() => {
    if (!selectedConnectionId || !selectedSnapshot?.id) {
      setPublicationPreview(null);
      return;
    }
    setPublicationPreview(null);
    let cancelled = false;
    void getPublicationPreview(selectedConnectionId).then((preview) => {
      if (!cancelled) setPublicationPreview(preview);
    }).catch(() => {
      if (!cancelled) setPublicationPreview(null);
    });
    return () => { cancelled = true; };
  }, [selectedConnectionId, selectedSnapshot?.id, selectedSnapshot?.version, selectedSnapshot?.agent_ready]);

  useEffect(() => {
    if (!selectedType) return;
    const defaults = initialValues(selectedType.config_fields ?? []);
    setFieldValues(defaults);
    setFieldSubmitted(false);
    setFormError('');
    setConnectionName(suggestedConnectionName(selectedType, defaults));
  }, [selectedTypeId, connectorTypes]);

  function openConnectionDialog() {
    const type = selectedType ?? connectorTypes[0];
    const defaults = initialValues(type?.config_fields ?? []);
    setSelectedTypeId(type?.id ?? '');
    setFieldValues(defaults);
    setConnectionName(type ? suggestedConnectionName(type, defaults) : '');
    setFieldSubmitted(false);
    setFormError('');
    setConnectOpen(true);
  }

  function updateConnection(connection: KnowledgeConnection) {
    setConnections((current) => [connection, ...current.filter((item) => item.id !== connection.id)]);
    setSelectedConnectionId(connection.id);
  }

  function goTo(next: Section) {
    setSetupOpen(false);
    window.history.replaceState(null, '', next === 'curation' ? '#review' : `#${next}`);
    setSection(next);
    window.scrollTo({ top: 0, behavior: 'instant' });
  }

  async function reviewBuiltSource(id: string) {
    try {
      const detail = await getConnection(id);
      updateConnection({ ...detail.connection, snapshot: detail.snapshot });
      goTo('curation');
    } catch (error) {
      setDecisionError(messageOf(error));
    }
  }

  async function reviewMapSource(id: string, proposalId?: string, location?: KnowledgeMapLocation) {
    setMapLocation(location);
    setMapReviewTarget(proposalId ? { connectionId: id, proposalId } : null);
    if (connections.some((connection) => connection.id === id)) {
      setSelectedConnectionId(id);
      goTo('curation');
      return;
    }
    try {
      const detail = await getConnection(id);
      updateConnection({ ...detail.connection, snapshot: detail.snapshot });
      goTo('curation');
    } catch (reason: unknown) {
      setLoadingError(reason instanceof Error ? reason.message : 'This source could not be opened for review.');
    }
  }

  async function handleConnect(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFieldSubmitted(true);
    setFormError('');
    setActionMessage('');
    if (!selectedType) {
      setFormError('No source types are available. Reload the page after the service is ready.');
      return;
    }
    if (!connectionName.trim()) {
      setFormError('Add a name for this connection.');
      return;
    }
    if (fields.some((field) => field.required && !String(fieldValues[field.name] ?? '').trim())) {
      setFormError('Fill in the required source details before connecting.');
      return;
    }

    setConnecting(true);
    try {
      const created = await createConnection({ name: connectionName.trim(), kind: selectedType.id, config: fieldsWithValues });
      updateConnection(created);
      setTotalSources((current) => current + 1);
      setActionMessage('Connection saved. Discovering available data…');
      try {
        const refreshed = await refreshConnection(created.id);
        if ('job' in refreshed) {
          updateConnection({ ...created, refresh_job: refreshed.job });
          setActionMessage('Connection saved. Discovery requested.');
        } else {
          updateConnection({ ...refreshed.connection, snapshot: refreshed.snapshot });
          setActionMessage(refreshed.snapshot?.status === 'partial'
            ? 'Initial discovery finished with items that need attention.'
            : 'Connection saved and initial discovery finished.');
        }
      } catch (refreshError) {
        setActionMessage('');
        let latest = created;
        try {
          const detail = await getConnection(created.id);
          latest = { ...detail.connection, snapshot: detail.snapshot };
        } catch {
          // Keep the confirmed create response if the status refresh is unavailable.
        }
        updateConnection(latest);
        setFormError(`The connection was saved, but initial discovery did not finish. ${messageOf(refreshError)}`);
      }
      setConnectOpen(false);
      goTo('build');
    } catch (error) {
      setFormError(messageOf(error));
    } finally {
      setConnecting(false);
    }
  }

  async function handleRefresh(connection: KnowledgeConnection) {
    if (reviewMutationBusy) return;
    setWorkingId(connection.id);
    setFormError('');
    setActionMessage('');
    try {
      const result = await refreshConnection(connection.id);
      if ('job' in result) {
        updateConnection({ ...connection, refresh_job: result.job });
        setActionMessage(`${connection.name} discovery is running in the background.`);
      } else {
        updateConnection({ ...result.connection, snapshot: result.snapshot });
        setActionMessage(result.snapshot?.status === 'partial'
          ? `${connection.name} synced with items that need attention.`
          : `${connection.name} is up to date.`);
      }
    } catch (error) {
      setFormError(`${connection.name}: ${messageOf(error)}`);
      try {
        const detail = await getConnection(connection.id);
        updateConnection({ ...detail.connection, snapshot: detail.snapshot });
      } catch {
        // Preserve the last confirmed view when the detail request also fails.
      }
    } finally {
      setWorkingId('');
    }
  }

  async function handleDecision(proposal: CurationRecord, decision: 'approve' | 'reject') {
    if (!selectedConnection || !proposal.id || reviewMutationBusy) return;
    if (decision === 'approve' && (!proposal.evidence_ids?.length || !evidenceFor(proposal, selectedSnapshot).length)) {
      setDecisionError('This suggestion cannot be approved until its supporting evidence is available.');
      return;
    }
    setDecisionWorkingId(proposal.id);
    setDecisionError('');
    try {
      await decideProposal(selectedConnection.id, proposal.id, decision, decisionRationale);
      const detail = await getConnection(selectedConnection.id);
      updateConnection({ ...detail.connection, snapshot: detail.snapshot });
      setDecisionRationale('');
      setExpandedProposalId('');
      setActionMessage(decision === 'approve'
        ? 'Suggestion approved and recorded for this workspace.'
        : 'Suggestion rejected and recorded for this workspace.');
    } catch (error) {
      setDecisionError(messageOf(error));
    } finally {
      setDecisionWorkingId('');
    }
  }

  async function handleRetire() {
    if (!canSteward || !selectedConnection || !retiringProposal?.id || retireWorking || retireReason.trim().length < 8) return;
    const connectionId = selectedConnection.id;
    setRetireWorking(true);
    setRetireError('');
    try {
      await archiveProposals(connectionId, [retiringProposal.id], retireReason.trim());
      const detail = await getConnection(connectionId);
      updateConnection({ ...detail.connection, snapshot: detail.snapshot });
      setRetiringProposal(null);
      setRetireReason('');
      setActionMessage('Outdated claim retired. Its prior review remains in the audit history. Build a new draft from current evidence.');
    } catch (error) {
      setRetireError(messageOf(error));
    } finally {
      setRetireWorking(false);
    }
  }

  async function handlePublication(withdraw: boolean) {
    if (!selectedConnection || !selectedSnapshot?.id || !selectedSnapshot.version || reviewMutationBusy) return;
    setPublicationWorking(true);
    setPublicationError('');
    try {
      if (withdraw) {
        await withdrawPublication(selectedConnection.id, publicationRationale.trim());
      } else {
        await publishSource(selectedConnection.id, selectedSnapshot.id, selectedSnapshot.version,
                            publicationRationale.trim());
      }
      const detail = await getConnection(selectedConnection.id);
      updateConnection({ ...detail.connection, snapshot: detail.snapshot });
      setPublicationRationale('');
      setActionMessage(withdraw ? 'Agent publication withdrawn.' : 'Reviewed source published for agent use.');
    } catch (error) {
      setPublicationError(messageOf(error));
    } finally {
      setPublicationWorking(false);
    }
  }

  async function reloadAcceptedBatch(connectionId: string, result: ReviewBatchResult) {
    const acceptedCount = result.reviewed_proposal_ids.length;
    const acceptedLabel = `${acceptedCount} ${acceptedCount === 1 ? 'draft' : 'drafts'} accepted`;
    const remainingLabel = result.remaining_pending_count === 0
      ? 'No drafts remain for review.'
      : `${result.remaining_pending_count} ${result.remaining_pending_count === 1 ? 'draft' : 'drafts'} still need review.`;
    setActionMessage(result.status === 'published'
      ? `${acceptedLabel} and source published for agents. ${remainingLabel}`
      : result.status === 'accepted_publication_blocked'
        ? `${acceptedLabel}. Publication needs attention: ${result.publication_error || result.publication_blockers?.map((blocker) => blocker.replaceAll('_', ' ')).join(', ') || 'the source is not eligible yet'}. ${remainingLabel}`
        : `${acceptedLabel}. ${remainingLabel}`);
    try {
      const detail = await getConnection(connectionId);
      const connection = { ...detail.connection, snapshot: detail.snapshot };
      setConnections((current) => [connection, ...current.filter((item) => item.id !== connection.id)]);
    } catch (error) {
      // Retire the old view after a committed batch; detail loading can retry it.
      setConnections((current) => current.map((connection) => connection.id === connectionId
        ? { ...connection, snapshot: undefined } : connection));
      throw error;
    }
    setExpandedProposalId('');
    setDecisionRationale('');
  }

  const fieldControl = (field: ConfigField) => (
    <FieldControl
      key={field.name}
      field={field}
      kind={selectedType?.id ?? ''}
      value={fieldValues[field.name] ?? ''}
      onChange={(value) => {
        const next = { ...fieldValues, [field.name]: value };
        setFieldValues(next);
        if (field.name === 'path' && selectedType
          && (!connectionName.trim() || connectionName === selectedType.name
            || connectionName === suggestedConnectionName(selectedType, fieldValues))) {
          setConnectionName(suggestedConnectionName(selectedType, next));
        }
      }}
      invalid={invalidFields.includes(field.name)}
    />
  );

  async function handleSignOut() {
    if (signingOut || reviewMutationBusy) return;
    setSigningOut(true);
    try { await signOut(); }
    catch (reason) { setLoadingError(messageOf(reason)); setSigningOut(false); }
  }

  return (
    <div className="platform-app">
      <header className="app-header">
        <button type="button" className="app-brand" onClick={() => goTo('build')} aria-label="Knowledge layer home">
          <Layers3 size={20} /><span>knowledge layer</span>
        </button>
        <nav className="app-navigation" aria-label="Main navigation">
          {([['build', 'Build'], ['curation', 'Review'], ['sources', 'Sources']] as const).map(([key, label]) => (
            <Button key={key} variant={!setupOpen && section === key ? 'soft' : 'ghost'} color={!setupOpen && section === key ? 'green' : 'gray'}
              aria-current={!setupOpen && section === key ? 'page' : undefined} onClick={() => goTo(key)}>{label}</Button>
          ))}
          <Button variant={setupOpen ? 'soft' : 'ghost'} color="gray" size="2" aria-current={setupOpen ? 'page' : undefined} onClick={() => setSetupOpen(true)}>Setup</Button>
          <DropdownMenu.Root>
            <DropdownMenu.Trigger><IconButton variant="ghost" color="gray" aria-label="Inspect knowledge"><ChevronDown size={17} /></IconButton></DropdownMenu.Trigger>
            <DropdownMenu.Content>
              <DropdownMenu.Item onSelect={() => goTo('map')}>Knowledge map</DropdownMenu.Item>
              {authStatus.mode !== 'demo' && <><DropdownMenu.Separator /><DropdownMenu.Item disabled={signingOut || reviewMutationBusy} onSelect={() => void handleSignOut()}>Sign out</DropdownMenu.Item></>}
            </DropdownMenu.Content>
          </DropdownMenu.Root>
        </nav>
      </header>

      <main className={`platform-content${!setupOpen && section === 'map' ? ' inspection-content' : ''}`}>
        {loadingError && <Callout.Root color="red" role="alert"><Callout.Text>{loadingError}</Callout.Text><Button variant="ghost" color="red" onClick={() => window.location.reload()}>Retry</Button></Callout.Root>}
        {formError && !connectOpen && <Callout.Root color="red" role="alert"><Callout.Text>{formError}</Callout.Text><Button variant="ghost" color="red" onClick={() => setFormError('')}>Dismiss</Button></Callout.Root>}
        {decisionError && <Callout.Root color="red" role="alert"><Callout.Text>{decisionError}</Callout.Text><Button variant="ghost" color="red" onClick={() => setDecisionError('')}>Dismiss</Button></Callout.Root>}
        {actionMessage && <Callout.Root color="green" role="status"><Callout.Text>{actionMessage}</Callout.Text><Button variant="ghost" color="green" onClick={() => setActionMessage('')}>Dismiss</Button></Callout.Root>}

        {loading ? <div className="loading-row"><LoaderCircle size={18} className="spin" />Loading sources…</div>
          : setupOpen ? <SetupGuide sourceCount={totalSources} onConnect={() => { goTo('sources'); openConnectionDialog(); }} onBuild={() => goTo('build')} onDone={() => setSetupOpen(false)} />
          : section === 'map' ? <Suspense fallback={<p>Loading knowledge map…</p>}><KnowledgeMap connections={connections} totalSources={totalSources} onConnect={() => goTo('sources')} initialLocation={mapLocation} onReviewSource={(id, proposalId, location) => void reviewMapSource(id, proposalId, location)} /></Suspense>
          : section === 'build' ? <BuildProgress connections={connections} selectedConnectionId={selectedConnection?.id ?? ''} totalSources={totalSources}
              disabled={Boolean(batchReviewWorking || decisionWorkingId || publicationWorking || workingId || connecting)} onSelectConnection={setSelectedConnectionId} onConnect={() => { goTo('sources'); openConnectionDialog(); }} onReview={reviewBuiltSource} />
          : section === 'sources' ? (
            <section aria-labelledby="sources-title">
              <div className="page-heading"><Heading as="h1" size="7" id="sources-title">Sources</Heading><Button onClick={openConnectionDialog}><Plus size={16} />Connect source</Button></div>
              {connections.length ? <div className="source-list-heading">
                <Text size="2" color="gray">{connections.length === totalSources ? `${totalSources} connected` : `${connections.length} of ${totalSources} loaded`}</Text>
                <Select.Root value={sourceFilter} onValueChange={setSourceFilter}><Select.Trigger aria-label="Filter sources" variant="ghost" /><Select.Content>
                  <Select.Item value="all">All sources</Select.Item><Select.Item value="agent-ready">Published knowledge</Select.Item>
                  <Select.Item value="review">Review needed</Select.Item><Select.Item value="attention">Needs attention</Select.Item><Select.Item value="unsynced">Not synced</Select.Item>
                </Select.Content></Select.Root>
              </div> : <div className="empty-state"><Heading size="4">Connect your first source</Heading><Text as="p" size="2" color="gray">Choose the data you want your agents to understand.</Text></div>}
              <div className="source-list">
                {scopedConnections.map((connection) => {
                  const status = prettyStatus(connection);
                  return <div className="source-row" key={connection.id}>
                    <div className="source-copy"><Button variant="ghost" color="gray" className="source-title" onClick={() => { setSelectedConnectionId(connection.id); goTo('build'); }}>{connection.name}</Button>
                      <Text as="p" size="1" color="gray">{prettyKind(connection.kind, connectorTypes)}</Text></div>
                    <Text className="source-status" size="2" color={status.tone === 'attention' ? 'orange' : status.tone === 'good' ? 'green' : 'gray'}>{status.label}</Text>
                    <DropdownMenu.Root><DropdownMenu.Trigger><IconButton variant="ghost" color="gray" aria-label={`Actions for ${connection.name}`}><ChevronDown size={16} /></IconButton></DropdownMenu.Trigger><DropdownMenu.Content>
                      <DropdownMenu.Item onSelect={() => { setSelectedConnectionId(connection.id); goTo('build'); }}>Build knowledge</DropdownMenu.Item>
                      <DropdownMenu.Item onSelect={() => void reviewBuiltSource(connection.id)}>Review drafts</DropdownMenu.Item>
                      <DropdownMenu.Item disabled={reviewMutationBusy || Boolean(connection.refresh_job)} onSelect={() => void handleRefresh(connection)}>Refresh source</DropdownMenu.Item>
                      <DropdownMenu.Separator /><DropdownMenu.Label>Last sync: {formatDate(lastSync(connection))}</DropdownMenu.Label>
                    </DropdownMenu.Content></DropdownMenu.Root>
                  </div>;
                })}
              </div>
              {connections.length > 0 && scopedConnections.length === 0 && <p className="muted">{nextCursor ? 'No matching sources loaded. Load more to keep looking.' : 'No sources match this filter.'}</p>}
              {nextCursor && <Button variant="soft" color="gray" loading={loadingMore} onClick={() => void handleLoadMore()}>Load more</Button>}
            </section>
          ) : (
            <section aria-labelledby="review-title">
              <div className="page-heading"><Heading as="h1" size="7" id="review-title">Review</Heading></div>
              {selectedConnection ? <>
                <label className="review-source"><Text size="2" color="gray">Source</Text><Select.Root value={selectedConnection.id} onValueChange={setSelectedConnectionId}><Select.Trigger aria-label="Source to review" /><Select.Content>{connections.map((source) => <Select.Item key={source.id} value={source.id}>{source.name}</Select.Item>)}</Select.Content></Select.Root></label>
                {selectedConnection.agent_ready || selectedSnapshot?.agent_ready ? <Text as="p" size="2" color="green">Accepted claims in this publication are available to your agents.{proposals.length > 0 ? ' Other drafts still need review.' : ''}</Text> : null}
                {staleAssertionCount > 0 && <Callout.Root color="orange" role="status"><Callout.Text>{staleAssertionCount} previously approved {staleAssertionCount === 1 ? 'claim is' : 'claims are'} inactive because its evidence changed. Retire the outdated approval, then build and review a replacement using current evidence.</Callout.Text></Callout.Root>}
                {staleContracts.length > 0 && <div className="review-stale-list" aria-label="Outdated approved claims">
                  {staleContracts.map((proposal) => <div className="review-stale-item" key={proposal.id}>
                    <div><Text as="p" size="2" weight="medium">{recordTitle(proposal, selectedSnapshot)}</Text><Text as="p" size="2" color="gray">{recordDescription(proposal)}</Text></div>
                    {canSteward && proposal.id && <Button variant="soft" color="orange" size="2" onClick={() => { setRetiringProposal(proposal); setRetireReason(''); setRetireError(''); }} disabled={reviewMutationBusy}>Retire outdated claim</Button>}
                  </div>)}
                </div>}
                <ReviewBatch connection={selectedConnection} snapshot={selectedSnapshot} proposals={proposals}
                  canSteward={canSteward} disabled={reviewMutationBusy || detailLoading} onBusyChange={setBatchReviewWorking} onAccepted={reloadAcceptedBatch}>
                  {({ selectedIds, selectionDisabled, toggleSelection }) => detailLoading ? <div className="loading-row"><LoaderCircle size={16} className="spin" />Loading drafts…</div> : proposals.length ? <div className="review-list">
                    {proposals.slice(0, reviewLimit).map((proposal, index) => <article className="review-item" id={proposal.id ? `proposal-review-${proposal.id}` : undefined} key={proposal.id ?? index}>
                      <Checkbox aria-label={`Select ${recordTitle(proposal, selectedSnapshot)} for review`} checked={Boolean(proposal.id && selectedIds.has(proposal.id))}
                        onCheckedChange={() => { if (proposal.id) toggleSelection(proposal.id); }} disabled={selectionDisabled || !proposal.id || (selectedIds.size >= 100 && !selectedIds.has(proposal.id))} />
                      <div className="review-copy"><Text as="div" weight="medium">{recordTitle(proposal, selectedSnapshot)}</Text>
                        {proposal.asset_id && <Text as="div" size="1" color="gray">{String(selectedSnapshot?.assets?.find((asset) => asset.id === proposal.asset_id)?.name ?? proposal.asset_id)}</Text>}
                        <Text as="p" size="2">{recordDescription(proposal)}</Text>
                        <Button variant="ghost" color="gray" size="1" onClick={() => { setExpandedProposalId(expandedProposalId === proposal.id ? '' : proposal.id ?? ''); setDecisionRationale(''); }} aria-expanded={expandedProposalId === proposal.id}>Evidence and details<ChevronDown size={13} /></Button>
                        {expandedProposalId === proposal.id && <div className="proposal-details">
                          {proposal.payload && <details><summary>Full definition</summary><pre>{JSON.stringify(proposal.payload, null, 2)}</pre></details>}
                          {evidenceFor(proposal, selectedSnapshot).map((evidence, evidenceIndex) => <div className="evidence-record" key={evidence.id ?? evidenceIndex}><Text as="p" size="2">{String(evidence.summary ?? 'Source evidence')}</Text>
                            {typeof evidence.source_pointer === 'string' && <Text as="p" size="1" color="gray">{evidence.source_pointer}</Text>}
                            {typeof evidence.trust_state === 'string' && <Text as="p" size="1" color="gray">{evidence.trust_state.replaceAll('_', ' ')}</Text>}
                            <EvidenceHighlights evidence={evidence} />
                          </div>)}
                          {evidenceFor(proposal, selectedSnapshot).length === 0 && <Text color="orange" size="2">Supporting evidence is unavailable. Review cannot confirm this claim.</Text>}
                          {proposal.evidence_ids?.length && evidenceFor(proposal, selectedSnapshot).length < proposal.evidence_ids.length ? <Text color="orange" size="2">Some referenced evidence is missing from this snapshot.</Text> : null}
                          {proposal.evidence_refs?.length ? <details><summary>Source citations</summary>{proposal.evidence_refs.map((ref, refIndex) => <div className="evidence-record" key={refIndex}><Text as="div" size="2">{connections.find((source) => source.id === ref.connection_id)?.name ?? ref.connection_id}</Text><Text as="p" size="1" color="gray">Snapshot {ref.snapshot_id} · Evidence {ref.evidence_id}</Text></div>)}</details> : null}
                          <label className="platform-field"><Text size="2">Decision note (optional)</Text><TextArea value={decisionRationale} onChange={(event) => setDecisionRationale(event.target.value)} maxLength={1000} aria-label="Decision note" /></label>
                          <Button variant="soft" color="red" size="1" loading={decisionWorkingId === proposal.id} disabled={!proposal.id || reviewMutationBusy} onClick={() => void handleDecision(proposal, 'reject')}>Reject draft</Button>
                        </div>}
                      </div>
                    </article>)}
                    {reviewLimit < proposals.length && <Button variant="soft" color="gray" onClick={() => setReviewLimit((limit) => limit + 8)}>Show more drafts</Button>}
                  </div> : <div className="empty-state"><Heading size="4">No drafts to review</Heading><Text as="p" size="2" color="gray">{staleAssertionCount > 0 ? 'Retire the outdated approval above, then build a replacement from the current source.' : 'New agent drafts will appear here.'}</Text><Button variant="soft" onClick={() => goTo('build')}>Build knowledge</Button></div>}
                </ReviewBatch>
                {canSteward && selectedSnapshot?.id && <details className="publication-details"><summary>{selectedSnapshot.publication ? 'Manage publication' : 'Publish previously reviewed knowledge'}</summary>
                  <Text as="p" size="2" color="gray">{selectedSnapshot.publication ? selectedSnapshot.publication.active ? 'This source is published. Candidate claims remain drafts.' : 'Publication is suspended; agents cannot use it as ready.' : 'Publish the current reviewed snapshot for your agents. Remaining claims stay labeled as drafts.'}</Text>
                  {!selectedSnapshot.publication && publicationPreview?.blockers?.length ? <Text as="p" color="orange" size="2">Before publishing: {publicationPreview.blockers.map((item) => item.replaceAll('_', ' ')).join(', ')}.</Text> : null}
                  <label className="platform-field"><Text size="2">{selectedSnapshot.publication ? 'Withdrawal reason' : 'Publication reason'}</Text><TextArea value={publicationRationale} onChange={(event) => setPublicationRationale(event.target.value)} maxLength={1000} aria-label={selectedSnapshot.publication ? 'Withdrawal reason' : 'Publication reason'} /></label>
                  {publicationError && <Text as="p" role="alert" color="red" size="2">{publicationError}</Text>}
                  <Button color={selectedSnapshot.publication ? 'red' : 'green'} variant="soft" loading={publicationWorking}
                    disabled={reviewMutationBusy || publicationRationale.trim().length < (selectedSnapshot.publication ? 8 : 16) || (!selectedSnapshot.publication && !publicationPreview?.eligible)}
                    onClick={() => void handlePublication(Boolean(selectedSnapshot.publication))}>{selectedSnapshot.publication ? 'Withdraw publication' : 'Publish for agents'}</Button>
                </details>}
              </> : <div className="empty-state"><Text as="p" color="gray">Connect a source to start.</Text><Button onClick={() => { goTo('sources'); openConnectionDialog(); }}>Connect source</Button></div>}
            </section>
          )}
      </main>

      <Dialog.Root open={Boolean(retiringProposal)} onOpenChange={(open) => { if (!open && !retireWorking) { setRetiringProposal(null); setRetireReason(''); setRetireError(''); } }}>
        <Dialog.Content maxWidth="480px"><Dialog.Title>Retire outdated claim</Dialog.Title>
          <Dialog.Description size="2">This old approval is inactive. Retiring it clears the stale publication blocker and preserves its review in the audit history. A new claim still needs current evidence and your approval.</Dialog.Description>
          {retiringProposal && <Text as="p" size="2" style={{ marginTop: 12 }}>{recordDescription(retiringProposal)}</Text>}
          <label className="platform-field"><Text size="2">Reason</Text><TextArea value={retireReason} onChange={(event) => setRetireReason(event.target.value)} maxLength={1000} aria-label="Reason for retiring outdated claim" placeholder="The source policy changed." /></label>
          {retireError && <Text as="p" size="2" color="red" role="alert">{retireError}</Text>}
          <Flex gap="3" justify="end" mt="4"><Button variant="soft" color="gray" onClick={() => setRetiringProposal(null)} disabled={retireWorking}>Cancel</Button><Button color="orange" onClick={() => void handleRetire()} disabled={retireReason.trim().length < 8 || retireWorking} loading={retireWorking}>Retire claim</Button></Flex>
        </Dialog.Content>
      </Dialog.Root>

      <Dialog.Root open={connectOpen} onOpenChange={(open) => { if (!connecting) { if (open) openConnectionDialog(); else { setConnectOpen(false); setFormError(''); } } }}>
        <Dialog.Content maxWidth="580px"><Dialog.Title>Connect a source</Dialog.Title><Dialog.Description size="2">Choose the data your agents can inspect.</Dialog.Description>
          {formError && <Callout.Root color="red" role="alert" mt="4"><Callout.Text>{formError}</Callout.Text></Callout.Root>}
          {connectorTypes.length ? <form className="connect-form" onSubmit={handleConnect} noValidate>
            {authStatus.mode === 'local' && <Text as="p" size="2" color="gray">Your connected sources are available to your installation’s agent. Only you can accept and publish its drafts.</Text>}
            <label className="platform-field"><Text size="2" weight="medium">Source type</Text><Select.Root value={selectedTypeId} onValueChange={setSelectedTypeId}><Select.Trigger aria-label="Source type" /><Select.Content>{connectorTypes.map((type) => <Select.Item key={type.id} value={type.id}>{type.name}</Select.Item>)}</Select.Content></Select.Root></label>
            <label className="platform-field"><Text size="2" weight="medium">Name</Text><TextField.Root value={connectionName} onChange={(event) => setConnectionName(event.target.value)} placeholder="Name this source" maxLength={120} aria-label="Connection name" /></label>
            {primaryFields.map(fieldControl)}
            {advancedFields.length > 0 && <details><summary>Advanced settings</summary><div className="connect-advanced">{advancedFields.map(fieldControl)}</div></details>}
            <Flex gap="3" justify="end"><Dialog.Close><Button type="button" variant="soft" color="gray" disabled={connecting}>Cancel</Button></Dialog.Close><Button type="submit" loading={connecting}>Connect source</Button></Flex>
          </form> : <Text as="p" color="gray">No source types are available.</Text>}
        </Dialog.Content>
      </Dialog.Root>
    </div>
  );
}

function messageOf(error: unknown): string {
  if (error instanceof PlatformApiError) return error.message;
  if (error instanceof Error) return error.message;
  return 'The request could not be completed. Try again.';
}

function PlatformApp() {
  return <SetupAuthGate>{({ status, firstRun }) => <PlatformWorkspace authStatus={status} firstRun={firstRun} />}</SetupAuthGate>;
}

export default PlatformApp;
