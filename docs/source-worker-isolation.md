# Source-scoped refresh execution

The trusted refresh coordinator owns registry access. Connector IO receives only
the selected source profile, never registry, OIDC, gateway, model or other source
credentials. Operator policy and the requester's direct management grant are
rechecked before source IO, at heartbeat and inside result commits. An asynchronous
job cannot preserve a revoked grant or invent current IdP group membership; give
its verified requester an explicit direct grant and source-policy subject entry.

Set both `KNOWLEDGE_WORKER_TENANT` and `KNOWLEDGE_WORKER_CONNECTION_IDS` to restrict
claiming before queue locks. A worker rejects execution outside that scope too.
Provision one coordinator per source credential boundary. Disable the generic
worker when enabling dedicated workers; an unscoped worker can otherwise claim
their jobs. Keep the API free of source credential mounts.

## Process and container modes

The default `KNOWLEDGE_CONNECTOR_RUNTIME=process` is bounded subprocess execution,
with an exact environment allowlist, private stdin/stdout, an empty working
directory, Python isolated mode, finite wall/output budgets and process-group
cleanup. It preserves the small pilot install. It is **not an OS sandbox**: the
same UID can still read files mounted in the coordinator's filesystem.

Set `KNOWLEDGE_CONNECTOR_RUNTIME=container` for a separate Docker filesystem and
process namespace. `KNOWLEDGE_CONNECTOR_IMAGE` must be an immutable SHA-256 image
identity already installed locally. The launch refuses mutable tags, host and
shared-container networking, fetching images, bind mounts, published ports and
privileged capabilities. It runs as UID/GID 10001, with a read-only root, no Linux
capabilities, no privilege escalation, bounded memory/PIDs/file descriptors and
private temporary storage. Only the selected credential enters private stdin;
it is absent from argv, Docker environment metadata and host mounts. Cleanup
verifies the task's random container label. An unavailable daemon or uncertain
cleanup fails the operation.

Networking defaults to `none`. An operator may choose a named network and must
separately enforce source-specific egress and cloud metadata-service denial at
the host/VPC. The application cannot establish that firewall policy. Local-file
sources refuse container mode until a separately reviewed source-only mount is
provisioned; this path never mounts an arbitrary source-provided host path.
Certificate-file profiles likewise require a separately qualified mount path.

The Docker client belongs only to the **trusted coordinator**. Do not mount the
Docker socket in the web API or the connector container. Docker daemon access is
host administration authority; restrict coordinator ownership accordingly. The
minimal application images intentionally contain no Docker client. Run the
coordinator from a frozen host installation using
[the systemd unit](../deploy/knowledge-source-worker.service) and
[reference-only environment template](../deploy/source-worker.env.example), or
use an equivalently reviewed orchestration controller. These templates do not
install a service or alter an existing customer deployment.

Queued native reads now commit `source.native_read_started` before IO and a
matching `source.native_read_finished` outcome after return/failure. Both bind
the verified requester, tenant, connection, exact job attempt and source identity
digest. A failed start-ledger insert prevents source IO. Driver errors, source
text and credentials are excluded. Hard process/host loss can leave an unmatched
start; alert on it alongside failed attempts. The events describe a discovery
attempt, not every provider request, row, field, direct source tool or guaranteed
delivery to the client. Independently retained export/log delivery and retention
remain deployment obligations; the registry ledger is not tamper evident.
