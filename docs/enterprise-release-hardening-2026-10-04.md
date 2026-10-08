# Release and small-install hardening evidence

This increment prepares a reviewable single-organization pilot and a guarded OSS
release lane. It **does not declare enterprise readiness or a public release**.
Keep Python, PostgreSQL and the bundled static UI; no mandatory model, broker,
vector database, separate Node server or Kubernetes service was added.

## Implemented

- [Optional offline SHACL Core validation](rdf-validation.md) and packaged shapes.
- [Small cloud deployment](cloud-deployment.md): immutable images, bounded nonroot
  API/worker, protected files, optional HTTPS and a validated GCP VM/disk/network
  recipe. Startup never autoformats retained disks and checks the mounted UUID.
  Configuration runs inside the reviewed image rather than Ubuntu system Python.
- Logical dump and isolated restore comparison of every registry table, host-side
  fingerprint deadlines, daily backup units and bounded complete-freshness monitoring.
- Request-local context/search batching, fresh term/direct-ID source and
  cross-source checks, explicit scoped concept identities, actual-byte HTTP guards,
  bounded unfinished embedding work, raw adapter response limits and post-lock grants.
- [Manual alpha release tooling](../RELEASING.md): signed annotated tag/exact-CI
  gates, full-history scanning, scoped dependency/container SBOMs, license-gap
  inventory, vulnerability refusal, checksums and pinned attestation identities.
  Independent protected-environment review is required before attestation/publishing.
- All five existing CI jobs are required on main. Package CI also exercises a
  small nonce installation and logical restore; release and Verify pin npm 12.2.0.
- The pinned dependency installer and its cache are mounted only during the
  image build. The runtime keeps installed Python packages and the static UI.
- [Minimal runtime assembly](container-runtime.md) retains the same CPython/glibc
  ABI, required native/TLS/DNS packages and actual package/copyright evidence,
  while removing unused OS tools. Frozen extras are selectable at build time.
  The one-time root setup command grants only CHOWN for private file ownership;
  actual-container refusal and positive controls also run in package CI.
- License inventories merge every scope's declarations and reject conflicting
  package identities. File records have separate counts. Missing optional/platform
  package metadata is fetched with exact-version/hash checks and a terminated,
  reaped subprocess deadline; missing declarations remain explicit gaps.
- Both body guards enforce a ten-second total reception deadline with compact
  byte buffering. MCP retains authentication before reading, and the standalone
  gateway applies HTTP budgets inside its bearer boundary. The runtime limits
  Uvicorn concurrency; an offline pinned-Caddy check verifies header/body budgets
  on both proxy listeners. Customer ingress and load remain separate gates.

## Local verification and measurements

Commits `934876b` and `280cb94` passed all five required Verify jobs, including
**2,655 tests** and the disposable package-install/restore drill. Guarded commit
`7b44663` subsequently passed all five jobs with **2,678 tests**. The 129 focused
body/auth/MCP/transport tests include a negative control that reproduces lingering
HTTP/1 connections without close headers. Each follow-up source increment still
requires its own exact-commit CI run before release. Ruff,
workflow actionlint, shell syntax, lock consistency and diff checks pass.
Terraform validates; generated immutable OIDC Compose settings validate. The
documented container-backed configure command ran offline with synthetic inputs.
These are not fresh customer-cloud or customer-IdP installation receipts.

Isolated base and optional-SHACL wheel/sdist installs, the pinned static web build,
chart lint/package and scoped SBOM generation pass locally. Rebuild the final
artifacts after any source change; a dirty local checksum receipt is explicitly
incomplete and unpublished. License/notice approval cannot be inferred from it.

The final measured image used Python 3.14.8 and Debian PCRE2 10.46-1~deb13u3. Its
local arm64 image identity was
`sha256:03da9bc0463e86df18971dd514ba1ca17485a72722f700e0884cd74f636bac35`.
Docker reported 79,998,683 bytes, down from 102,649,476 before removal of the
build installer/cache; this field does not establish registry download
size or the total VM footprint. The HTTP workload had five synthetic sources,
100 assets, 1,000 fields, ten concurrent clients and 200 timed requests. PostgreSQL,
API and an idle worker used the same nonce installation with no model/customer calls.

