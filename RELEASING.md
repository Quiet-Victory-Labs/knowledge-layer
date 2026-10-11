# Release process

The current `0.1.x` line is an alpha evaluation. Do not describe a tag or green build as enterprise deployment qualification. The [release gates](docs/release-readiness.md) and [current self-host qualification](docs/self-host-qualification-2026-10-05.md) record the present proof and limits.

The six-job Verify run for `5c1befc` passed. Changes after that commit need
their own exact-commit CI and security evidence. The current notice coverage
result and remaining runtime blockers are in the [GA review packet](docs/ga-review-packet.md).

## Prepare

1. Start from a clean checkout of the protected default branch. Review merged changes and update [CHANGELOG.md](CHANGELOG.md), version fields in `pyproject.toml`, `web/package.json`, and `charts/knowledge-layer/Chart.yaml`, and the deployment and migration notes. Keep the versions aligned when shipping one service release.
2. Confirm CI and the local qualification runbook pass. Build the wheel, source distribution, web app, container, and Helm chart from the exact commit. Run migration and recovery drills against an isolated database.
3. Generate base Python, each runtime extra, their union, and web SBOMs with `scripts/generate_sbom.sh`. The release workflow also builds and scans the app and shipped PostgreSQL derivative with pinned Syft, retaining the exact PostgreSQL build identity and SBOM in signed evidence. Strict [pip-audit](https://github.com/pypa/pip-audit) checks exact locked Python runtime versions for every non-dev extra, excluding the local project. Full web-lock `npm audit` and pinned [Grype](https://github.com/anchore/grype) check build dependencies and both actual container OS/runtime SBOMs. The [container finding policy](docs/container-security-policy.md) retains every raw match, blocks ignored matches and unreviewed Critical, High or Unknown findings, and allows exact, evidence-backed, expiring decisions. Medium and lower findings stay visible for maintenance. These checks use the advisory databases available when they run and cannot establish absence of unknown vulnerabilities. Follow the [license inventory commands](docs/dependency-license-review.md) to join declarations from every SBOM scope, matching installed metadata and hash-verified core metadata for missing locked wheels. File records are counted separately from dependency gaps. Unknown licenses, platform variants, required license texts and notices remain explicit gaps; the inventory never constitutes legal approval. Review direct and transitive licenses, notices, known vulnerabilities, vendored material, and data rights, then obtain independent review before public distribution.
4. Review security findings, dependency alerts, connector qualifications, and the deployment limitations. Do not package credentials, `.env`, `.local`, downloaded customer data, or generated test databases.

5. Collect hash-verified archive license, copyright and NOTICE texts using the
   [workflow-matching notice commands](docs/dependency-license-review.md). Include
   the explicitly selected locked Tokenizers source distribution and immutable
   publisher texts/proof files from `docs/publisher-notices.json`. The collector
   hashes and parses the same private archive snapshot without extracting package
   trees or executing code. Frozen Linux requirements supply explicit marker
   exclusions. The selected Python runtime extras and full web build tree must
   pass the independent offline coverage check before signatures are requested;
   the October 8 current-lock selected archive/text coverage check passes. Publisher
   proof consistency does not verify registry attestation signatures or establish
   legal approval. Ship `third-party-notices.tar.gz` and
   `notice-requirements.txt` with the other artifacts. This selected coverage
   check does not approve licenses, settle corresponding-source obligations or
   establish complete container/OS coverage; those remain independent review.

## Tag and publish

1. A maintainer approves the exact commit and release notes. Create a signed, annotated `vMAJOR.MINOR.PATCH` tag on that commit; never move a published tag.
2. Use the manual [OSS release workflow](.github/workflows/release.yaml), selecting the exact tag as the workflow ref and the same tag as its input: `gh workflow run release.yaml --ref TAG -f tag=TAG -f channel=prerelease -f publish=false`. Leave `publish=false` to prepare signed artifacts for review. The protected `oss-release-candidate` environment requires independent approval of the exact tag and workflow before preparation can request an OIDC identity. A guard before checkout refuses dispatch from a branch, even when the input names a valid tag; checkout alone does not change the workflow's certificate identity. Preparation also requires a GPG-signed annotated tag, the explicitly trusted maintainer fingerprint, aligned Python/web/chart versions, main-branch ancestry, a complete fetched-history candidate scan, and the latest successful exact-commit Verify workflow with all six required jobs, including `self-host-integration`. No branch push publishes artifacts.
3. After independent review, opt in with `publish=true`. The separate `oss-release` environment must have independent required reviewers and tag-only deployment rules. Configure its `RELEASE_ENVIRONMENT_READY=true`, `RELEASE_LEGAL_REVIEW_ID` and `RELEASE_LEGAL_REVIEW_COMMIT` only after verifying those controls and recording approval for the exact commit. Supply the matching review reference as the workflow input. The job verifies every staged file's checksum and keyless attestation before copying the exact OCI archive to GHCR, signing and verifying the OCI digest, and creating the requested GitHub release channel. Publication readback must match the tag and prerelease flag. PyPI publication is not configured.
4. Verify published artifacts from a checkout of the exact signed source commit.
   Download them and extract `sbom-bundle.tar.gz` into `sbom/` and
   `third-party-notices.tar.gz` into `notices/` beneath the release directory.
   Run `uv run --no-project --with packaging==26.3 python scripts/verify_release_attestations.py RELEASE_DIRECTORY --repository OWNER/REPOSITORY --tag TAG --commit COMMIT`.
   The notice checker independently compares against that checkout's locks and
   publisher catalogue, revalidating copied notice and binding proof bytes
   offline. This does not turn the publisher consistency proofs into verified
   registry signatures. The release attestation verifier binds
   the control receipt to the expected repository and pins workflow, certificate
   identity, tag, source commit and workflow commit, denying self-hosted runner
   signatures. Verify the image with `cosign verify --certificate-identity https://github.com/OWNER/REPOSITORY/.github/workflows/release.yaml@refs/tags/TAG --certificate-oidc-issuer https://token.actions.githubusercontent.com IMAGE@sha256:DIGEST`.
   Repeat a clean-start smoke against the chosen deployment platform. The
   workflow checks published file readback; it does not claim a deployed
   customer environment.

## One-time maintainer configuration

Set repository variables `RELEASE_GPG_PUBLIC_KEY` (armored public key only) and `RELEASE_GPG_FINGERPRINT` (trusted primary fingerprint). The workflow creates a private temporary GPG keyring; no private signing key belongs in CI. Tag locally with the approved key. Expired/revoked signatures, unapproved fingerprints, lightweight tags, missing or incomplete CI, and version disagreement fail closed.

Create and protect both `oss-release-candidate` and `oss-release` before dispatch; require independent reviewers, restrict allowed release tags, and prevent approval bypass. Set `RELEASE_CANDIDATE_ENVIRONMENT_READY=true` only in the protected candidate environment after those controls are verified. Set the publication ready and exact-commit review variables only in the protected publishing environment. Also protect release tags against update/deletion and restrict their creation to trusted maintainers; require independent review of release workflow, gate scripts and historical exemption changes on the protected default branch. The signed tag executes its own gate code, so trust depends on the maintainer key, reviewed workflow and protected repository/environment policy together. Ready variables are operator acknowledgments, not API proof of repository settings. Review all pinned action commits and required CI job names when changing workflow policy. Neither this document nor the workflow changes repository settings automatically.

Both jobs now read the actual GitHub protections with
`scripts/check_release_controls.py` before building or publishing. Store a
read-only `RELEASE_CONTROLS_READ_TOKEN` with repository Administration and Actions
read permissions in each protected environment. The check requires all six
strict GitHub Actions contexts on `main`, administrator enforcement, stale
review dismissal, code-owner and last-push approval, with no explicit review
bypass allowances. Both environments must name independent individual human
reviewers, prevent self-review and administrator bypass, and allow only the
exact release tag or `v*` tags. Group reviewer membership is not qualified by
this checker. Unknown values and API permission failures refuse; ready
variables cannot replace a successful readback. The signed manifest retains
the preparation receipt, and publication performs a fresh check.

The private development archive has both named environments, but neither has
required independent reviewers and both currently allow administrator bypass.
They do not satisfy the release control checker. Administrator enforcement,
code-owner review and latest-push approval are enabled on `main`; an independent
eligible code reviewer still needs to be added. The public source alpha uses a
fresh sanitized history and has a separate [source-publication record](docs/public-alpha-readiness.md).
Release environments and secrets were not copied to the public source repository.
Independent review, security-contact monitoring and source/fixture and dependency
rights review remain requirements for the signed binary release lane. Source
publication does not approve that lane. No subscription upgrade is part of this
publication.

## Stable channel

The default channel is `prerelease`; the current `0.1.0` Alpha cannot pass the
stable gate. A future `channel=stable` dispatch requires all aligned versions to
be at least `1.0.0` and the sole Python Development Status classifier to be
`5 - Production/Stable`. This metadata change alone is insufficient: complete
the product, security, legal and deployment qualifications, then obtain an
independent GA review for the exact signed source commit.

In **each** protected environment, set `RELEASE_GA_ENVIRONMENT_READY=true`,
`RELEASE_GA_REVIEW_ID` to that review reference, and
`RELEASE_GA_REVIEW_COMMIT` to the full approved commit SHA. Supply the matching
`ga_review_id` input for both preparation and publication. Stale or missing
approval refuses. These variables acknowledge a maintainer's verified controls
and recorded human approval; they are not automatic proof of independent review.
No such approval is currently configured by this implementation.

The release gate, signed manifest and staged artifact name bind the channel,
production metadata and review to the exact source commit. Publication rechecks
them against the protected environment and verifies the resulting GitHub
prerelease flag. Stable publication cannot promote previously prepared
prerelease artifacts. Both channels retain the same signed-tag, exact-commit CI,
risk-reviewed vulnerability, license and provenance requirements.

GitHub keyless [artifact attestations](https://github.com/actions/attest) are available on public repositories; private/internal repositories require GitHub Enterprise Cloud and GitHub Enterprise Server is unsupported. Preparation fails if attestation capability is unavailable. Do not silently skip signatures to publish from an unsupported private plan. [GitHub CLI verification](https://cli.github.com/manual/gh_attestation_verify) and [Sigstore verification](https://docs.sigstore.dev/cosign/verifying/verify/) describe the trust checks. An attestation proves a particular workflow produced particular bytes; it does not establish license approval, model accuracy, reproducible builds or enterprise readiness.

The OCI artifact currently targets `linux/amd64`. ARM64 deployment and additional platform license inventories need their own build/smoke evidence before support claims. Partial publication can occur if registry or GitHub operations fail after an image push; investigate by immutable digest, never move a published tag or overwrite an existing release to conceal a failed run.

The publish job refuses an existing GitHub release or GHCR image tag. Only a
confirmed GitHub 404 and registry `manifest unknown` response establish absence;
auth, network and ambiguous errors fail closed. This preflight cannot make two
external publications atomic. Resolve any partially published artifact deliberately
before dispatching again, with its original signed tag and recorded digest.

For the first public release, enable GitHub's private vulnerability reporting, publish a monitored security contact, complete independent license and data-rights review, and update the public support policy. GitHub's private vulnerability reporting is a public-repository feature and is not a substitute for the private-phase contact route.
