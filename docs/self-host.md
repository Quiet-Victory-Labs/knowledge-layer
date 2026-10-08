# Install on your own computer or server

The portable installer uses Docker Engine with Compose and Python 3.11 or newer
on a POSIX host (Linux or macOS) with Linux containers. Native Windows filesystem
ownership and permission behavior is outside this installer profile.
It builds the app from the checked-out source; no unpublished application image,
host Python packages, Node installation, cloud SDK, model account or identity
provider is needed for the default single-owner profile.

```sh
./knowledge-layer start
```

Open `http://127.0.0.1:4173/`. The setup page asks for the installation bootstrap
token, then creates the owner's login. Read the token from the private file shown
by the launcher, or explicitly display it in your terminal:

```sh
./knowledge-layer bootstrap
```

The installer never displays this token during normal initialization or startup.
Keep it private. The backend claims the owner once, saves the owner in a private
file and checks the current file on subsequent requests. The owner identity is
`local-owner`; the agent credential represents the separately constrained
`local-agent`. This profile supplies one owner, not organization membership or
upstream permission mirroring. Use the existing OIDC profile for an organization
with an identity provider.

The default installation directory is
`${XDG_DATA_HOME:-$HOME/.local/share}/knowledge-layer`. It contains the durable
PostgreSQL database, private secrets, owner authentication state, Compose
configuration and selected source files. It is outside the repository. Files
created by setup have mode `0600`; directories have mode `0700`. The app runs as
the non-root installation owner UID with a read-only root filesystem; only its
small authentication directory and temporary memory filesystem are writable.
The project's PostgreSQL image supports the same arbitrary non-root UID. It is
built locally from the digest-pinned PostgreSQL 18.6 Alpine parent using
[`Dockerfile.postgres`](../Dockerfile.postgres); its non-root entrypoint refuses
UID 0 and retains the parent/removal inventory described in the
[container runtime guide](container-runtime.md).

Use a different directory or loopback port by initializing explicitly. The
global `--install-dir` option comes before the command:

```sh
./knowledge-layer --install-dir /absolute/private/knowledge init --port 4273
./knowledge-layer --install-dir /absolute/private/knowledge start
```

Initialization refuses an existing directory and never rotates existing secrets.
Startup reuses them, including the saved owner. Authentication settings cannot
silently change on a subsequent `start`. A partial preparation remains private
and has no committed installation manifest; inspect it before removing it.
Public origins are canonicalized: hostname case and default HTTP/HTTPS ports
resolve to the same stored origin. Paths, credential-bearing URLs, interpolation
characters, malformed hostnames and IPv6 zone identifiers are refused.

## Sources and agents

The default source allowlist contains only `local_files`. Copy the folders you
want to connect into the installation's `sources/` directory, then select their
relative paths in the UI. That directory is mounted read-only on the API for
selection and on the refresh worker for discovery. The API has no remote source
credentials. To copy the two bundled synthetic examples during initialization:

```sh
./knowledge-layer init --examples
./knowledge-layer start
```

Configure your existing agent client without replacing other MCP servers:

```sh
./knowledge-layer agent --client codex --project /absolute/customer/project
./knowledge-layer agent --client claude --project /absolute/customer/project
./knowledge-layer agent --client cursor --project /absolute/customer/project
```

These commands write an environment reference, not credential bytes. The command
prints a shell instruction that loads `KNOWLEDGE_AGENT_TOKEN` from its private
file into the environment of your chosen client. Confirm that client's server
trust prompt. Agent configuration does not launch a model worker or paid harness.
The local agent cannot create or refresh sources, review claims or publish them.
For Codex, trust the project in Codex before running the agent: Codex skips a
project's `.codex/config.toml` until that project is trusted. Run `codex mcp list`
from the trusted project and confirm `knowledge_layer` appears. If it does not,
check project trust and that `KNOWLEDGE_AGENT_TOKEN` is in the Codex process
environment before asking the agent to use the layer.

