# Public source alpha — October 11, 2026

The public product is a `0.1.x` alpha evaluation under Apache 2.0. The owner
requested public source availability on October 11, 2026. Public history begins
with the sanitized source snapshot; the original development repository and
its pilot history remain private. Source visibility does not imply a signed
binary release or enterprise qualification.

## Prepared

- Audited SDK, catalog provenance, publication, demo/managed authentication,
  setup origin, concurrent SQLite reads and browser onboarding fixes.
- Desktop and mobile browser smoke tests are required by the Verify workflow;
  the examples use disposable synthetic fixtures.
- Five private pilot reports and the source-specific adapter/drills are excluded
  from the current tree. Originals are preserved in ignored, owner-only local
  storage; [fixture provenance](public-fixture-provenance.md) states the retained scope.
- Current-lock offline notices cover 271 dependencies, 272 verified archives,
  208 texts and 64 publisher proofs with zero missing texts/source pointers.
  Vendored schema/license inputs match pinned upstream bytes; 80 synthetic
  benchmark cases regenerate exactly. No human rights approval is inferred.
- Main protection is enforced for administrators and requires code-owner review,
  latest-push approval and six strict CI checks. See [repository operations](repository-operations.md).

## Remaining review and release work

1. Name an independent GitHub reviewer and add that reviewer to repository access
   and CODEOWNERS. Review and merge the exact change after all six Verify jobs pass.
2. Keep the private development history separate. Public development must start
   from this sanitized repository; do not push branches or tags from the private
   original into it. Removed pilot files must not be reintroduced.
3. Record first-party/source/fixture rights and dependency obligations for the
   exact intended snapshot. LGPL-declared psycopg packages and corresponding-source
   obligations remain review inputs; notice coverage is a technical inventory.
4. Specify the sender for a content-free security-contact test, confirm private
   delivery, and name the monitoring/escalation owner. Verify the acknowledgment
   procedure. See [SECURITY.md](../SECURITY.md).
5. Maintain private vulnerability reporting and the required branch/reviewer
   protections, and verify actual settings before each release.

The [signed release procedure](../RELEASING.md) additionally requires qualified
release environments, current image vulnerability review and signed artifact
verification. Those binary/GA requirements have not been approved by this source
preparation.
