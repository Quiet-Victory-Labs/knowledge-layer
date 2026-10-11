# Repository operations

The public source repository is [Quiet-Victory-Labs/knowledge-layer](https://github.com/Quiet-Victory-Labs/knowledge-layer).
It is an Apache-2.0 source alpha. Public history begins with the sanitized source
snapshot prepared on October 8, 2026; original development and private pilot
history are preserved in a separate private repository. Do not push private
branches or tags into this public repository. No package, image, chart or
GitHub release is published automatically.

## Public repository controls

`main` requires one approving pull-request review, dismisses stale reviews,
requires code-owner review and approval of the latest push, and enforces these
controls for administrators. The branch must be current and pass all six GitHub
Actions checks: `python`, `package`, `local-integration`, `oidc-integration`,
`autonomy-integration` and `self-host-integration`. Their required identities
are bound to GitHub Actions app 15368. Linear history and conversation resolution
are required; force pushes and deletion are disabled.

Eric Kittelson (@erickittelson) is the maintainer and sole
[CODEOWNER](../.github/CODEOWNERS). An independent eligible reviewer must be added
and included in ownership before maintainer-authored changes can satisfy the
required approval. Public source initialization is not independent review.

GitHub private vulnerability reporting, secret scanning, push protection and
Dependabot security updates are enabled. The [local secret candidate check](secret-scanning.md)
also runs in CI with explicit limits. These checks do not prove absence of
secrets or replace source-permission review. The alternative security email
route still needs its delivery, monitoring and acknowledgment check; use
[private GitHub reporting](../SECURITY.md) in the meantime.

## Release separation

Release environments, secrets and variables from the private development
repository were not copied. Signed binary preparation and publication require
independent reviewers, environment restrictions, source/fixture and dependency
rights review, and the artifact qualifications in
[RELEASING.md](../RELEASING.md). A public source alpha does not satisfy them.
The [actual control checker](../scripts/check_release_controls.py) must read back
qualified settings; a ready variable cannot replace its receipt.

The October 8 offline notice check covers 271 selected dependencies, 272
hash-verified archives, 208 distinct texts and 64 publisher proof files, with no
missing notice texts or source pointers. This inventory does not independently
settle rights, license compatibility, corresponding-source obligations or
container OS coverage. See the [source-alpha record](public-alpha-readiness.md).

Before merging or releasing, read back the actual commit, required checks,
review, protections, visibility and security route. Local tests and green CI on
an earlier commit do not cover new changes.