Other source kinds require an explicit operator source policy and allowlist at
initialization. Connector permissions and optional runtime extras retain their
existing qualification requirements. Put approved source credentials only in
`source-secrets/`, in owner-only files owned by the runtime UID. Copy
[`worker.override.example.yaml`](../deploy/self-host/worker.override.example.yaml)
into the installation as `worker.override.yaml`, with mode `0600`, to mount the
selected files on the refresh worker. It is included automatically. Do not put
source credentials on the API or migration container. See the
[source access policy](source-access.md) and
[worker isolation guide](source-worker-isolation.md) before adding remote sources.

## Run, check and stop

```sh
./knowledge-layer status
./knowledge-layer doctor
./knowledge-layer stop
./knowledge-layer start
```

Startup builds both the application and PostgreSQL source images, waits for
PostgreSQL initialization, runs the
existing guarded migrations with the restricted application role, then starts
the API and refresh worker. The API must pass `/api/ready`; the default startup
wait is bounded to 180 seconds after the build. `--wait-seconds` accepts 10–600
seconds, and `--no-build` reuses images already built or explicitly tagged for
this installation.
Docker build and command failures produce a generic diagnostic without dumping
database URLs or provider logs. Inspect private container logs locally when
needed. `doctor` checks Docker, private files and Compose configuration; it does
not certify a source's permission controls.

Before starting a stopped installation, the launcher checks that its loopback
port is available without modifying the process using it. If the port is busy,
free it or initialize a separate directory with an unused `--port`. A failed
build starts no services. A failed readiness wait attempts to stop only container
IDs newly introduced in this exact Compose project, preserving prior containers
and durable files. If cleanup cannot be confirmed, the error directs you to this
installation's `stop` command. Inspect private PostgreSQL, migration and API logs,
resolve the error, then retry `start`; do not reinitialize or delete retained
data to recover an ordinary startup failure.

Only PostgreSQL initialization sees the separate bootstrap database password.
It creates `knowledge_layer` with `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`,
`NOREPLICATION` and `NOBYPASSRLS`, owning only its application database and schema.
The app, migration and refresh containers receive only the application URL.
PostgreSQL has no published host port. CPU, memory, process and log limits apply
to every service. Container stop preserves the database, owner and secrets.

## Cold backup, restore and upgrade

The portable profile has its own complete cold recovery commands. The older
cloud deployment's logical database helper does not back up the owner's login,
installation secrets or source files, and its temporary-database verification
requires privileges that the portable app role deliberately lacks.

Choose an owner-only backup directory outside the repository and installation:

```sh
mkdir -m 700 /absolute/private/knowledge-backups
./knowledge-layer stop
./knowledge-layer backup --output /absolute/private/knowledge-backups/bundle.tar
```

Backup requires an initialized PostgreSQL 18 cluster, no running containers for
this exact project, no retained `postmaster.pid`, and a recorded runtime image
receipt. The launcher records actual API and PostgreSQL container image IDs on a
successful start and before stop; it does not infer them from mutable image tags.
For an older installation without this receipt, complete a verified start and
stop before backup. Backup never stops services itself.

The private, uncompressed archive includes the complete installation directory:
PostgreSQL files, owner login, database and agent credentials, source files and
configuration. Its adjacent `.tar.json` receipt records checksums, PostgreSQL
major, actual image IDs and available repository digests, Docker OS/architecture,
and checkout commit and dirty status at backup time. Checkout status is not proof
of the running image's source provenance. Keep both files together, protect them
like credentials, and make your own encrypted off-host copy. The archive contains
only files under the installation; mount overrides that reference external files
require separate protected copies. Links and special files are refused. A bundle
is bounded to 2 GiB of file payload, 10,000 files/directories and a ten-minute
operation budget; paths must fit the archive's 255-byte USTAR limit.
Restore walks fixed-size USTAR headers before invoking the archive parser. PAX,
GNU long-name/long-link and sparse extensions are refused before their declared
payload can be allocated; only regular files, directories and zero end padding
are accepted. Third-party archive rewrites are unsupported.

Restore into a new directory beneath an owner-only parent. It never overwrites
an existing installation or starts containers:

```sh
./knowledge-layer --install-dir /absolute/private/restored-knowledge restore \
  --backup /absolute/private/knowledge-backups/bundle.tar --port 4273
```

