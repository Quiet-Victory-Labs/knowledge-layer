# Source metadata access in the OIDC pilot

The OIDC profile requires an explicit grant before a browser user or agent can read a connection's metadata. The grant matches a verified OIDC `sub` claim or one of the configured group claims. `read` permits source status, curated snapshots, search, contracts, graph, and MCP context. `manage` also permits refreshing or changing that source when the caller has the steward group or the `knowledge.sources.manage` agent scope. Source creation requires that management role **and an operator source approval**; it grants `manage` only to the creator.

The operator supplies `KNOWLEDGE_SOURCE_POLICY` or an owner-only `KNOWLEDGE_SOURCE_POLICY_FILE` at OIDC startup and to the refresh worker. A missing or malformed policy stops startup. Each entry binds an exact tenant, connector kind, and source selector to allowed OIDC subjects or groups. For example:

```json
{"version":1,"sources":[{"tenant_id":"demo","kind":"postgres","selector":{"secret_ref":"KNOWLEDGE_SOURCE_ORDERS","schema":"public","tables":["orders"]},"allowed_subjects":[],"allowed_groups":["orders-stewards"]}]}
```

The selector must name the full source identity: local path and file pattern; Iceberg catalog, namespace, and optional table list; PostgreSQL credential reference, schema, and optional table list; SQL provider, credential reference, optional schema and table list; object-store credential reference and prefix; or external-adapter credential reference and scope. For local files and external adapters, `retain_semantic_samples` is also part of the identity and defaults to `false`. Omitting an optional table list explicitly approves all tables visible to that source credential. Use narrow read-only source credentials and separate entries for distinct audiences. A steward cannot change a connection's identity or enable content retention after creation; create a new connection with new grants. Profiling budgets can still change. API and worker reload operator policy at authorization boundaries, including queued jobs. Missing or malformed rotated policy refuses access. In version 1, removing an approval stops new refreshes but does not itself revoke existing metadata grants or erase saved snapshots.

Grants are stored in PostgreSQL and checked on each request. Removing one takes effect on the next request. There is no grant editor in the UI or agent tools. An infrastructure operator with database access runs the offline command from the same image after applying schema revision `0002_source_grants`:

```sh
python -m knowledge_layer.source_access_cli grant --connection conn_example --principal-type group --principal finance-analysts --role read --actor access-operator
python -m knowledge_layer.source_access_cli list --connection conn_example
python -m knowledge_layer.source_access_cli revoke --connection conn_example --principal-type group --principal finance-analysts
```

The command reads `DATABASE_URL_FILE` or `DATABASE_URL` through the configured operator secret mechanism. Run it only from a trusted operator shell with the same database endpoint as the service. Match `--principal` to the actual IdP claim; the service does not guess display names or email aliases. Existing connections receive **no automatic grants** when upgrading from revision `0001_initial`, so OIDC access fails closed until the operator provisions them. Keep a verified backup before upgrading.

At revision `0006_audit_events`, grant and revocation changes also append [operator-exportable governance events](audit-events.md) in the same transaction. Use `--actor` on both grant and revoke to identify the operator; earlier grant changes are not backfilled.

At revision `0007_proposal_lifecycle`, a read-granted contributor can keep at most 250 unarchived proposals per source across semantic and contract types. Revision `0015_inventory_concepts` includes primary inventory-concept proposals in these same combined quotas. The source has a 2,000 unarchived-proposal cap, with its final 200 slots reserved for stewards. A steward with a `manage` grant can archive 1–100 local proposals per request through `POST /api/knowledge/connections/{id}/proposals/archive` with `proposal_ids` and an 8–1000 character reason. The browser route requires OIDC steward membership and CSRF protection. Agent HTTP and MCP tools cannot archive. Archive preserves review and audit rows, removes archived proposals from current projections, and withdraws the corresponding active inventory-concept publication and any active source snapshot publication; historical snapshot versions retain their point-in-time view. The owner should refresh and review the source before republishing. This lifecycle bounds active projection work but does not automatically cap historical database or audit storage.

