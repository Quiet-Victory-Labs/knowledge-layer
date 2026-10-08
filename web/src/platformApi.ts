export type ConfigField = {
  name: string;
  type?: string;
  required?: boolean;
  default?: unknown;
  label?: string;
  description?: string;
  placeholder?: string;
  minimum?: number;
  maximum?: number;
  options?: Array<string | { label?: string; value: string }>;
  discovery_modes?: Array<'snapshot' | 'inventory'>;
};

export type ConnectorType = {
  id: string;
  name: string;
  description?: string;
  config_fields: ConfigField[];
};

export type CurationRecord = {
  id?: string;
  name?: string;
  title?: string;
  label?: string;
  type?: string;
  proposal_type?: string;
  assertion_kind?: string;
  asset_id?: string;
  proposed_definition?: string;
  payload?: Record<string, unknown>;
  target_concept_id?: string;
  candidate_id?: string;
  description?: string;
  status?: string;
  summary?: string;
  rationale?: string;
  evidence_ids?: string[];
  evidence_refs?: Array<{ connection_id: string; snapshot_id: string; evidence_id: string }>;
  subject_id?: string;
  predicate?: string;
  object_id?: string;
  confidence?: number;
  [key: string]: unknown;
};

export type KnowledgeSnapshot = {
  id?: string;
  snapshot_id?: string;
  version?: number;
  created_at?: string;
  status?: 'ready' | 'partial' | 'error' | string;
  discovery_complete?: boolean;
  meaning_certified?: boolean;
  agent_ready?: boolean;
  publication?: { snapshot_id: string; snapshot_version: number; published_by: string; published_at: string; rationale: string; active: boolean };
  curation_status?: string;
  source?: Record<string, unknown>;
  assets?: CurationRecord[];
  concepts?: CurationRecord[];
  relationships?: CurationRecord[];
  proposals?: CurationRecord[];
  metrics?: CurationRecord[];
  evidence?: CurationRecord[];
  errors?: Array<string | CurationRecord>;
  drift?: Record<string, unknown>;
  capabilities?: Record<string, unknown>;
};

export type KnowledgeConnection = {
  id: string;
  tenant_id?: string;
  name: string;
  kind: string;
  status: string;
  created_at: string;
  updated_at?: string;
  last_refreshed_at?: string | null;
  last_synced_at?: string | null;
  last_error?: string | null;
  error?: string | null;
  asset_count?: number | null;
  concept_count?: number | null;
  relationship_count?: number | null;
  proposal_count?: number | null;
  needs_attention_count?: number;
  stale_assertion_count?: number;
  stage?: string;
  agent_ready?: boolean;
  discovery_complete?: boolean | null;
  discovery_mode?: 'snapshot' | 'inventory';
  curation_status?: string;
  coverage?: Record<string, number | null>;
  counts?: Record<string, number | null>;
  config?: Record<string, unknown>;
  snapshot?: KnowledgeSnapshot | null;
  refresh_job?: RefreshJob | null;
};

export type RefreshJob = {
  id: string;
  connection_id: string;
  status: 'queued' | 'running' | 'retry_wait' | 'succeeded' | 'failed' | 'cancelled';
  attempt: number;
  max_attempts: number;
  requested_at: string;
  result_snapshot_id?: string | null;
  result_inventory_id?: string | null;
  error_code?: string | null;
};

export type PublicationPreview = {
  connection_id: string;
  snapshot_id?: string | null;
  snapshot_version?: number | null;
  eligible: boolean;
  blockers: string[];
  reviewed_assertions: number;
  pending_proposals: number;
  publication?: KnowledgeSnapshot['publication'] | null;
};

export type ReviewBatchSelection = {
  proposal_ids: string[];
  expected_snapshot_id: string;
  expected_snapshot_version: number;
};

export type ReviewBatchPreview = {
  connection_id: string;
  snapshot_id: string;
  snapshot_version: number;
  review_digest: string;
  proposals: CurationRecord[];
  evidence: CurationRecord[];
  remaining_pending_count: number;
};

