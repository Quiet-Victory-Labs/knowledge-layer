# Public source alpha preparation — October 8, 2026

The publishable product is a `0.1.x` alpha evaluation. The repository is still
private. Source visibility does not imply a signed binary release or enterprise
qualification.

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

## Required decisions and review

1. Name an independent GitHub reviewer and add that reviewer to repository access
   and CODEOWNERS. Review and merge the exact change after all six Verify jobs pass.
2. Choose public history: a fresh source snapshot preserving this private original,
   or an explicitly reviewed disclosure of the original full history. Removing
   current files does not remove older Git objects. The prepared snapshot is for
   review; it does not authorize publishing either history.
3. Record first-party/source/fixture rights and dependency obligations for the
   exact intended snapshot. LGPL-declared psycopg packages and corresponding-source
   obligations remain review inputs; notice coverage is a technical inventory.
4. Specify the sender for a content-free security-contact test, confirm private
   delivery, and name the monitoring/escalation owner. Verify the acknowledgment
   procedure. See [SECURITY.md](../SECURITY.md).
5. On the chosen public repository, enable/test private vulnerability reporting,
   apply the required branch/reviewer protections and verify their actual readback.

The [signed release procedure](../RELEASING.md) additionally requires qualified
release environments, current image vulnerability review and signed artifact
verification. Those binary/GA requirements have not been approved by this source
preparation.