This is a metadata grant for the whole connection. It does not mirror warehouse row policies, document permissions, or field-level entitlements. A source's stored snapshot may include document excerpts, sample labels, and citations from other sources. Grant a principal only if the full stored snapshot is appropriate for that principal. Split sources by permission boundary or disable semantic sample retention when needed. The platform does not execute source queries for agent users.

## Opt-in whole-source permission attestations

Policy version 2 adds `metadata_permissions` with exactly `enforce`, `trusted_native_exporters`, and `attestations`. Set `enforce` to `true` to require a registry grant **and** a separate current attested metadata audience. Management `allowed_subjects`/`allowed_groups` in `sources` remain source creation/refresh authority; they never imply that the principal can read the discovered metadata. Version 1 retains pilot behavior. Once a running store observes enforced version 2, downgrading or withdrawing its policy cannot silently restore version 1 visibility; a deliberately configured restart is required to change deployment mode.

Each attestation contains exactly these fields:

| Field | Meaning |
| --- | --- |
| `tenant_id`, `kind`, `selector` | Exact complete source identity, using the same normalized selector as the source approval. |
| `source_identity_digest` | `source_permissions.digest(source_identity(kind, normalized_config))`. |
| `basis`, `authority`, `revision` | `operator_partition` explicitly asserts a uniformly shareable scope; `native_exporter` identifies an independently qualified exporter named in `trusted_native_exporters`. `authority` and `revision` are bounded identifiers. |
| `state` | Only `complete` permits access. `unknown`, `partial`, `stale`, and `withdrawn` refuse. |
| `checked_at`, `expires_at` | Timezone-aware clocks; checks cannot be in the future, expiry is exclusive, and lifetime is at most 900 seconds. |
| `allowed_subjects`, `allowed_groups` | Separate metadata audience; nonempty, bounded, explicit verified subject/group mappings. |
| `manifest_digests`, `resource_manifest_digest` | Exhaustive artifact permission coverage and `source_permissions.manifest_digest(manifest_digests)`. |

The server computes `source_permission_manifest_digest` from **all sanitized
native snapshot metadata**, including errors, profiling, drift, concepts, metrics
and proposals. The version pin is `snapshot-full-v1`; older partial manifests
fail closed. It computes `source_permission_identity_digest` from the locked
source identity. Connector-supplied pins cannot establish permission authority.
Only internal steward-review snapshots inherit the native resource pin, after
checking the exact current parent ID/version and read permission under the source
lock. A native refresh always computes a new full manifest. This preserves local
review without allowing new native metadata to borrow prior permission evidence.
Inventory artifacts use their exact `manifest_digest`. Attest **every retained
snapshot and inventory manifest**, including history/staging. One uncovered
artifact denies the whole source. Requalify or retire legacy snapshots before
enabling reads. Policy limits are 128 identities, 256 distinct manifests per
identity and 1,024 total references; exhaustion denies rather than dropping
history. Input is capped at 1 MiB, with a stricter 64 KiB owner-only file limit.

This gate applies before shared source lists/counts, search candidates, evidence streams, snapshots/history, graphs, inventory pages, and dependent citations/contracts can return metadata. There is no partial snapshot redaction that could leave derivative concepts or copied citations visible. Removing an attestation, changing its audience or state, withdrawing source approval, or expiry denies the next authorization check. Authorized managers can still repair a hidden source; their refresh response exposes only a mutation acknowledgement until the separate read boundary is valid again.

An `operator_partition` is an operator assertion, **not native ACL mirroring**. A `native_exporter` entry trusts an operator-provisioned, owner-only policy artifact; the repository does not verify an exporter signature, query the provider's current ACLs, or qualify account/group mappings. Deploy an authoritative exporter that checks the complete selected resource/field set, maps verified identities, rotates this policy on native revocation, and refreshes evidence within its expiry. Until that pipeline is qualified, use clearly labeled operator partitions or leave native-protected scopes denied. Provider failures and partial native permission results must publish a refusing state rather than retaining a `complete` assertion. Cached IdP group claims also remain bounded by their token/session expiry; remove registry grants or permission audience entries for immediate denial.
