# Minimal Python runtime image

The pinned Linux application images apply a bounded temporary-cleanup policy:
`TemporaryDirectory` uses Python's descriptor-safe `shutil.rmtree` and performs
no automatic permission/flag repair. Inaccessible files can remain for explicit
operator cleanup; normal writable-directory cleanup remains supported. The
applier refuses any change to the reviewed CPython 3.14.8 source, records exact
before/after hashes, removes stale bytecode and retains the CPython license.
The default and optional vendor image preserve original package versions and
scanner evidence. This policy does not constitute a vendor security update or
independent advisory disposition, and the strict release gate still applies.
See [the patch notice](../third_party/runtime-patches/NOTICE.md).

The Docker build keeps Python 3.14.8 and the same immutable Debian Trixie/glibc
base. A separate stage assembles the runtime filesystem from CPython, the app,
the static UI and the full Debian packages owning its native libraries. It keeps
certificates, TLS configuration/providers, timezone data, DNS/user lookup files,
upstream copyright files and original selected-package dpkg records. Independent
scanners can still identify the retained OS libraries and their source versions.
The runtime receipt at `/usr/share/knowledge-layer/runtime-files.json` also
records binary and source package identities separately, a SHA-256 of the real
dpkg status file, and hashes of each native library used by the loader. Assembly
and the offline checks verify those hashes against the retained files.

The application runtime removes `pip`, `ensurepip`, their bootstrap wheel payloads
and `poplib` source/bytecode. Package installation belongs to the frozen build;
this service does not implement POP mail. `removed_runtime_code` records the
path and SHA-256 of every removed file. Original distribution metadata, Debian
records, APK database and vendor SPDX SBOMs stay intact. This reduces actual
executable code; it does not alter source-package versions or scanner findings.
Paths that escape the assembled root through symlinks refuse before removal.

The final image has no shell, package manager, Perl, mount/login tools, Node or
`uv`. Run operator commands with `--entrypoint python` and the documented Python
module/script. Compose API health checks and Helm probes already use Python or
HTTP directly. PostgreSQL's shell health check runs in its own image. Build tools
and host-side startup/backup commands remain outside the application runtime.

Every installed native ELF library is checked with `ldd` during assembly, then
the assembled root is exercised with the real loader, binary psycopg, native
Python modules and the default TLS trust store. Missing/ambiguous native-library
ownership refuses the build. The official slim image's already unsupported
Tcl/Tk GUI extension is excluded from this headless linkage check; other stdlib
native extensions remain present. This does not qualify arbitrary third-party
native drivers loaded later with `ctypes`/`dlopen`.

Choose connector extras at build time; the default image stays small:

```sh
docker build --build-arg 'KNOWLEDGE_RUNTIME_EXTRAS=iceberg objects clickhouse snowflake bigquery drive search semantic-validation' \
  --tag knowledge-layer:local-extras .
```

Unknown extra names refuse. The installer uses exact frozen non-dev lock inputs.
The optional-extra local check imports all eight groups, round-trips a small
Parquet table, exercises ONNX's CPU allocation, signs and verifies with the
cryptography native library, checks psycopg linkage, and validates a SHACL graph.
It runs offline and makes no model calls; source-server/warehouse behavior and
embedding-model downloads remain separate qualification steps.

The one-time root configuration command needs `--cap-drop ALL --cap-add CHOWN`
to assign private files to the service UID/GID; [cloud deployment](cloud-deployment.md)
has the full command. The API and worker continue to drop all capabilities.
Run the offline positive and negative controls against a locally built image:

```sh
KNOWLEDGE_RUNTIME_TEST_IMAGE=knowledge-layer:local \
KNOWLEDGE_RUNTIME_EXTRAS_TEST_IMAGE=knowledge-layer:local-extras \
  uv run --no-sync pytest -q tests/test_container_runtime_root.py
```