| Measurement | Earlier baseline | Trimmed image | Final guarded repeat |
| --- | ---: | ---: | ---: |
| Warm HTTP search p95 | 1,556 ms | 1,515 ms | 1,101 ms |
| Warm scoped context p95 | 2,345 ms | 1,608 ms | 1,392 ms |
| API idle memory sample | 124.6 MiB | 119.6 MiB | 136.7 MiB |
| Worker idle memory sample | 57.64 MiB | 56.5 MiB | 56.58 MiB |
| PostgreSQL idle memory sample | 114.5 MiB | 116.9 MiB | 116.6 MiB |

The proposed search budget of two seconds is met in this declared workload;
the one-second context budget is **not met**. The memory figures are samples,
not peak RSS. The runs are samples on a shared host, not a controlled paired
experiment or proof that image trimming caused a latency change. The same final
image also ran alongside this task's scanner/tests: search p95 was 1,449 ms,
context 2,446 ms and restore 29.562 seconds. The repeat started after those task
processes ended; unrelated host activity remained. Both receipts are retained,
and neither establishes a latency guarantee. The final repeat's after-load
samples were API 220.5 MiB, worker 56.09 MiB and PostgreSQL 128.3 MiB;
no OOM or restart was observed. Startup-to-ready was 4.111 seconds; direct
synchronous refresh took 1.690 seconds. An isolated 565,125-byte logical dump
restored and matched all registry tables in 6.967 seconds,
and the temporary database and test containers were removed.

One earlier final-image measurement attempt failed with a bounded generic error;
its containers were removed. A small diagnostic run and the full retry passed.
The original failure remains unattributed; this is not endurance evidence. These
measurements also do not qualify OIDC/MCP latency, approved semantic accuracy,
source refresh peaks, image download, real cloud sizing, large estates or recovery
objectives. Private receipts are generated by `scripts/measure_installation.py`;
the final repeat is `.local/installation-final-budgeted-quiet-20261004.json`;
the simultaneous-work run is `.local/installation-final-budgeted-20261004.json`.

## Release-blocking evidence and remaining inputs

Strict Python audit checked 107 locked third-party dependencies for every non-dev
runtime extra: zero known findings and zero skips on the evaluation platform.
The full web-lock audit reported zero findings. The fetched pre-increment history
scan checked 92 commits/1,166 text objects with zero unresolved issues; exemptions
are reviewed synthetic literals pinned to exact Git objects and candidate hashes.
Current/final history must be rescanned for the actual release commit.

