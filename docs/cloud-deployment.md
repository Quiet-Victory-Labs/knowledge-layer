# Small single-organization deployment

The supported architecture remains one Python API with its bundled static UI, one
worker using the same image, and PostgreSQL. The optional Caddy proxy terminates
HTTPS. There is no required model, vector service, broker, Kubernetes cluster or
Node runtime. The new deployment bundle is a **pilot recipe**: Terraform validation
and local container evidence do not establish a successful customer-cloud install.

## Prepare the reviewed install

Use a verified immutable image from the [release lane](../RELEASING.md). Copy
[`deploy/settings.example.json`](../deploy/settings.example.json) outside Git and
replace the image digests, HTTPS origin, IdP issuer/client/audience/JWKS, groups
and data path. Pin verified PostgreSQL and Caddy digests too. Keep the selected
connector allowlist small; additional drivers require a separately reviewed image.

Create an owner-only OIDC client-secret file and an owner-only source-policy file
following [source access](source-access.md). A valid empty policy starts with
`{"version":1,"sources":[]}` and permits no sources. The same source policy must
cover the subjects/groups that receive registry grants. Registration alone never
grants a principal access to all metadata.

Unpack the verified matching source distribution at `/root/knowledge-release`.
Keep settings and the two protected input files in `/root/knowledge-inputs`.
Run configuration **inside the verified application image**; a fresh Ubuntu VM's
system Python does not supply the application's Python 3.14 runtime/dependencies:

```sh
sudo docker run --rm --network none --read-only --user 0:0 \
  --cap-drop ALL --cap-add CHOWN --security-opt no-new-privileges \
  --memory 384m --cpus 1 --pids-limit 64 --tmpfs /tmp:size=64m \
  --mount type=bind,src=/root/knowledge-release,dst=/reviewed,readonly \
  --mount type=bind,src=/root/knowledge-inputs,dst=/inputs,readonly \
  --mount type=bind,src=/srv/knowledge-layer,dst=/srv/knowledge-layer \
  --entrypoint python YOUR_VERIFIED_IMAGE_AT_SHA256 \
  /reviewed/scripts/configure_install.py \
  --settings /inputs/settings.json \
  --client-secret-file /inputs/oidc-secret \
  --source-policy-file /inputs/source-policy \
  --output /srv/knowledge-layer/install
cd /srv/knowledge-layer/install
sudo docker compose up -d --wait postgres
sudo docker compose run --rm migrate
sudo docker compose --profile https up -d --wait
```

Replace `YOUR_VERIFIED_IMAGE_AT_SHA256` with the same verified digest specified in
settings; pull/verify it before this offline step. Configuration uses the image's
Python runtime and the reviewed matching source. It creates random database/session
secrets, refuses existing installation paths,
and writes secrets into separate owner-only regular files. Its defaults use
container UID/GID 10001 when run as root. It does not print credentials or start
services. The installed `.env` contains non-secret settings and numeric ownership.
Secrets are absent from Terraform variables, state and VM metadata.
The one-time root configuration container needs only `CHOWN` to assign protected
files to UID/GID 10001. Dropping that capability makes ownership assignment fail;
the API and worker continue to drop every capability.

