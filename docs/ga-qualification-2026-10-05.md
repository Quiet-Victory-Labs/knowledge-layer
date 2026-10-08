# GA qualification follow-up, 2026-10-05

The first hosted six-job Verify run exercised commit
`252cc348a6553d72be0630926ae5948bc9c25070` on Linux amd64.
[Run 37354031219](https://github.com/Quiet-Victory-Labs/knowledge-layer/actions/runs/37354031219)
failed overall: package, local integration, OIDC integration and the complete
self-host integration passed; autonomy integration and Python failed. This
report records those failures and their corrections. The corrected commit
`fa4959051467e323544bf63959fe2b4742282fa6` subsequently passed all six jobs in
[run 37356861134](https://github.com/Quiet-Victory-Labs/knowledge-layer/actions/runs/37356861134),
with 3,308 Python tests passing and seven skipped, including the complete Linux
amd64 self-host flow. That confirms this baseline's CI, not a public release or
resolution of its runtime findings. The later notice and protection commit
`142af158cf2f7081713f65ad346905bf725c0a93` passed five jobs in
[run 37362862649](https://github.com/Quiet-Victory-Labs/knowledge-layer/actions/runs/37362862649)
after one retry of failed jobs. Python reported 3,403 passing tests and seven
skipped; self-host qualification passed all 31 checks. Autonomy never acquired
a hosted runner and was cancelled by the provider, so the overall run failed
and the exact-commit gate correctly refused. Subsequent changes require their
own complete six-job result.

## Publisher evidence and cleanup policy

The updated collector inspected 415 locked archives for 414 selected
dependencies, including the exact locked tokenizers source archive. Pinned
publisher texts reduce the missing-notice packages from 28 to eight. Two
collections matched all 305 retained files byte for byte: 265 distinct license
texts and 39 publisher/source-binding proofs, plus the manifest. The independent
coverage check still refuses the eight gaps; corresponding-source, container/OS
and independent legal review remain unresolved. Publisher proof content is
validated against the catalogue; registry attestation signatures are not verified.
See [dependency license review](dependency-license-review.md) for scope and commands.

The application images now apply a narrowly scoped temporary-cleanup policy to
the exact reviewed CPython 3.14.8 source. It removes automatic permission repair
and delegates to descriptor-safe removal. Inaccessible temporary files can require
operator cleanup. Original package versions and scanner matches remain unchanged;
this is not an upstream release or an independent advisory disposition.
The source and unmodified license are hash-bound in the build receipt.

Local focused verification passed 414 tests with five skipped. A bounded local
derivative of the earlier default image passed 15 runtime tests, with the optional
extra-image check skipped; this proves policy installation and runtime behavior
in that fixture, not a fresh full application image or scan. Isolated wheel and
source installs, optional SHACL validation and the packaged consumer example
also passed. Full Linux image builds and the six CI jobs remain exact-commit
requirements. The temporary image and 562,714,531-byte archive pool were removed;
small private receipts and notice/proof files remain in the operator review folder.

## Corrected qualification failures

The inventory and refresh drills compared physical schema state to a stale
`0015_inventory_concepts` literal after migration 0016 had become current.
They now compare the actual queried revision against the installed migration
head and retain physical schema matching. The corrected disposable PostgreSQL
autonomy drill passed 132 checks across autonomy, inventory, refresh and native
semantic qualification. No model or customer account was used.

The hosted Python suite passed 3,282 tests and skipped seven, but one hostile
PAX-header test failed because archive-parser behavior differed across managed
Python builds. Restore now validates fixed-size USTAR headers before invoking
the parser, refusing PAX, GNU long-name/long-link and sparse extensions before
reading their declared payloads. Normal backup/restore remains compatible.
The focused recovery, autonomy, distribution and release suites passed 245
tests; Ruff passed. These local results do not replace final exact-commit CI.

CI failure diagnostics now contain only fixed phase keys, numeric counts and
failed-check indexes. Private driver details and synthetic source values are
not printed. Verification preserves the real failed phase instead of treating
a green narrower suite as a successful full run.

## Runtime and release channel

The official PostgreSQL parent is Alpine 3.24.2. The earlier stable v3.23 package
comparison did not assess its actual branch. The refreshed derivative applies
the signed stable v3.24 `nghttp2-libs=1.70.0-r0` update, saving the original APK
inventory and recording both original and current hashes. Three real offline
container controls passed, including arbitrary-UID initialization, root refusal
and compressed custom-format dump/restore.

The October 5 local arm64 scan had four raw matches: one High zlib and three Medium
BusyBox package matches. It ignores zero findings and still exits 2 under the
unchanged strict gate. Default/all-extra application and optional vendor results
remain the earlier 47 and eight matches; no new application scan is claimed.
A vendor PostgreSQL alternative failed both the strict scan and this portable
arbitrary-UID installation and was not adopted. See
[container-runtime.md](container-runtime.md) for exact identities and limits.

On October 7, local arm64 and amd64 derivatives with supported zlib and OpenSSL
updates each passed three nonroot runtime controls and had three unsuppressed
BusyBox-family matches with the newer advisory database. The strict gate still
exited 2. These local results do not qualify the release OCI artifact.

The manual release workflow now supports an explicit stable channel. It requires
aligned >=1.0 production metadata and independent GA review bound to the exact
commit in both protected environments. Gate receipts, signed manifests,
preparation and publication readback bind the channel; prerelease artifacts
cannot be promoted through that route. The current 0.1.0 Alpha stays a
prerelease. No stable approval, signed release or public distribution was made.
See [RELEASING.md](../RELEASING.md) for the operator configuration.

## Remaining launch gates

Resolve all strict runtime findings, complete independent license/notices and
fixture-rights review, establish a monitored security-reporting route and obtain
an independent release reviewer. Protected exact-commit CI and signed release
approval remain mandatory. Customer IdP, native source ACL, deployment/load and
paid agent-client qualification remain scoped in the existing runbooks.

Testing created no cloud resources and invoked no hosted models. Local probes
use disposable fixtures and bounded resources. Only task-owned resources are
removed; existing user services and images are preserved. Hosted Verify uses
standard Linux runners under the organization's existing Actions spending cap.