export type ReviewBatchResult = {
  status: 'accepted' | 'published' | 'accepted_publication_blocked';
  reviewed_proposal_ids: string[];
  snapshot_id: string;
  snapshot_version: number;
  remaining_pending_count: number;
  publication?: Record<string, unknown>;
  publication_blockers?: string[];
  publication_error?: string;
};

export type KnowledgeBuildStatus = 'queued' | 'waiting_for_discovery' | 'running' | 'awaiting_review' | 'ready' | 'needs_attention' | 'cancelled' | 'budget_exhausted';

export type KnowledgeBuildCounts = {
  total: number;
  pending: number;
  leased: number;
  proposed: number;
  abstained: number;
  deferred: number;
  errors: number;
  stale: number;
};

export type KnowledgeBuildSummary = {
  id: string;
  status: KnowledgeBuildStatus;
  connection_ids: string[];
  created_at: string;
  updated_at?: string;
  expires_at?: string;
  max_items?: number;
  counts?: KnowledgeBuildCounts;
  coverage?: { complete: boolean; source_count: number; reason?: string };
};

export type KnowledgeBuild = KnowledgeBuildSummary & {
  updated_at: string;
  expires_at: string;
  max_items: number;
  counts: KnowledgeBuildCounts;
  coverage: { complete: boolean; source_count: number; reason?: string };
  sources: Array<{ connection_id: string; snapshot_id: string | null; snapshot_version: number | null; status: string }>;
  resume: { owner: 'customer_harness'; mcp_tool: 'build_knowledge'; arguments: { operation: 'claim'; arguments: { run_id: string; [key: string]: unknown } } };
  tasks?: Array<{ id: string; connection_id: string; track: string; kind: string; status: string; reason?: string; proposal_id?: string }>;
  execution: { worker_configured?: boolean; dispatch: 'customer_harness' };
};

export type ConnectionDetail = {
  connection: KnowledgeConnection;
  snapshot: KnowledgeSnapshot | null;
};

export type GraphNode = {
  id: string;
  kind: 'concept' | 'metric' | 'asset' | 'evidence' | 'field' | 'assertion' | 'source' | 'shared_term' | 'document' | 'ontology';
  label: string;
  description: string | null;
  status: string;
  trust_state: string;
  evidence_ids: string[];
  connection_id?: string;
  snapshot_id?: string;
  record_id?: string;
  source_name?: string;
  semantic_type?: string;
  definition_status?: string;
  definition_assertion_id?: string;
  definition_trust_state?: string;
  native_type?: string;
  source_pointer?: string;
  unit?: string;
  nullable?: boolean | null;
  asset_name?: string;
  source_count?: number;
  member_count?: number;
  definition_variant_count?: number;
  unit_variants?: string[];
  members?: Array<string | {
    id?: string;
    node_id?: string;
    connection_id?: string;
    record_id?: string;
    kind?: GraphNode['kind'];
    snapshot_id?: string;
    source_name?: string;
    label?: string;
    description?: string | null;
    status?: string;
    trust_state?: string;
  }>;
  conflict?: boolean | string | Record<string, unknown>;
};

export type GraphEdge = {
  id: string;
  subject_id: string;
  predicate: string;
  object_id: string;
  status: string;
  trust_state: string;
  evidence_ids: string[];
  confidence: number | null;
  assertion_id?: string | null;
};

export type KnowledgeGraph = {
  connection_id: string;
  snapshot_id: string;
  version: number;
  focus_id: string | null;
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
  assertion_count?: number;
  linked_assertions?: number;
  stale_citations?: number;
  warning?: string;
};

export type WorkspaceGraph = Omit<KnowledgeGraph, 'connection_id' | 'snapshot_id' | 'version'> & {
  view: 'workspace';
  connections: Array<{
    id: string;
    name: string;
    kind: string;
    snapshot_id: string | null;
    snapshot_version: number | null;
    agent_ready: boolean;
    source_status?: string;
  }>;
  total_sources: number;
  loaded_sources: number;
  next_cursor: string | null;
  coverage: Record<string, number | boolean | string | null>;
};

export class PlatformApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'PlatformApiError';
    this.status = status;
  }
}

const API_ROOT = '/api/knowledge';
let csrfPromise: Promise<string | null> | null = null;

