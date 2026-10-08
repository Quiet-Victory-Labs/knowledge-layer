# Inventory citations qualification — 2026-10-04

Initial implementation baseline: `b559da18ef659af94d2fa2cdd56a539e34c47e90`.

An exact current catalog observation can now support local semantic meaning on
an existing snapshot-backed target. The target's proposal, owner review and
steward publication remain separate from discovery and native catalog approval.
The [consumer guide](inventory-semantic-citations.md) describes the reference
shape, budgets, currentness checks and remaining scope boundaries.

Verified from a clean detached checkout of that implementation commit:

- **2,360 tests passed**, one existing SQLite expression-index reflection warning.
  Ruff passed. The immutable secret gate passed for 335 tracked files, with the
  reviewed baseline unchanged.
- Disposable **PostgreSQL 18.6** passed 49 inventory checks, 15 real paged-worker
  checks, **38 native/portable checks**, and 16 autonomy/recovery checks. Actual
  worker parent/child captures fed separately reviewed and published local CSV
  metric interpretations for two synthetic provider families. Compact contracts
  retained exact manifest/entity/proof pins. A later producer account substitution
  failed the worker, retained the historical inventory head and clocks, suspended
  target metrics/publication, and invalidated prior task receipts without changing
  the target snapshot ID.
- Registry table counts returned to their baselines; fixture rows, owned adapter
  servers and the disposable database were removed. Unrelated local
  services were preserved.
- Isolated wheel and source **base-only installs** passed actual citation module
  provenance, linked-proof resolution, unlinked-proof and source-error refusal,
  plus existing inventory, portable mapping/CLI, conformance and retention smoke
  checks. Artifacts remain unsigned and unpublished; no new SDK/model dependency.

Private receipts and build artifacts live under `.local/inventory-citations/`;
they contain fixture ownership identifiers and are not release artifacts. Final
CI results are reported separately on [draft PR 6](https://github.com/Quiet-Victory-Labs/knowledge-layer/pull/6).

This evidence does not qualify a live Databricks, Alation, Atlan, Collibra,
DataHub or dbt account, native field/document ACL propagation, native formulas,
estate-scale semantic projection, primary concept publication directly from an
inventory-only catalog, cloud operations, a paired analyst-value holdout or an
enterprise production release. Final rechecks bound currentness work; they do
not guarantee atomic multi-source delivery or authorize later source execution.

Subsequent RDF hardening uses structured identity encoding so opaque separator
characters cannot collapse distinct entity/proof citations. Its regression case
commits both real inventory records before review and RDF projection. Current CI
validates the final branch head separately from the initial baseline above.
