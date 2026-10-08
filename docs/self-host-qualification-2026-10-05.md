# Portable self-host qualification, 2026-10-05

The clone → start → connect → agent draft → human accept/publish → agent consume
flow passed a real, disposable local installation drill. Complete cold backup
and recovery also passed. This is progress toward GA; the strict container scan,
independent review and public reporting gates still block release.

## Customer flow and recovery

[`scripts/drill_self_host.py`](../scripts/drill_self_host.py) passed **31 checks**
in 71 seconds on macOS arm64 with Linux Docker containers. The working tree was
based on `983c2e1f14ba4ca0fcad763b1f5fe2eb72450eb9`; runtime changes were not yet
committed or signed when these images were built. The receipt records that dirty
state, the driver hash and the exact image IDs used:

| Component | Local immutable image ID |
| --- | --- |
| Default application | `sha256:149bd8a87e09661ad72b2de18e72de5762992e5f07ebeedb6b1b1db960516016` |
| Nonroot PostgreSQL derivative | `sha256:bc5651681fb640c934ce3e5d88a2862acc10a50bb5abceff7cbfab6b41e143d5` |

The drill uses a CSV and a Markdown document, a real queued refresh worker,
native MCP JSON-RPC and separate owner/agent credentials. It verifies private
installation files, fresh owner claim, CSRF/Origin and disabled-connector
refusals, bounded durable builds, lease recovery after restart, idempotent
checkpoints, pending drafts and explicit abstentions. Agents cannot accept or
publish. A changed review digest refuses; the owner accepts an exact selection
and agents retrieve the published revision pins.

The owner's login and accepted context survive an ordinary restart. Cold backup
then captures the PostgreSQL files, installation configuration, owner login,
source definitions and credentials. Restore creates a separate installation
with the exact recorded app/PostgreSQL images and a rotated browser session
version. The original browser session refuses; a fresh owner login succeeds and
native MCP returns the same approved assertions and publication pins. Withdrawal
removes agent readiness immediately. Logout revokes browser authority.

The recovery implementation rejects traversal, links, special files, duplicate
members, unsupported platforms, changed hashes, oversized archives and partial
manifests. An independent bounded code review found no actionable issue; its
offline setup/recovery suites passed 77 tests. It was not a whole-source audit.
Backup is a stopped, physical PostgreSQL 18 backup for the same Linux image
architecture. External mounts, off-host disaster recovery and customer RPO/RTO
are outside this proof; follow the [operator runbook](self-host.md).

All three client configuration commands ran under isolated standard-library-only
Python and produced no embedded credentials. This certifies generated Codex,
Claude and Cursor configuration plus the native protocol path. It does not
certify inference or a complete run inside all three paid agent clients; their
earlier native outcomes remain in the [enterprise qualification](enterprise-qualification-2026-10-05.md).

## Local footprint and package checks

The full Python suite passed **3,257 tests, ten skipped**. The three PostgreSQL
container tests were separately exercised with the actual image and passed;
they otherwise skip without an explicit image. Actual default/all-eight-extra
and vendor/seven-extra runtime controls each passed 16 tests. Ruff passed.
Fresh wheel and source archive base installations, optional SHACL and packaged
consumer/conformance checks passed. The source archive includes the recovery
helper, repeatable customer drill and PostgreSQL Dockerfile.
After the final PostgreSQL release-gate increment, combined setup, recovery,
distribution, runtime and release suites passed **263 tests, seven skipped**,
including all 50 release-engineering tests.

A second bounded HTTP measurement resolved both input tags to immutable image
IDs before creating any service. Two synthetic sources, ten assets and 100
fields produced ten search and ten context requests at concurrency two: warm
search p95 93 ms and context p95 84 ms. After-load memory samples were 125 MiB
for the API, 87 MiB for PostgreSQL and 58 MiB for the idle worker; none restarted
or was OOM-killed. A separate logical restore check passed. These small samples
do not qualify peak refresh memory, broad source estates, customer latency,
endurance or approved semantic accuracy.

## Release status

The new `self-host-integration` CI job builds both source images and runs the
container controls, bounded HTTP measurement and complete customer/recovery
drill. GitHub `main` protection was updated and read back: all six jobs are
required on an up-to-date branch, bound to Actions app 15368. A configured job
and local proof do not replace successful CI on the final release commit.
Release preparation separately builds the PostgreSQL derivative for amd64 and
requires its risk-reviewed scan, build identity, SBOM and license evidence before
attestation. The derivative OCI stays transient; the signed evidence is retained.

Fresh strict Syft/Grype scans use a 2026-10-05 database, negligible cutoff and
zero ignore rules:

| Runtime | Raw findings | Release gate |
| --- | ---: | --- |
| Debian app, default or all eight extras | 47, including 15 High | Refuses |
| Optional vendor app, default or seven extras | 8, six Medium and two Low | Refuses |
| Official PostgreSQL parent | 51, including one Critical | Refuses |
| Shipped nonroot PostgreSQL derivative | 5, one High and four Medium | Refuses |

The derivative removes only the unused `gosu` executable and records its hash;
original APK inventory, licenses and upstream entrypoint are retained. All 46
removed Go findings belonged to that removed executable. Its remaining zlib,
nghttp2 and BusyBox findings are unresolved. The vendor app still rejects the
frozen Snowflake extra because of OpenSSL incompatibility. See the precise
limits and primary advisory links in [container-runtime.md](container-runtime.md).
These arm64 results do not establish amd64 scan results.

These image identities and five-finding PostgreSQL result describe the initial
local qualification above. The later [hosted qualification follow-up](ga-qualification-2026-10-05.md)
corrects two CI failures, applies the supported stable nghttp2 update, and records
the refreshed PostgreSQL derivative's four raw findings. It does not rewrite
the original image's receipt or qualify the final release commit automatically.

No security exception, ignored match, unsigned release or public GA label was
introduced. Independent legal review, an independent release reviewer, a tested
monitored security contact, exact-commit CI and trusted signed release approval
remain required. Customer IdP, native source ACL and provider/harness certification
remain scoped qualifications rather than a promise that every deployment is
enterprise ready.

The drills invoked no models, created no cloud resources and verified removal of
their own containers, credentials, source files, PostgreSQL data and image
aliases. Unrelated services were preserved. Safe local evidence can be regenerated
with the commands in [CONTRIBUTING.md](../CONTRIBUTING.md); no customer records or
credentials are included in this report.
