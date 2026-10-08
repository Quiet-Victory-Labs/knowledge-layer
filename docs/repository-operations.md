# Repository operations

The current source repository is [Quiet-Victory-Labs/knowledge-layer](https://github.com/Quiet-Victory-Labs/knowledge-layer).
It remains private during public-alpha preparation. No package, image, chart or
GitHub release is published automatically.

## Controls read back on October 8, 2026

`main` requires one approving pull-request review, dismisses stale reviews,
requires code-owner review and approval of the latest push, and enforces these
controls for administrators. The branch must be current and pass all six GitHub
Actions checks: `python`, `package`, `local-integration`, `oidc-integration`,
`autonomy-integration` and `self-host-integration`. Their required identities
are bound to GitHub Actions app 15368. Linear history and conversation resolution
remain required; force pushes and deletion are disabled.

Eric Kittelson is currently the only collaborator and the sole
[CODEOWNER](../.github/CODEOWNERS). An independent eligible reviewer must be added
and included in ownership before this maintainer-authored PR can merge. There is
no review bypass in the preparation procedure.

Both `oss-release-candidate` and `oss-release` environments exist, but the current
readback contains no required reviewers and permits administrator bypass. They
are not qualified release environments. [RELEASING.md](../RELEASING.md) requires
named independent reviewers, prevention of self-review and administrator bypass,
and release-tag restrictions before signed binary preparation/publication.
The [actual control checker](../scripts/check_release_controls.py) verifies those
settings; a ready variable cannot replace its receipt.

Dependabot security updates are enabled. GitHub Code Security and Secret
Protection are currently disabled; the repository does not claim continuous
GitHub code/secret scanning. The [local secret candidate check](secret-scanning.md)
runs in CI with explicit limits. Do not enable paid private features without a
separate entitlement and cost decision.

## Public-alpha preparation

The [public source checklist](public-alpha-readiness.md) records current-tree
sanitation and source publication gates. Removed pilot reports and connector
code remain in private Git history; that history has not been cleared for public
disclosure. Preserve the private original when preparing a new source snapshot.

The October 8 offline notice check covers 271 selected dependencies, 272
hash-verified archives, 208 distinct texts and 64 publisher proof files, with no
missing notice texts or source pointers. The inventory follows current lock
files and publisher inputs. It does not grant source/fixture rights, settle
license compatibility or corresponding-source obligations, qualify container OS
coverage, or approve publication. Independent owner review remains required.

The [security contact](../SECURITY.md) uses `hello@quietvictorylabs.com`.
Delivery, monitoring and escalation have not yet been verified for this project.
The content-free test needs an authorized sender and private receipt plus owner
acknowledgment. GitHub private vulnerability reporting is unavailable on the
current private repository; enable and test it on the chosen public repository.

Before merging or publishing, read back the actual commit, required checks,
review, protections, visibility and security route. Local tests and green CI on
an earlier commit do not cover new changes.