export type AuthStatus = {
  mode: 'demo' | 'local' | 'oidc' | 'iap';
  configured: boolean;
  authenticated: boolean;
  bootstrap_required: boolean;
  login_url?: string;
  public_url?: string;
};

export type BrowserSession = {
  subject: string;
  sub?: string;
  mode?: AuthStatus['mode'];
  groups: string[];
  steward: boolean;
  csrf_token: string | null;
};

export type InstallationSetup = {
  mode: AuthStatus['mode'];
  public_url?: string;
  agent: {
    endpoint: string;
    auth: 'bearer' | 'oidc' | 'iap';
    token_env: 'KNOWLEDGE_AGENT_TOKEN';
    token_file_hint?: string;
    clients: { codex: string; claude: string; cursor: string };
    command_template: string;
  };
  checks: Array<{ id: string; status: 'ready' | 'action'; title: string; detail: string }>;
  sources: { count: number };
  builds: { available: boolean };
  next_step: 'agent' | 'sources' | 'build';
};

function sessionExpired() {
  csrfPromise = null;
  window.dispatchEvent(new Event('knowledge-session-expired'));
}

function sessionRequestToken(session: BrowserSession): string | null {
  if (session?.mode === 'demo' && session.csrf_token === null) return null;
  if (typeof session?.csrf_token !== 'string' || !session.csrf_token.trim()) {
    throw new PlatformApiError('The session is missing its request token.', 403);
  }
  return session.csrf_token;
}

