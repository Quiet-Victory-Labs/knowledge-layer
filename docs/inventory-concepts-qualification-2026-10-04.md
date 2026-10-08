# Inventory primary-concept qualification — 2026-10-04

This slice makes an inventory-only catalog's local business meaning directly
consumable without a dataset snapshot. The registry stores an immutable local
definition with exact native entity/proof pins, an independent owner decision,
and a separate steward publication. Agent transports expose proposals and reads;
browser review/publication retains organization-role and CSRF checks.

The [consumer workflow](inventory-concepts.md) is scoped concept lookup and direct
handle retrieval. It preserves complete definitions, conflicting names, unknown
native revisions and untrusted native authority. It refuses stale head, completed
empty successor, source error/profile/policy changes, active refresh, lost access,
archive and withdrawal. Mutation history can remain committed while a subsequent
response refuses changed authority. It neither executes source SQL nor compiles
native formulas or establishes joins/equivalence.

## Actual disposable PostgreSQL and package evidence

Clean implementation commit `9ec25aef46b32b2fc1af0883e656785066677e37` ran the managed
drill against disposable PostgreSQL 18.6, using the existing parent/child workers
and synthetic dbt/portable catalog exports:

| Check family | Passed |
| --- | ---: |
| Inventory persistence, migration and read/lock checks | 49 |
| Paged adapter workers and permission fences | 15 |
| Native/portable capture, snapshot assertions and primary-concept use | 44 |
| Autonomy publication, backup/restore and cleanup | 16 |

The two portable fixture accounts published their own local Revenue concepts,
read them through exact scoped pages and direct context, then refused those
concepts after an actual failed refresh with substituted source identity. The
catalogs remained snapshotless. All 25 registry-table census counts were restored
after nonce-scoped cleanup. The task-owned adapter, PostgreSQL database/container
and temporary fixtures were removed; no customer source was targeted.

The wheel and source distribution built from that same clean commit each passed
an isolated base-only installation and actual proposal → review → publication →
consumption → failed-source refusal. Their digests are:

- Wheel SHA-256: `1add66f1a7946eaac622675a51130bc16bd8fc1db5871923720c25df09493136`
- Source SHA-256: `26c3c028b5434d4aaea9311cce2f4d4930079efd24973b06eea763a34d5d9edd`

Private receipts remain in `.local/inventory-concepts/` on the qualification
host. They are unsigned, unpublished, runner-specific artifacts. The unchanged
reviewed secret baseline remained
`192d013cf00330e318ac43730329f63161e2f323a8a371c2b2d56544ca7a9a74`.

Final branch regression and package/Helm/Docker, native IdP fixture, local-service,
and autonomy checks are independently reported by the draft PR's current-head
[Verify workflow](https://github.com/Quiet-Victory-Labs/knowledge-layer/actions/workflows/verify.yaml).
The immutable baseline evidence above must not be presented as execution of a
later commit. Tests include complete MCP/HTTP text and role/CSRF/origin denial
effects; mocked OIDC claims are separate from the disposable Keycloak drill and
from real customer IdP qualification.

## Remaining boundaries

This immutable baseline predates global/term retrieval and inventory-aware signed
task receipts. Those later consumer paths are qualified separately in the
[inventory consumption slice](inventory-consumption-qualification-2026-10-04.md).
Ontology/RDF projection and exact company-guidance inventory dependencies remain
outside both slices.
Native vendor APIs/ACLs, real enterprise scale/operations and measured analyst
uplift remain unqualified. A local definition publication does not certify an
entire catalog, authorize later execution or establish enterprise release.
