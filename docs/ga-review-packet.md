# GA review packet

> October 8 source-alpha update: the [public-source checklist](public-alpha-readiness.md)
> supersedes older source publication/control status below. Main now enforces
> administrator protection, code-owner review and latest-push approval. Both
> release environments exist but have no independent reviewers and allow admin
> bypass. Current-lock notice coverage passes for 271 selected dependencies and
> 208 texts, with no text/source-pointer gaps. This is inventory evidence, not
> rights or legal approval. Historical image scans below remain historical.


This packet prepares a concrete review; it does not grant approval. The release
commit must have all six Verify jobs green, complete signed artifact evidence,
resolved security findings and recorded independent review before publication.
A six-job [Verify run](https://github.com/Quiet-Victory-Labs/knowledge-layer/actions/runs/37668706573)
passed for `31f59aeb4bcd2e9101a0fee2d3b4eb05d35b6092` with 3,522 Python
tests passing and eight skipped. A local reproduction of the release workflow's
Linux amd64 OCI builds at that commit yielded 43 raw application matches and
three raw PostgreSQL matches using Syft 1.54.0, Grype 0.120.0 and the October 7
advisory database. The strict release manifest check refused both scans. This
validation does not approve release. No independent
human license or GA review has been recorded. Current package metadata remains 0.1.0 Alpha;
the stable-channel mechanism does not make this an approved stable release.

## Measured notice evidence

The pending production candidate's locked web and Python union SBOMs contain
**151** and **122 packages**, respectively. Its collection covers **270 locked
archives** for **269 selected dependencies**, including Tokenizers' additional
locked source distribution. It emitted **206 distinct license/notice texts**
totaling **879,591 bytes**, with **64 publisher proof files** totaling
**321,344 bytes**. The independent offline checker passes with **zero missing
selected notice texts or source pointers**. The UI dependency change removed
the package behind the prior npm notice gap. The refreshed bundle is retained
under ignored `.local/ga-next/notices-mui-complete/`.
The prior `50803bb` tree's 415 archives, 414 dependencies, 265 texts and eight
notice gaps are historical. Read the exact scope and commands in
[dependency-license-review.md](dependency-license-review.md).

The collector hashes and parses the same private bounded archive snapshots,
verifies wheel/source/npm identities against the locks, and binds copied text
hashes to archive receipts. Supplemental publisher texts retain immutable source
and version associations plus their proof bytes for independent offline
revalidation against the checked-in catalogue (SHA-256
`5135618ba8f93328f71d20604d948a414527c499a3109dfd51e4fb086feb4a5e`).
Registry attestation consistency is not cryptographic signature verification:
`attestation_signatures_verified=false`. Collection has byte, item and time
budgets and neither extracts package trees nor executes package code. The frozen
Linux amd64 CPython 3.14.8 markers explicitly exclude `colorama`,
`httpx2-jsfetch`, `pywin32` and `win32-setctime`; missing wheels are not exclusions.
The web scope retains its complete lock inventory, including platform variants.
Copied texts and publisher source pointers do not establish legal approval,
complete OS/runtime notices or verified corresponding-source obligations. Both
`legal_approved` and `notices_complete` remain false.

The prior `react-remove-scroll-bar@2.3.8` notice request is no longer applicable
to the locked production tree. The comparison Graph Lab and its G6, Reactodia,
Graph Explorer and Bergos packages were also removed; the prior lab remains in
Git history at `50803bb`. The catalogue has no unresolved selected package.
The previous full notice bundle had 21 source-pointer gaps. Nineteen now have
catalogue entries bound to exact-version npm registry `gitHead` values and
matching immutable license bytes. The remaining two now have annotated release
tag, versioned manifest and license proofs. The refreshed full archive bundle
and its independent offline checker pass with **zero source-pointer gaps**.
These pointers do not prove reproducible builds or satisfy corresponding-source
obligations. Legal review must still assess source obligations, OS inventory
and fixture/data rights.

## Private report route test: pending

The existing [QVL website](https://www.quietvictorylabs.com/) publishes
`hello@quietvictorylabs.com`; [SECURITY.md](../SECURITY.md) names it and the current
repository administrator. To verify it, send this content-free message and
confirm private receipt and acknowledgment from the responsible maintainer:

```text
To: hello@quietvictorylabs.com
Subject: Knowledge Layer security reporting route test

This is a delivery and monitoring test, not a vulnerability report.
Project: Quiet-Victory-Labs/knowledge-layer
Release commit: FULL_COMMIT_SHA
Test reference: OPERATOR_UNIQUE_REFERENCE

Please acknowledge receipt privately, name the monitoring owner and confirm
the escalation route for future confidential reports. No credentials or source
records are included.
```

Record the reference, full commit, delivery and acknowledgment times, monitoring
owner and escalation contact in the private release review. An address on a
website or a successful DNS lookup is not delivery/monitoring proof. No test
message was sent, and this reporting route remains untested. A sender must be
authorized before an agent sends it. After the repository becomes public, enable and test
[GitHub private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)
and the administrator's security-alert notifications. Preserve this email route
during that change.

## Independent review

An independent reviewer has not been established. Name an independent human
GitHub reviewer with repository access and add them to the appropriate CODEOWNERS
rules. Review the exact commit, locked notices
bundle and corresponding-source obligations, the project/Ossie notices and
synthetic fixture rights. Package license declarations and copied texts do not
settle compatibility, source offers or redistribution obligations. OS/runtime
copyrights are retained in the image; the archive collector's package coverage
does not claim a complete OS/corresponding-source review.

The retained `fa49590` supported-package recheck found no newly supported compatible
patched candidate at that time. Later Alpine stable updates were installed in
the local PostgreSQL derivative and rescanned. The rebuilt Linux amd64 application
profiles each have 43 raw matches after unused terminal code and libraries were
removed; both still fail the strict gate with zero ignored. The earlier vendor
profile had eight and the historical PostgreSQL derivative four raw matches.
The October 7 local arm64 and amd64 PostgreSQL candidates each have three
BusyBox-family raw matches after supported zlib and OpenSSL updates. These image
identities and controls remain separate; see
[GA qualification](ga-qualification-2026-10-05.md) and
[container-runtime.md](container-runtime.md).

The runtime change bounds temporary cleanup to descriptor-safe removal
without permission/flag repair. It binds exact reviewed CPython 3.14.8 source
bytes and retains the upstream license. Package versions are unchanged. The
separate headless-terminal trim removes four raw ncurses/tinfo matches from the
default application profiles; it does not update remaining packages or approve
an advisory disposition. Each proposed release commit requires exact-commit CI
evidence.

The concrete package blockers are:

- Alpine 3.24 now publishes `zlib=1.3.2-r1` with a backport of the upstream
  [df84af25 fix](https://github.com/madler/zlib/commit/df84af25dc1942490e1d1c899a07619152a46148)
  for CVE-2026-85091. The PostgreSQL derivative pins that update and
  [Alpine's stable OpenSSL `3.5.9-r0` security update](https://raw.githubusercontent.com/alpinelinux/aports/3.24-stable/main/openssl/APKBUILD).
  Its local arm64 and amd64 builds passed runtime checks and raw rescans with
  three BusyBox-family matches each. The exact release-workflow OCI build on
  Linux amd64 also has three raw matches; no release was published.
- Stable BusyBox `1.37.0-r31` still includes affected wget code. Related package
  matches require exact source/file review; no match was suppressed.
- The [CPython 3.14 backport PR 158430](https://github.com/python/cpython/pull/158430)
  remained open at the recheck. A merged fix on main does not patch the shipped
  3.14 runtime.
- GCC `14.4.0-2~bpo13+1` was only queued in `backports-new` for amd64. It was not a
  published supported stable candidate or a tested installed replacement.

Other Debian/vendor application findings require supported updates or exact
independent applicability assessment. No independent disposition or exception is
approved by this packet. The later [container finding policy](container-security-policy.md)
replaces the zero-raw-finding gate while continuing to block unreviewed High
matches and preserve every raw scanner result.
The [October 7 High finding triage](container-high-triage-2026-10-07.md)
records static evidence for all 13 current application High package matches:
eight are `not_actionable` review candidates for that saved amd64 image and five
still need caller/provenance review. It grants no release decision; an exact-commit
scan and independent review remain required.
The [historical 47-row container finding queue](container-finding-review-2026-10-05.md)
binds every earlier application match (identical across base and all-extra
profiles) and PostgreSQL match to an unsuppressed scan SHA-256, exact package
identity, reviewer track and an explicit `needs_review` verdict. It prepares
source/control/sink review; it does not issue VEX or change the scanner gate.
A read-only comparison against an already cached official `postgres:18.6` Debian
arm64 image produced 375 raw matches with Syft 1.54.0 and Grype 0.120.0 using
the 2026-10-05 advisory database; it was not pulled, built or deployed for this
task. That comparison did not solve the four earlier Alpine-image matches, so
the shipped PostgreSQL base was not changed.

Record the legal and GA review references against the full source SHA as
described in [RELEASING.md](../RELEASING.md). Do not set ready variables until the
corresponding work and actual protections have been verified.

## Current repository and pilot preflight

At `054b858`, the tracked-file secret candidate scan passed for 515 files. The
full reachable Git-history scan passed with zero unreviewed issues across 106
commits and 1,595 scanned text objects; it retains 17 previously reviewed
historical candidates and still marks customer data rights as requiring
independent review. The selected locked-archive notice checker passed offline
for 269 dependencies and 206 texts, while explicitly leaving container OS
coverage, corresponding source and legal approval unresolved. These are
technical inventory results, not legal sign-off. New commit contents require a
fresh history and notice check at release.

The independent rights review has concrete inputs: the pinned Apache Ossie
development schema and upstream NOTICE, the shipped `psycopg`/
`psycopg-binary` LGPL-3.0-only distribution question, the 269-dependency
notice bundle with 64 publisher proof files, retained OS package license
files, and the declared synthetic Northstar, Juniper and 80-question benchmark
fixtures. The benchmark README calls its corpus generated and public, but
the repository's assertions do not independently prove authorship or
redistribution rights. A human reviewer must record the final source/fixture
rights and corresponding-source obligations for the exact release artifacts.

The Quiet Victory Labs GCP inventory found no existing BigQuery dataset,
Cloud Run service/job, Cloud SQL instance or Artifact Registry repository to
reuse for the customer-like pilot. The BigQuery and IAM Credentials APIs are
enabled, but there is no currently identified restricted source table, IdP
client or deployed HTTPS endpoint. On 2026-10-07, a disposable live drill
created two empty BigQuery tables and a metadata-only service account. The
active operator account was denied `iam.serviceAccounts.getAccessToken` after
the temporary Token Creator binding and bounded propagation retries, so the
restricted connector did **not** reach a live metadata read. The drill's
unsupported `gcloud --scopes` call was repaired to use the fixed IAM Credentials
API endpoint with a ten-minute BigQuery-readonly scope; focused transport and
failure tests pass, but live issuance remains unqualified. No query jobs or
application-row reads ran. All ten disposable datasets and test identities
from the diagnostic attempts were verified absent after cleanup; no test
resource remains. A synthetic replacement would repeat earlier local/metadata
proof, not qualify a real restricted-source and IdP journey. The next pilot
needs a working scoped source identity, an IdP application for the chosen HTTPS
origin, and a bounded host with owner-only secrets and isolated source workers.
No source records or credentials belong in this review packet.
The project's domain currently has a Google MX record; that only supports a
mail-routing configuration and does not prove that the published security
inbox delivers, is monitored, or has an escalation owner. The content-free
test above remains pending an authorized sender and private acknowledgment.

## Historical private-repository controls and cost

This section records the private development repository's preflight before
public source publication. The source alpha now uses a separate sanitized
public history; see [current repository operations](repository-operations.md).
The historical refusals below remain binary-release evidence, not a claim that
the current source repository is private.

Run the read-only actual-control checker with an authorized read token:

```sh
python3 scripts/check_release_controls.py \
  --repository Quiet-Victory-Labs/knowledge-layer --actor MAINTAINER_LOGIN \
  --tag vMAJOR.MINOR.PATCH --output /absolute/private/release-controls.json
```

The actual `v0.1.0` read-only preflight refused **main protection**,
**oss-release-candidate** and **oss-release**. Its receipt records
`settings_changed=false` and `human_approval_inferred=false`. Current
unqualified release environments and missing independent review remain blockers;
unknown settings or API denials also refuse. The supported policy uses named
independent individual reviewers, prevents self-review and administrator
environment bypass, and permits only release tags. A ready variable cannot
replace the actual readback. Give `RELEASE_CONTROLS_READ_TOKEN` read access to
repository Administration and Actions in both protected environments; no private
signing key belongs in CI.

The private GitHub Team repository could not supply required environment
reviewers or private artifact attestations on this plan. Environment secrets on a
private Team repository do not provide the required independent-review gate.
Private/internal artifact attestations require GitHub Enterprise Cloud. GitHub documents
[environment reviewer availability](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments)
and [attestation availability](https://docs.github.com/en/actions/concepts/security/artifact-attestations).
The OSS public repository can use these features without an Enterprise upgrade.
The source publication preserves the original private history and does not copy
its release secrets or environments. Signed binary publication still requires
the security, rights, contact and independent review qualifications in this
packet. Public source availability does not infer those approvals.

## Retained operator evidence

These retained local paths identify this run's evidence; they are not shipped
artifacts or portable public links. Preserve them with the exact-commit review
record. Remove task-owned temporary archive downloads after verification.

- Candidate notice manifest: ignored `.local/ga-next/notices-mui-complete/third-party-notices.json`
  (SHA-256 `323b17cfb096df766139804684c495be47ea39c29d75e24949ec718f32b3451d`).
- Candidate locked archive receipt: ignored `.local/ga-next/archive-receipt-mui.json`
  (SHA-256 `9f657ff2f417ea35e2ced5afac30d35ced5467e6f2be4bb04de63e36f75bcaae`).
- Candidate publisher text/proof receipt: ignored `.local/ga-next/publisher-notices-mui-complete/publisher-notices.json`
  (SHA-256 `5fc3828ba7b1309b55296541aacb8bf1d91f2d448a7b0f4eb3b7d0528dd9e1d2`).
- Protection preflight: `/Users/erickittelson/qvl-code/knowledge-layer/.local/release-review-20261005/release-controls-final.json`.
- Qualification receipt: `/Users/erickittelson/qvl-code/knowledge-layer/.local/release-review-20261005/qualification.json`.
  It retains the prior CI allocation failure separately from current notice and
  local test evidence. Read its exact source SHA and current CI status; pending
  or failed CI never supplies complete qualification.
- Earlier six-job CI baseline: `/Users/erickittelson/qvl-code/knowledge-layer/.local/release-review-20261005/baseline-exact-ci.json`.
- Supported-package recheck: `/Users/erickittelson/.codex/state/plugins/codex-security/scans/knowledge-layer/artifacts-d231d5fc58a442fce1cbe74b66bf6c27f870719ac56f830867219e83e1116fbe/artifacts/runtime-ga-fa49590/qualification.md`;
  `residual-finding-inventory.json` alongside it binds every retained match to
  the original scan-file hash.