async function csrfToken(): Promise<string | null> {
  if (!csrfPromise) {
    csrfPromise = fetch('/auth/me', { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' })
      .then(async (response) => {
        if (response.status === 401) {
          sessionExpired();
          throw new PlatformApiError('The sign-in session expired.', 401);
        }
        if (!response.ok) throw new PlatformApiError('Unable to verify your session.', response.status);
        const session = await response.json() as BrowserSession;
        return sessionRequestToken(session);
      })
      .catch((error: unknown) => { csrfPromise = null; throw error; });
  }
  return csrfPromise;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return requestURL<T>(`${API_ROOT}${path}`, init, true);
}

async function requestURL<T>(url: string, init?: RequestInit, authenticated = false): Promise<T> {
  let response: Response;
  try {
    const csrf = authenticated && init?.method && !['GET', 'HEAD'].includes(init.method.toUpperCase())
      ? await csrfToken() : null;
    response = await fetch(url, {
      credentials: 'same-origin',
      cache: 'no-store',
      ...init,
      headers: {
        Accept: 'application/json',
        ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
        ...(csrf ? { 'X-Knowledge-CSRF': csrf } : {}),
        ...init?.headers,
      },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    if (error instanceof PlatformApiError) throw error;
    throw new PlatformApiError('The service could not be reached. Check that the platform is running, then try again.', 0);
  }

  const body = await response.json().catch(() => null) as unknown;
  if (!response.ok) {
    if (response.status === 401 && authenticated) sessionExpired();
    const detail = body && typeof body === 'object' && 'detail' in body
      ? String((body as { detail?: unknown }).detail ?? '')
      : '';
    const message = response.status === 404 && detail.trim().toLowerCase() === 'not found'
      ? 'The source service does not have this route yet. Check that the platform is running the current version.'
      : detail || (response.status >= 500
      ? 'The source could not be curated. Check the connection details and try again.'
      : `The request could not be completed (${response.status}).`);
    throw new PlatformApiError(message, response.status);
  }
  return body as T;
}

export async function getAuthStatus(signal?: AbortSignal): Promise<AuthStatus> {
  const status = await requestURL<AuthStatus>('/auth/status', { signal });
  if (!status || !['demo', 'local', 'oidc', 'iap'].includes(status.mode)
    || typeof status.configured !== 'boolean' || typeof status.authenticated !== 'boolean'
    || typeof status.bootstrap_required !== 'boolean') {
    throw new PlatformApiError('Sign-in configuration could not be verified.', 502);
  }
  return status;
}

export async function getBrowserSession(signal?: AbortSignal): Promise<BrowserSession> {
  try {
    const session = await requestURL<BrowserSession>('/auth/me', { signal }, true);
    csrfPromise = Promise.resolve(sessionRequestToken(session));
    return session;
  } catch (error) {
    csrfPromise = null;
    throw error;
  }
}

export async function signInLocally(input: { name: string; password: string }): Promise<BrowserSession> {
  const session = await requestURL<BrowserSession>('/auth/login', { method: 'POST', body: JSON.stringify(input) });
  csrfPromise = Promise.resolve(sessionRequestToken(session));
  return session;
}

export async function setUpLocalOwner(input: { name: string; password: string; bootstrap_token: string }): Promise<BrowserSession> {
  const session = await requestURL<BrowserSession>('/auth/setup', { method: 'POST', body: JSON.stringify(input) });
  csrfPromise = Promise.resolve(sessionRequestToken(session));
  return session;
}

export async function signOut(): Promise<void> {
  await requestURL('/auth/logout', { method: 'POST' }, true);
  sessionExpired();
}

export function getInstallationSetup(signal?: AbortSignal): Promise<InstallationSetup> {
  return requestURL<InstallationSetup>('/api/setup', { signal }, true);
}

export async function getConnectorTypes(): Promise<ConnectorType[]> {
  const result = await request<{ connector_types: ConnectorType[] }>('/connector-types');
  return Array.isArray(result.connector_types) ? result.connector_types : [];
}

export type ConnectionPage = {
  connections: KnowledgeConnection[];
  next_cursor: string | null;
  total: number;
};

export async function getConnectionPage(cursor?: string): Promise<ConnectionPage> {
  const params = new URLSearchParams({ limit: '50' });
  if (cursor) params.set('cursor', cursor);
  const result = await request<ConnectionPage>(`/connections?${params}`);
  return {
    connections: Array.isArray(result.connections) ? result.connections : [],
    next_cursor: typeof result.next_cursor === 'string' ? result.next_cursor : null,
    total: Number.isSafeInteger(result.total) ? result.total : 0,
  };
}

export async function createConnection(input: {
  name: string;
  kind: string;
  config: Record<string, unknown>;
}): Promise<KnowledgeConnection> {
  const result = await request<{ connection: KnowledgeConnection }>('/connections', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return result.connection;
}

export async function getConnection(id: string): Promise<ConnectionDetail> {
  const result = await request<ConnectionDetail>(`/connections/${encodeURIComponent(id)}`);
  if (result.connection) {
    return { ...result, snapshot: result.snapshot ?? result.connection.snapshot ?? null };
  }
  throw new PlatformApiError('Connection details were missing from the service response.', 502);
}

export async function getStewardRole(): Promise<boolean> {
  const session = await getBrowserSession();
  return session.steward === true;
}

export async function getPublicationPreview(id: string): Promise<PublicationPreview> {
  return request<PublicationPreview>(`/connections/${encodeURIComponent(id)}/publication`);
}

export async function previewReviewBatch(connectionId: string, selection: ReviewBatchSelection): Promise<ReviewBatchPreview> {
  return request<ReviewBatchPreview>(`/connections/${encodeURIComponent(connectionId)}/review-batch/preview`, {
    method: 'POST', body: JSON.stringify(selection),
  });
}

export async function acceptReviewBatch(connectionId: string, input: ReviewBatchSelection & {
  expected_review_digest: string;
  rationale: string;
  publish: boolean;
}): Promise<ReviewBatchResult> {
  return request<ReviewBatchResult>(`/connections/${encodeURIComponent(connectionId)}/review-batch/accept`, {
    method: 'POST', body: JSON.stringify(input),
  });
}

export async function listKnowledgeBuilds(connectionId: string, signal?: AbortSignal): Promise<KnowledgeBuildSummary[]> {
  const params = new URLSearchParams({ connection_id: connectionId, limit: '20' });
  const result = await request<{ builds: KnowledgeBuildSummary[] }>(`/builds?${params}`, { signal });
  return Array.isArray(result.builds) ? result.builds : [];
}

export async function getKnowledgeBuild(runId: string, signal?: AbortSignal): Promise<KnowledgeBuild> {
  const result = await request<{ build: KnowledgeBuild }>(`/builds/${encodeURIComponent(runId)}`, { signal });
  return result.build;
}

export async function startKnowledgeBuild(input: { connection_ids: string[]; max_items: number; max_seconds: number }): Promise<KnowledgeBuild> {
  const result = await request<{ build: KnowledgeBuild }>('/builds', { method: 'POST', body: JSON.stringify(input) });
  return result.build;
}

export async function cancelKnowledgeBuild(runId: string, reason: string): Promise<KnowledgeBuild> {
  const result = await request<{ build: KnowledgeBuild }>(`/builds/${encodeURIComponent(runId)}/cancel`, {
    method: 'POST', body: JSON.stringify({ reason }),
  });
  return result.build;
}

export async function publishSource(id: string, expectedSnapshotId: string, expectedSnapshotVersion: number,
                                    rationale: string): Promise<void> {
  await request(`/connections/${encodeURIComponent(id)}/publication`, {
    method: 'POST', body: JSON.stringify({ expected_snapshot_id: expectedSnapshotId,
      expected_snapshot_version: expectedSnapshotVersion, rationale }),
  });
}

export async function withdrawPublication(id: string, reason: string): Promise<void> {
  await request(`/connections/${encodeURIComponent(id)}/publication/withdraw`, {
    method: 'POST', body: JSON.stringify({ reason }),
  });
}

export async function getKnowledgeGraph(
  connectionId: string,
  options: { focusId?: string; depth?: number; limit?: number } = {},
): Promise<KnowledgeGraph> {
  const params = new URLSearchParams();
  if (options.focusId) params.set('focus_id', options.focusId);
  params.set('depth', String(options.depth ?? (options.focusId ? 2 : 1)));
  params.set('limit', String(options.limit ?? 60));
  return request<KnowledgeGraph>(`/connections/${encodeURIComponent(connectionId)}/graph?${params}`);
}

export async function getFederatedGraph(connectionId: string): Promise<KnowledgeGraph> {
  return request<KnowledgeGraph>(`/connections/${encodeURIComponent(connectionId)}/federated-graph?limit=8`);
}

export async function getWorkspaceGraph(options: {
  connectionId?: string;
  query?: string;
  focusId?: string;
  depth?: 1 | 2;
  limit?: number;
  sourceLimit?: number;
  cursor?: string;
  signal?: AbortSignal;
} = {}): Promise<WorkspaceGraph> {
  const params = new URLSearchParams();
  if (options.connectionId) params.set('connection_id', options.connectionId);
  if (options.query) params.set('q', options.query);
  if (options.focusId) params.set('focus_id', options.focusId);
  if (options.cursor) params.set('cursor', options.cursor);
  params.set('depth', String(options.depth ?? 2));
  params.set('limit', String(options.limit ?? 180));
  params.set('source_limit', String(options.sourceLimit ?? 40));
  return request<WorkspaceGraph>(`/workspace/graph?${params}`, { signal: options.signal });
}

export async function refreshConnection(id: string): Promise<ConnectionDetail | { job: RefreshJob }> {
  const result = await request<ConnectionDetail | { job: RefreshJob }>(`/connections/${encodeURIComponent(id)}/refresh`, {
    method: 'POST',
  });
  if ('job' in result && result.job) return result;
  if ('connection' in result && result.connection) {
    return { ...result, snapshot: result.snapshot ?? result.connection.snapshot ?? null };
  }
  throw new PlatformApiError('The refresh response did not include a connection or job.', 502);
}

export async function decideProposal(
  connectionId: string,
  proposalId: string,
  decision: 'approve' | 'reject',
  rationale?: string,
): Promise<{ proposal: CurationRecord }> {
  const result = await request<{ proposal: CurationRecord }>(
    `/connections/${encodeURIComponent(connectionId)}/proposals/${encodeURIComponent(proposalId)}/decision`,
    { method: 'POST', body: JSON.stringify({ decision, rationale: rationale?.trim() || undefined }) },
  );
  return result;
}

export async function archiveProposals(connectionId: string, proposalIds: string[], reason: string): Promise<void> {
  await request(`/connections/${encodeURIComponent(connectionId)}/proposals/archive`, {
    method: 'POST', body: JSON.stringify({ proposal_ids: proposalIds, reason }),
  });
}