Register `https://YOUR_HOST/auth/callback` at the IdP and point the domain's DNS at
the VM before starting the HTTPS profile. Caddy obtains the certificate when DNS,
ports 80/443 and its outbound certificate-authority access are available. To use a
customer proxy, omit `--profile https`; the API binds to loopback port 8273. Preserve
the external Host header. Only the proxy publishes ports on all interfaces;
PostgreSQL has no host port. [Docker's production guidance](https://docs.docker.com/compose/how-tos/production/)
describes Compose production overrides and restart configuration.

The application buffers at most 1 MiB per request and expires body reception
after ten seconds. MCP checks authentication before reading its envelope; the
deadline ends when body reception completes, leaving tool processing and
responses unaffected. Early body/auth refusals close HTTP/1 connections with
unread uploads; HTTP/2 forbids that header and needs ingress stream budgets.
The image uses Uvicorn's concurrency limit of 64 and
five-second keepalive; excess complete requests receive 503. This limit does
not cap sockets before headers complete. The bundled proxy therefore sets a
five-second header-read timeout, ten-second body-read timeout, 30-second idle
timeout and 32 KB headers on both HTTP and HTTPS listeners, without a total
response deadline. Equivalent header/body/connection policies are required at a
customer proxy or Kubernetes ingress. Keep the API private behind that boundary.
The offline CI configuration check uses pinned Caddy 2.11.6; it validates the
recipe, not customer traffic or all ingress implementations.
[Caddy server timeout controls](https://caddyserver.com/docs/caddyfile/options#timeouts).

Provision read-only source credentials/mounts in a private Compose override on
the **worker**, following the existing [OIDC pilot](oidc-pilot.md). Do not mount
source secrets on the API. The worker's credentials remain shared capabilities,
so this topology does not claim per-source process isolation. Agent source
management and autonomous publication start disabled. Enable only reviewed
scopes and policies after the customer access drill.

## GCP first

[`deploy/gcp`](../deploy/gcp/main.tf) creates one Ubuntu VM, a dedicated VPC/subnet,
a static IP, a separate protected data disk and a daily seven-day snapshot
schedule. It enables OS Login and Shielded VM controls. SSH ingress is restricted
to IAP, HTTP/HTTPS can be restricted to client egress ranges, and its dedicated
service account has no project roles. The administrator needs their own approved
OS Login/IAP and resource-management permissions; the module grants none.

```sh
cd deploy/gcp
cp terraform.tfvars.example terraform.tfvars
# Set the approved project, region, zone and ingress ranges.
terraform init
terraform validate
terraform plan -out=pilot.tfplan
# Review the concrete plan and cost before applying it.
terraform apply pilot.tfplan
```

The startup script installs Docker/Compose and mounts only the module's exact
`google-knowledge-data` device at `/srv/knowledge-layer`. It **never formats a disk**:
a failed signature probe or unexpected filesystem refuses startup, including after
reboots. On the first install, confirm that the reviewed Terraform plan created
this exact new unused disk, inspect its identity with `lsblk -f`, and deliberately
initialize only that disk before rerunning the reviewed bootstrap:

```sh
# First initialization only, after verifying the new unused disk's identity.
sudo mkfs.ext4 -L knowledge-data /dev/disk/by-id/google-knowledge-data
sudo bash /root/knowledge-release/deploy/gcp/bootstrap.sh
```

Never run the format command on a reused, damaged or uncertain disk. The bootstrap
also checks the mounted filesystem UUID. It does not deploy a demo service.
Upload the verified source bundle and protected configuration using IAP
SSH/SCP, then run the install commands above. Terraform intentionally blocks
accidental data-disk destruction, and the VM has deletion protection. Planned
decommissioning needs a retained restore-tested backup and deliberate removal of
those protections. Protect the Terraform state and inspect IAM/network readback.

The starting size is `e2-medium`, 4 GiB RAM, with 20 GiB boot + 30 GiB data disks.
E2 medium is a shared-core machine: its two visible vCPUs do not provide two
dedicated sustained CPUs. Qualify it under the customer's load; use a dedicated
machine if the measured latency or refresh budget requires it.
[Google machine characteristics](https://docs.cloud.google.com/compute/docs/general-purpose-machines).

For Iowa/us-central1, an illustrative 730-hour month is about **$33.11 before
snapshot storage, egress, logs, taxes and optional services**: $24.46 for E2 medium
at $0.03350571/hour, $5 for 50 GiB balanced disk at approximately $0.10/GiB-month,
and $3.65 for an in-use public IPv4 at $0.005/hour. This is a calculated list-price
example checked on 2026-10-04, not a measured customer bill or performance promise.
Verify region/account pricing before apply.
[VM pricing](https://cloud.google.com/products/compute/pricing/general-purpose),
[disk pricing](https://cloud.google.com/compute/disks-image-pricing),
[IP pricing](https://cloud.google.com/vpc/pricing).

## Backup, upgrades and failure qualification

Create protected backup storage and run a logical backup before every migration:

```sh
sudo install -d -m 0700 /srv/knowledge-layer/backups
sudo python3 scripts/backup_registry.py \
  --install /srv/knowledge-layer/install --output /srv/knowledge-layer/backups
```

For a restore drill, stop only this installation's writers, keep PostgreSQL
running, and require a matching isolated restore of **all** registry table rows:

```sh
cd /srv/knowledge-layer/install
sudo docker compose stop api worker
sudo python3 /path/to/reviewed/scripts/backup_registry.py \
  --install /srv/knowledge-layer/install --output /srv/knowledge-layer/backups \
  --verify-restore --quiescent
sudo docker compose --profile https up -d --wait
```

The drill never restores over the live database. It records dump size/checksum,
backup time, restore time and row fingerprints, then removes its temporary
database. Changed data during the comparison fails verification. Copy dumps and
their manifests into protected off-host storage and verify retrieval/restore.
Disk snapshots are crash-consistent protection, not a replacement for the logical
drill. [Google snapshot guidance](https://docs.cloud.google.com/compute/docs/disks/scheduled-snapshots).
Retain the installation's credential/configuration files securely too; losing the
VM's signing/client/database configuration can prevent recovery.

The supplied systemd service/timer runs a daily logical backup. Install the exact
reviewed `backup_registry.py` at `/opt/knowledge-layer-ops/backup_registry.py`, copy
the two units to `/etc/systemd/system`, reload systemd and enable
`knowledge-backup.timer`. Test the service and configure an `OnFailure` integration
with the company's monitored channel. The backup tool does not delete prior dumps;
define retention and disk-full alerts explicitly. Daily dumps and seven retained
snapshots do not establish the proposed RPO <=24h or RTO <=60m until measured in
the actual recovery environment.

Upgrade the verified image digest, take a tested backup, stop writers, run the
guarded migration, start services and verify permissions/current publications.
Migrations are forward-only; rollback restores a compatible database backup and
its matching image. Restart API and worker after source-policy or database/client/
session-key changes. Source credential files are reread on refresh, but bind-file
replacement may need container recreation; qualify the chosen secret manager.

## Monitoring and portability

Use [`monitor_installation.py`](../scripts/monitor_installation.py) with an
owner-only rotating agent-token file and an owner-only JSON list of 1-25 expected
connection IDs. It samples readiness, unauthenticated denial and **complete**
inventory freshness. It refuses redirects and unknown freshness clocks, keeps
provider diagnostics out of output, records private JSONL, and exits nonzero if
any sample fails. `--duration-hours 168 --interval-seconds 60` prepares a seven-day
record; omitting duration runs once. Test failure delivery, not only success.

A customer endurance run must additionally inject source/IdP outages, worker
loss, schema drift, credential/grant revocation and restoration, while checking
that failed attempts never advance complete freshness and stale meaning refuses.
Export bounded access/audit events to protected external storage with retention,
loss detection and immutable storage controls. Local rotated container logs are
bounded convenience storage, not a tamper-evident audit system. Uvicorn access
logging is disabled because raw URLs can contain search terms; application access
events intentionally omit queries and bodies. HTTP bodies are limited to 1 MiB
before JSON parsing; MCP retains its separate authentication-first envelope guard
with the same 1 MiB limit.

The same reviewed Compose bundle can run on a generic Linux VM in Azure or AWS.
Those clouds remain **unqualified** until provisioned with their own restricted
SSH/IAM/network/disk/backup controls and the same customer tests. No cloud-specific
managed service is required by the app. This guide intentionally claims only the
actual recipe and local evidence, not an Azure/AWS deployment result.