Restore verifies the archive checksum, complete manifest and individual files,
rejecting path traversal, duplicate entries and links. It requires the same
Linux Docker architecture and the exact recorded PostgreSQL and application
images to be locally available. It reuses that exact PostgreSQL image ID and
assigns a fresh Compose project and the current checkout. Database and agent
credentials, owner name/password hash and accepted publication data are
preserved. Existing local owner browser sessions are invalidated; sign in with
the saved owner password after starting the restored instance. OIDC settings and
their existing session handling are retained. A new local loopback port
also updates its origin. Use `--public-url https://HOST` explicitly when the shared
server origin changes, and regenerate your agent client's endpoint configuration.

The restore JSON prints the recorded app `image_id` and new `project`. Tag that
exact local image using those values, then start without rebuilding:

```sh
docker image tag sha256:RECORDED_APP_ID knowledge-layer-local:NEW_PROJECT
./knowledge-layer --install-dir /absolute/private/restored-knowledge start --no-build
```

The launcher refuses a different app tag or an implicit rebuild of a restored
installation. Inspect login, source readiness and accepted publication pins after
start. Filesystem verification alone does not establish database recovery or
semantic correctness. These physical bundles support the same PostgreSQL major,
image and Docker platform; they are not cross-major logical migrations. Retain the
original installation and bundle until your own restored customer flow succeeds.
The CLI does not claim off-host availability or customer RPO/RTO qualification.

For an upgrade, first stop and create a new complete backup, then update the
checkout at the same path and start it to rebuild and run guarded migrations.
On a restored installation, deliberate upgrades require `start --allow-upgrade`.
After success its guarded app ID is updated to the new actual runtime image.
Its PostgreSQL image stays pinned to the recorded ID, without a build definition;
an application upgrade rebuilds only the app.
Inspect the release's migration compatibility before proceeding. To roll back,
restore the prior bundle into a new directory with its prior exact app image;
do not run old app code against a database already migrated by a newer release.

Failed recovery operations preserve the bundle, original installation and any
private partial archive or `.knowledge-restore-*` directory. A failed backup has
no completed sidecar; a partial restore has no usable destination manifest. Keep
these for inspection and remove them explicitly only after confirming that no
services use them. The launcher never deletes data as recovery cleanup.

Deleting an installation requires an explicit command that removes only its
generated Compose project and installation directory:

```sh
./knowledge-layer remove --purge
```

## A shared server or VM

The same bundle can run on a computer or an existing GCP, AWS or Azure VM:

```sh
./knowledge-layer init --public-url https://knowledge.example.org
./knowledge-layer start
```

The listener still binds to loopback. Provide your own HTTPS reverse proxy,
certificate, firewall and durable disk; proxy requests to `127.0.0.1:4173` on the
same host and retain the configured public origin. The installer creates no
cloud compute, networking or paid resources. Stopping the containers does not
stop an independently operated VM.

For existing OIDC settings, supply protected files using the same identity keys
as [`deploy/settings.example.json`](../deploy/settings.example.json): `issuer`,
`client_id`, `audience`, `jwks_uri`, `allowed_group`, `steward_group`, and optionally
`enabled_kinds`. Existing deployment image and data-root fields are accepted as
input but the portable bundle builds its own image and uses its installation
directory. The installer does not create an identity provider or client:

```sh
./knowledge-layer init --auth oidc --public-url https://knowledge.example.org \
  --oidc-settings /private/settings.json \
  --client-secret-file /private/client-secret \
  --source-policy /private/source-policy.json
./knowledge-layer start
```

OIDC settings must use HTTPS, with distinct user and steward groups. The existing
backend validates the complete IdP and source policy configuration on startup.
For an OIDC agent, expose an IdP-issued token with the configured audience as
`KNOWLEDGE_AGENT_TOKEN`; the portable installer generates no IdP tokens.
Read the [OIDC pilot guide](oidc-pilot.md) for client registration and deployment
limits. Neither this installer nor its offline tests establishes a customer
deployment, native source ACL equivalence or production qualification.