The controls use `--network none --read-only --cap-drop ALL`, a numeric service
UID/GID, `no-new-privileges`, finite memory/CPU/process limits and a temporary
filesystem. They also exercise the real C++ aligned allocator with overflowing
sizes and a legitimate aligned allocation. These checks establish local runtime
behavior on the tested architecture, not live cloud account access or customer
deployment qualification.

## Optional maintained vendor profile

`Dockerfile.chainguard` is an evaluated alternative, not the default image or a
release-approved replacement. It pins both the public minimal and development
[Chainguard Python images](https://images.chainguard.dev/directory/image/python/overview)
by immutable digest. The tested arm64 runtime is CPython 3.14.8, glibc 2.44-r7 and
OpenSSL 4.0.3. Development tools install the same frozen application lock; the
final image retains the vendor's actual APK package database, original package
SPDX SBOMs, TLS roots and native libraries. It runs as UID/GID 10001 without a
shell or installer executable. The build verifies each ELF dependency with the
actual loader and retains package identity and native-file hash receipts.
The Debian and vendor package inventories still describe their original Python
package, including code omitted from this application runtime.
Runtime assembly removes the installer/bootstrap module payloads, including
the vendor's external pip wheel. Offline controls verify absence in the
application interpreter, system interpreter and a newly created venv without
pip. The tested runtime also uses read-only files, nonroot execution and
operator-controlled images. Arbitrary Python code remains an operator capability;
this is not a Python sandbox.

The supported optional groups for this profile are `iceberg objects clickhouse
bigquery drive search semantic-validation`. Snowflake 4.8.0 with the frozen
pyOpenSSL dependency fails to import against OpenSSL 4 because `PROTOCOL_TLSv1`
is unavailable. This profile explicitly refuses a `snowflake` build instead of
shipping that combination. At the October 5 check, [PyPI's latest stable
Snowflake connector](https://pypi.org/project/snowflake-connector-python/) was
still 4.8.0. A supported upstream TLS-compatible release and renewed native
qualification are required before enabling that extra here. The default Debian
profile continues to support all eight groups.

```sh
docker build --file Dockerfile.chainguard --tag knowledge-layer:vendor-local .
docker build --file Dockerfile.chainguard \
  --build-arg 'KNOWLEDGE_RUNTIME_EXTRAS=iceberg objects clickhouse bigquery drive search semantic-validation' \
  --tag knowledge-layer:vendor-local-extras .
KNOWLEDGE_RUNTIME_TEST_IMAGE=knowledge-layer:vendor-local \
KNOWLEDGE_RUNTIME_EXTRAS_TEST_IMAGE=knowledge-layer:vendor-local-extras \
  uv run --no-sync pytest -q tests/test_container_runtime_root.py
```

An arm64 check does not establish amd64 native compatibility. The public vendor
image's distribution and supported release policy also need operational review
before any default migration.

## October 5 security qualification

Local arm64 builds use Syft 1.54.0 and Grype 0.120.0 with the advisory
database built at `2026-10-05T06:45:38Z`. Scan the actual Docker image, retain both
native Syft JSON and CycloneDX, and use native Syft JSON as the Grype input so the
scan source retains the full image ID:

```sh
syft docker:knowledge-layer:local -o syft-json=runtime.syft.json -o cyclonedx-json=runtime.cdx.json
grype sbom:runtime.syft.json --fail-on negligible -o json
```

The native scan's config digest and rootfs layer IDs are checked against the
Docker inspection and build receipt. Docker's OCI manifest digest can differ
from the config digest reported as the scanner image ID; both are retained.

| Fresh arm64 image | High | Medium | Low | Negligible | Raw matches | Strict gate exit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Default base | 15 | 19 | 3 | 10 | 47 | 2 |
| Default, all eight extras | 15 | 19 | 3 | 10 | 47 | 2 |
| Vendor base | 0 | 6 | 2 | 0 | 8 | 2 |
| Vendor, seven supported extras | 0 | 6 | 2 | 0 | 8 | 2 |

The later headless-terminal trim was rebuilt and scanned for the release target
`linux/amd64` against the same October 5 advisory database. The default and
all-eight-extras images each have **43 raw matches**: 13 High, 17 Medium, three
Low and ten Negligible, with zero ignored; the strict Grype gate exits **2**.
Removing unused CPython `curses`, `_curses`, `_curses_panel` and `readline`
modules also removes their ncurses/tinfo runtime libraries and exactly four
associated scanner matches. Third-party wheel files and Debian package records
are preserved. The 17-test runtime suite, including amd64 connector/TLS probes,
passed; `/api/health` and `/api/ready` returned 200 in both images. The native
Syft and Grype receipts are retained under ignored
`.local/terminal-trim-scan/` as `base-amd64` and `extras-amd64` records. The
remaining 43 matches still block release; this removal is not a security
exception or an upstream package fix.

The refreshed candidate runtime suite passed 16 tests for each profile with its
applicable extras. The default runtime records 455 removed installer/POP files;
the vendor profile records nine. The added controls verify module absence in
the application interpreter, system interpreter and a new venv using symlinks
under the unchanged `noexec` temporary filesystem.
The connector, source-policy and release-boundary regression passed 45 tests;
the host-only installed-google-auth test skipped because that optional extra
was absent from the host environment. The applicable images did import
google-auth. No live accounts or hosted deployment were exercised.

The zero-match gate in effect for this historical scan refused every raw or
ignored match, including Unknown severity. Neither profile had a clean result,
and no ignored matches, VEX declarations or exceptions were added. The current
[container finding policy](container-security-policy.md) retains every raw match
and requires scoped review of serious findings.

The default profile's 15 High matches map to the following retained source
packages. Debian's current Trixie package candidates did not provide newer
compatible versions for these findings at the check:

| High advisory | Installed evidence and status |
| --- | --- |
| [CVE-2026-19499](https://security-tracker.debian.org/tracker/CVE-2026-19499) | `libc6 2.41-12+deb13u4` retains `strfmon`/`strfmon_l`; no supported Trixie fix was available. The alternative vendor [security database](https://packages.wolfi.dev/os/security.json) records its fix at `glibc-2.44 2.44-r4`, below the installed `2.44-r7`. |
| [CVE-2026-5435](https://security-tracker.debian.org/tracker/CVE-2026-5435) | The same libc package retains `libresolv` and `fp_nquery` code. Absence of an unprefixed exported symbol does not prove absence of the deprecated resolver path; unresolved. |
| [CVE-2026-85091](https://security-tracker.debian.org/tracker/CVE-2026-85091) | `zlib1g 1:1.3.dfsg+really1.3.1-1+b1` reports runtime 1.3.1. Upstream's affected nonblocking-write change was [introduced after 1.3.1](https://github.com/madler/zlib/commit/81cc0bebedd935daeb81b0b6e475d8786b51af3d). That source/version disagreement requires independent review; the raw match remains. |
| [CVE-2025-69720](https://security-tracker.debian.org/tracker/CVE-2025-69720) | The two ncurses/tinfo library matches are source-package fanout for `infocmp`, which is absent from the trimmed Debian image. That exact-file evidence is a review candidate, not an exemption. |
| [CVE-2026-95619](https://security-tracker.debian.org/tracker/CVE-2026-95619) | Three GCC 14 source-package matches. The installed C++ library uses `posix_memalign`, excluded by the [upstream fix discussion](https://github.com/gcc-mirror/gcc/commit/59d235ffa5a69231eb42e5290d52dc8c90d28b7a), and the real system allocator refuses nine wrapped sizes. This does not prove the provenance of every wheel's compiled C++ code. |
| [CVE-2026-102010](https://security-tracker.debian.org/tracker/CVE-2026-102010) | Three GCC 14 matches for a [template-header fix](https://github.com/gcc-mirror/gcc/commit/aaa8351f4d2e636f9680a1f0a8ebc2f0a60611e6). Runtime headers are absent, but installed native wheels could contain compiled template code; unproven. |
| [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642), [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408), [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410), [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409) | `libuuid1 2.41.5-0+deb13u1` has four util-linux source-package matches for mount/nsenter paths. The library remains for Python UUID support; `mount` and `nsenter` are absent. Exact-file dispositions still require independent review. |

The vendor alternative has eight raw matches: six Medium and two Low, with no
High. The refreshed immutable vendor profile has
`python-3.14 3.14.8_git20261001-r2` and `glibc-2.44 2.44-r7`; its upstream
package versions still produce the same matches. They
retain the following unresolved qualification work:

| Advisory | Evidence and remaining condition |
| --- | --- |
| [CVE-2026-87910](https://security-tracker.debian.org/tracker/CVE-2026-87910) | Installed tarfile code contains the [upstream first-filter refusal fix](https://github.com/python/cpython/commit/fb2f0bbc3b35264f09cc2cb2934b7987527a6bc2). An offline link-fallback control observes one filter call and no extraction. Review must bind that result to the retained source hash and vendor package; scanner match remains. |
| [CVE-2025-15367](https://security-tracker.debian.org/tracker/CVE-2025-15367) | The upstream module accepts CRLF and emits a second POP command in an offline fake-socket control. Runtime assembly now removes that unused module and its bytecode. System/app/new-venv controls cannot import it; the original source-package match still refuses release. |
| [CVE-2026-12345](https://security-tracker.debian.org/tracker/CVE-2026-12345) | The installed `TemporaryDirectory._rmtree` retains path-based permission reset behavior. The [upstream fix](https://github.com/python/cpython/pull/157580) merged, but the 3.14 backport is still open at the check. This requires a supported vendor/upstream fix or independently verified disposition; symlink-resistant `shutil.rmtree` alone does not establish closure. |
| [CVE-2026-8674](https://security-tracker.debian.org/tracker/CVE-2026-8674) | Resolver search-domain handling is relevant to this runtime. The vendor security database does not identify a fix for this advisory; version 2.44 alone is insufficient proof. |
| [CVE-2026-89092](https://security-tracker.debian.org/tracker/CVE-2026-89092) | The named `nscd` executable is absent from the retained root. Source-package fanout is an exact-file review candidate. |
| [CVE-2026-97399](https://security-tracker.debian.org/tracker/CVE-2026-97399) | The advisory names the Power8 implementation; this tested image is arm64. An architecture-specific disposition cannot qualify other architectures. |
| [CVE-2026-86805](https://security-tracker.debian.org/tracker/CVE-2026-86805), [CVE-2026-95818](https://security-tracker.debian.org/tracker/CVE-2026-95818) | The loader advisories depend on secure/setuid execution. The retained image has no SUID/SGID files, and the controls use nonroot, no capabilities and `no-new-privileges`. These conditions support review of the tested deployment boundary, not removal of the raw loader matches. |

The primary vendor [glibc recipe](https://github.com/wolfi-dev/os/blob/main/glibc-2.44.yaml)
and security database supplement the real installed APK records. A recipe's
current branch or an APK build-configuration commit must not be misrepresented
as proof of every installed upstream source patch. Raw SBOMs, image inspections,
native receipts, tests and strict scan failures are retained separately.

## October 7 release-workflow OCI rescan

At `31f59aeb4bcd2e9101a0fee2d3b4eb05d35b6092`, the local reproduction used
the release workflow's Linux amd64 Buildx commands, pinned Dockerfiles and
`--provenance=mode=max` OCI archive output for both shipped images. Syft 1.54.0
scanned the archives; Grype 0.120.0 used its database built at
`2026-10-07T06:31:48Z`. Native Syft JSON preserved image identities, while the
CycloneDX scans used by the release workflow produced the same advisory and
package match sets. No findings were ignored.

| OCI image | Buildx index digest | Raw Grype JSON SHA-256 | Raw matches | Strict gate |
| --- | --- | --- | ---: | --- |
| Application | `sha256:1a06957d562ac48fab725f5cd3a35909dc48a4882bb7aa6004f75dcb629bd259` | `4c857c19344aba9e39420855ddfd41b6fa0bf49ec3fcd2cd08f88bd2cec7cd62` | 43 | Failed |
| PostgreSQL | `sha256:9d06edae7333044afeebd8c6e5aeea99045f6cc9c8da431fbc76dd2c1ada5960` | `7cbf16244f62932be90eb7a2c10fa668c0adcc31b9563c2f10d02c46b7891b18` | 3 | Failed |

The application result contains 13 High, 17 Medium, three Low and ten
Negligible matches. The PostgreSQL result contains three Medium matches for
BusyBox-family CVE-2025-60876. `scripts/release_manifest.py --check-container-scans`
refused these raw records. The ignored local build metadata, OCI archives, SBOMs
and scans are under `.local/ga-current/`; they are not published or signed
artifacts. A future release commit must rebuild and rescan its own images.

## Nonroot PostgreSQL derivative

`Dockerfile.postgres` pins the official `postgres:18.6-alpine` image by digest
and removes `/usr/local/bin/gosu` from the final runtime filesystem. This helper
switches from root to the PostgreSQL user; the portable installation starts with
an operator-selected nonroot numeric UID instead. A small entrypoint refuses
UID 0 before delegating to the unchanged official entrypoint. Its existing NSS
wrapper handles numeric UIDs without requiring per-install image builds. The
image defaults to UID/GID 10001:10001; the data directory must already belong to
the selected UID/GID. Overriding the image entrypoint remains Docker operator
authority, not an enforced isolation boundary.

The receipt at `/usr/share/knowledge-layer/postgres-runtime.txt` records the
original helper's actual SHA-256 and version, immutable parent identity, original
and updated APK database hashes, and the unchanged upstream entrypoint hash.
The original package inventory is saved separately. The parent is Alpine 3.24.2;
the derivative installs stable `nghttp2-libs=1.70.0-r0`, the supported
`zlib=1.3.2-r1` CVE-2026-85091 backport, and `libcrypto3=3.5.9-r0` and
`libssl3=3.5.9-r0` from Alpine's stable OpenSSL security update. Existing
upstream legal files and actual package records remain intact. The parent SBOM and
scan are retained alongside the derivative's evidence. Immutable parent layers
still contain their original bytes; removal applies to the final executable
filesystem.

```sh
docker build --file Dockerfile.postgres --tag knowledge-layer-postgres:local .
KNOWLEDGE_POSTGRES_TEST_IMAGE=knowledge-layer-postgres:local \
  uv run --no-sync pytest -q tests/test_postgres_runtime.py
```

Three offline container controls passed on arm64: root execution refuses,
and UID/GID 10001:10001 and 501:20 each initialize a database and round-trip a
table and a compressed custom-format `pg_dump`/`pg_restore` through a local Unix
socket. They use a read-only root filesystem,
`--network none`, all capabilities dropped, `no-new-privileges`, finite
CPU/memory/process limits and `noexec` data/temporary mounts. Host data ownership
and existing physical cluster compatibility remain installation/recovery checks.

| PostgreSQL arm64 image | Critical | High | Medium | Low | Raw matches | Strict gate exit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Official pinned 18.6 Alpine parent | 1 | 24 | 24 | 2 | 51 | 2 |
| Nonroot derivative after stable nghttp2 update | 0 | 1 | 3 | 0 | 4 | 2 |
| October 7 candidate after zlib and OpenSSL updates | 0 | 0 | 3 | 0 | 3 | 2 |

All 46 removed Go matches in the earlier derivative were attributed to the
removed helper. The October 7 candidate's three remaining raw matches are
BusyBox-family package records at `1.37.0-r31` for CVE-2025-60876; it has zero
ignored matches. The supported zlib and OpenSSL updates removed those packages'
previous scanner matches. The earlier v3.23 comparison did not assess the
actual parent's branch; qualification now uses the signed stable v3.24 APK
index and installed records. Normal APK removal refuses BusyBox because the
upstream shell and SSL-client packages require it. No unstable packages,
dependency overrides, scanner ignores or package-record deletions were used.
The derivative still fails the unchanged strict release gate.

Syft 1.54.0 and Grype 0.120.0 used the advisory database built at
`2026-10-07T06:31:48Z` for both October 7 local candidates. The native Syft
SBOMs bind the following image config and platform-manifest digests to the raw
Grype JSON. Both scans contain three CVE-2025-60876 matches and zero ignored
matches; the strict gate exits 2. Three nonroot runtime tests passed on each
architecture.

| Local image | Image config ID | Platform manifest digest | Raw Grype JSON SHA-256 |
| --- | --- | --- | --- |
| arm64 | `sha256:c575d3e2bf35a5f0f00b60adee38ce855050c1a039abaf629dc4c363f44958e2` | `sha256:93c4c19d517561df8398217112850d8f544767b2b8a16a90c01861f580af9c26` | `6cc2501243b95d18adaec1114a0d1cb8234084d97e6b3088ca2e6c661f6fad4e` |
| amd64 | `sha256:6a187eafb872a1bc011fcca455b0df653baecfc07fb646a1418ae8397e3ff6db` | `sha256:c693699e79f4660254326ab37f0fc4305ec18d5fb52dc7b11a8073d1f568c975` | `7ebcadeaacd02b9ebb9c7c7dacdf2e50027536e15bdcf56a699df929f10e82de` |

The ignored local receipts are `.local/ga-next/postgres-c85c8ea-arm64.*` and
`.local/ga-next/postgres-c85c8ea-amd64.*`. The amd64 local build used the same
Dockerfile instructions with only its BuildKit syntax comment omitted after
Docker Desktop's frontend fetch stalled. The release workflow must still build
and scan its actual OCI artifact from the exact release commit.

The earlier derivative's local arm64 manifest digest is
`sha256:859b72e5c1e3ed1d243baaeeafd7ac2ea77cbea777976f295d0ac77b36023804`;
its image config ID is
`sha256:364b143b59cef4632cb23f616bdb3e47692d714329c289f3f372b79f56883064`.
A bounded alternative-vendor PostgreSQL evaluation also failed the strict scan
with five raw findings and did not support this arbitrary-UID/NSS-wrapper
installation as a drop-in replacement. It was not adopted. These local arm64
results do not qualify another architecture or a customer database migration.

The October 4 local arm64 comparison used Syft 1.54.0, Grype 0.120.0 and the same
advisory database built at `2026-10-04T08:11:47Z`. The rebuilt baseline had 163
matches across 71 advisory/namespace identities, including 55 High. Both base
and all-extra minimal candidates had 47 matches across 41 identities: 15 High,
19 Medium, three Low and ten Negligible; neither scan ignored findings.
The reduction comes from removing actual unused packages/files, not scanner
exemptions. File inventory counts are not third-party dependency counts.

The release gate still refuses these images. Required glibc, C++ runtime,
compression, UUID, ncurses, SQLite and Python packages retain known matches.
Debian's source-package fanout can report a command vulnerability against a
library: for example its [ncurses advisory](https://security-tracker.debian.org/tracker/CVE-2025-69720)
names `infocmp`, which is absent from this image. That is evidence for review,
not an exemption or a resolved scanner result. The retained
[C++ runtime](https://security-tracker.debian.org/tracker/CVE-2026-95619) and
[zlib](https://security-tracker.debian.org/tracker/CVE-2026-85091) reports still
require upstream/version assessment. Python scanner fixes outside the supported
3.14 line are not automatically installed. No CVE ignores or VEX declarations
were added. This follows [Docker's multi-stage guidance](https://docs.docker.com/build/building/multi-stage/)
and the official image's [native dependency inspection](https://github.com/docker-library/python/blob/master/3.14/slim-trixie/Dockerfile).