The container scan **fails**. Updating Python 3.14.7 to the verified 3.14.8 image
and applying the reviewed Debian PCRE2 fix reduced reported matches from 207 to
163. Rescanning the trimmed image preserved these counts while reducing the
Syft component inventory from 4,171 to 2,939. The final Grype 0.120.0 scan has
55 high, 53 medium, 10 low and 45 negligible
match instances across 71 advisory IDs, with no ignored matches. Match counts
do not prove exploitability; three Python match instances advertise fixes only
outside the 3.14 line. The remaining base/runtime matches require source-backed
disposition, replacement or upstream fixes. They were not blanket-allowlisted,
and the manual release pipeline refuses them. The OS/runtime scan is separate
from the clean Python/web dependency audits.
[Python security release](https://www.python.org/downloads/release/python-3148/),
[Debian PCRE2 advisory](https://security-tracker.debian.org/tracker/CVE-2026-103111).

Five source defects were independently identified and locally patched: optional
embedding backlog, legacy decompression budgeting, post-lock grant writes and
direct concept context currentness, plus indefinite slow-body retention. The running source-security review is
**partial**, and the working tree changed during remediation. It is not a sealed
whole-source audit of a frozen release commit. Application slow-body waits now
have deadlines and transport/proxy limits, with focused cancellation/authentication
regressions. The remaining registry/helper/deployment paths and customer ingress
failure/load behavior require final review.

## Follow-up local fixes and measurements

The follow-up base image is
`sha256:8c0d531d3063fc0ea827b2cbf0ed70bff127df9b6045d8e90703ca3f490acd45`,
56,751,863 Docker-reported bytes. The same-tool/database rebuilt baseline was
79,998,789 bytes. The all-five-extras candidate is 189,955,362 bytes; extras remain
optional. Actual-root loader/native/TLS checks and offline rootless/read-only
base/extras checks pass, including Parquet and SHACL without model calls.
No task containers or long-running probes remain after verification.

The follow-up full local suite passed **2,707 tests**, including actual-container
configuration controls with and without CHOWN. Ruff, actionlint, lock consistency,
current-file secret checks and diff checks passed. Exact-commit CI and clean-source
artifact receipts remain required for the release candidate.

Fresh base and extras scans each report **47 matches**, comprising 15 High,
19 Medium, three Low and ten Negligible, across 41 advisory/namespace identities.
There are zero ignored findings. The preceding baseline had 163 matches across
71 identities with 55 High using the same database. Required native-library and
Python matches remain; the release gate still refuses the image. This is a
reduction in actual unused package/file contents, not vulnerability exemptions
or a clean scan. [Runtime details and source-backed limitations](container-runtime.md).

Request-local context assembly removes one redundant full snapshot copy and
discovers cross-source labels/types without projecting every source twice. The
offline same-fixture comparison returned identical packets for all 20 alternating
reads: median assembly 66.30 to 30.40 ms, SQL statements 22 to 16 and snapshot
projections 11 to six. Returned suggestions still receive full current grant,
revision and concept checks; no cross-request cache was added.

The subsequent real HTTP sample retained the same five-source/100-asset/1,000-field,
ten-client/200-request workload. Search p95 was **1,518 ms** and context p95 was
**1,782 ms**. Context still misses the one-second goal and this sample does not
show an end-to-end speedup over the preceding guarded repeat. Task builds/scans
had ended, but the host was still shared. Idle samples were API 136.3 MiB, worker
64.5 MiB and PostgreSQL 90.36 MiB (about 291 MiB combined); after-load samples
were 218.3, 57.85 and 130.7 MiB respectively. No OOM or restart was observed.
Startup took 4.002 seconds, direct refresh 2.410 seconds, and the 565,355-byte
logical dump restored and matched all registry tables in 6.830 seconds.
The temporary database and services were removed. These are sampled values,
not peaks or customer sizing/latency guarantees. The receipt is
`.local/installation-minimal-context-20261004.json`; earlier receipts are retained.

All 17 missing Python packages in the prior bundle received verified locked-wheel
core metadata, but one publisher supplied no license declaration. Metadata
availability does not resolve that license gap. File entries, the OS and generic
application records are separately counted; license texts, platform variants,
copyleft obligations and independent notices/legal approval remain open.

## Remaining qualification inputs

Before public release, provide a monitored security/code-of-conduct route and an
independent eligible maintainer/release reviewer. Review all license evidence,
copyleft obligations and required notices; configure protected tag-only candidate
and publish environments, a trusted tag-signing key, exact legal review and actual
attestation entitlement. Remove administrator bypass only with a working independent
review route. Do not substitute a self-approval for these controls.

Before customer qualification, provide an approved GCP project/region/domain and
restricted IdP/source accounts. Then measure the real install, permissions and
secret rotation, off-host recovery, monitored failure delivery and a seven-day
outage/revocation/drift run. Azure/AWS recipes, native ACL propagation, per-source
worker isolation and representative approved-meaning accuracy retain their own
explicit qualification gates. The [completion plan](platform-completion-plan-2026-10-03.md)
and [public checklist](release-readiness.md) remain the governing acceptance scope.
