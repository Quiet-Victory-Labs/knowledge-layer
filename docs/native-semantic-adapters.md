# Optional native semantic adapters

These components run outside the core service and require no additional SDK or
hosted model. The sample supports the existing v1.2 bounded snapshot and v1.3
paged inventory paths. Native account and permission propagation qualification
remain open; mounted artifacts, mocked DataHub transport and a disposable
PostgreSQL worker are separate evidence layers.

The DataHub component issues only a fixed `glossaryTerm(urn:)` GraphQL query for
1–100 operator-selected native URNs. It preserves the term, description, source
reference and returned ownership inside opaque native evidence. Unavailable or
denied selected URNs refuse the scope rather than establishing deletion.
Its read query follows the [DataHub query API](https://docs.datahub.com/docs/graphql/queries)
and [native glossary schema](https://github.com/datahub-project/datahub/blob/master/datahub-graphql-core/src/main/resources/entity.graphql).
This first subset does not inventory datasets, crawl relationships, infer term
equivalence, call mutations, obtain tokens or turn ownership into local grants.

The dbt component accepts a bounded UTF-8 manifest with the exact
[manifest v12 schema identifier](https://schemas.getdbt.com/dbt/manifest/v12.json),
an aware native generation clock and 1–100 selected unique IDs from metrics,
semantic models or exposures. It retains each selected native node unchanged
inside a bounded opaque definition. Unsupported versions, duplicate keys, absent
IDs and records over 4,000 bytes refuse. Global environment metadata is not
imported. A metric remains a native declaration for the customer's dbt runtime;
the importer does not compile dbt, resolve its dependencies or execute it.
Other [dbt artifacts](https://docs.getdbt.com/reference/artifacts/dbt-artifacts)
graduate independently.

## Preserve full meaning through paged inventory

The opt-in inventory functions preserve complete native records in evidence,
including expressions, filters, dependencies, units, owners, custom properties
and unsupported constructs where the selected native record carries them.
Same-named records in different operator namespaces have distinct stable IDs.
Missing, null, zero, empty and false values retain their native state. Dependencies
remain native references; importing them does not prove a join or resolve a
referenced record outside the selected scope.

`dbt.inventory_records` accepts manifest v12 only, a maximum 16 MiB artifact and
1–10,000 exact selected metric, semantic model or exposure IDs. It retains a
small explicit artifact metadata set and excludes `metadata.env`. An absent or
null generation clock is labelled unknown; an invalid supplied clock refuses.
The artifact content hash pins the exact selected capture, without asserting
that the mounted artifact reflects the current dbt service.

`datahub.inventory_records` reads 1–1,000 explicit glossary URNs through a fixed
GraphQL query. Its ownership selection uses native union type fragments. It
retains the returned properties and ownership, including raw schema, source
references, custom properties and native creation/ownership clocks. Each native
HTTP response is uncompressed and bounded to 64 KiB. A partial, denied or missing
term refuses the entire selected capture. The content watermark represents the
bounded sequence of term reads; DataHub atomic snapshot consistency, native
search pagination and end-user ACL propagation are unqualified.

The sample validates the whole capture before its first page, then serves at
most 100 entities/200 evidence records and 256 KiB per page. Whole records are
bounded to 64 KiB and never truncated. One active capture is retained per process,
bounded to 16 MiB of canonical captured metadata. Python heap usage is higher
because parsing, validation and serialization allocate additional objects; this
is not a 16 MiB process-memory guarantee. Complete capture and page budgets must
fit the platform's configured inventory limits before any response is exposed.

Continuation pins the scan, source profile, scope, watermark, cursor and five
minute observation window. Each request re-reads the owner-only native profile;
removed scopes refuse and changed configuration or native credentials invalidate
the active capture. Continuations use captured observations, without a fresh
native permission probe. After restart or scan replacement, start a fresh scan;
this adapter does not persist or resume native vendor cursors. Expired captures
are inaccessible and their memory is released on replacement or process exit.
Run separate adapter processes for independent concurrent refreshes. Completion
applies only to the explicit selected IDs; absence outside that selection and
deletion of unavailable selected IDs are never inferred.

## Run the scoped sample

Create an owner-only profile file containing named scopes. For dbt, a scope has
`provider: dbt`, `namespace`, a read-only mounted `manifest` path and `unique_ids`.
For DataHub, it has `provider: datahub`, `namespace`, an operator-approved HTTPS
`endpoint` ending in `/api/graphql`, a restricted `read_token` and selected
`urns`. Never put that profile, tokens, manifests or generated metadata in Git.

Set `NATIVE_ADAPTER_PROFILE_FILE` and `ADAPTER_BEARER_TOKEN` through protected
operator configuration, then run:

```sh
.venv/bin/python examples/native_semantic_adapter.py
```

The sample binds loopback port 8061. Put it behind the customer's HTTPS and
network boundary before remote access. Requests may select only a named scope;
they cannot supply endpoints, native tokens, GraphQL or artifact paths. Register
an operator-managed external-adapter profile with protocol `1.2`, endpoint
`/discover` and the exact allowed scopes. Upstream tokens must have read-only
privileges. Retain existing platform source grants and steward review.

For the paged path, register a platform profile with protocol `1.3`, provider
`dbt` or `datahub` matching the selected native scope, a pinned operator revision
and source identity. Create an `external_adapter` source with
`discovery_mode: inventory` and explicit `retain_inventory_payloads: true`.
The [paged refresh guide](paged-adapter-refresh.md) describes the profile,
retention decision, worker authorization and budgets. The mounted native profile
still has a 64 KiB limit, so its selected IDs must fit that configuration budget.
Refresh commits discovery metadata and returns an inventory result. A harness
uses `get_inventory` with operations `scopes`, `page` and `select` to retrieve
exact complete evidence for bounded curation. It must preserve imported
authority, unsupported semantics and conflicts when proposing local meaning.
Inventory completion does not publish a metric or grant native execution.

For a disposable PostgreSQL worker qualification, run:

```sh
.venv/bin/python scripts/drill_autonomy.py --managed --include-native-semantic --output .local/native-semantic.json
```

The drill reads synthetic artifacts for two business units through the actual
HTTP sample and worker parent/child. It verifies full large definitions, separate
same-name identities, selected evidence readbacks, failed artifact handling and
exact registry cleanup. It does not connect to a real dbt or DataHub account.
The [frozen qualification record](native-federation-qualification-2026-10-03.md)
separates regression, installed-package and native-worker evidence.

For an offline conversion, `scripts/import_dbt_manifest.py MANIFEST --namespace
UNIT --unique-id NATIVE_ID` emits a bounded adapter response. Output contains
company metadata; store it under customer retention/access policy. Import
completion applies only to the explicit selected IDs and observation window.

## Qualification still required

Pin the exact deployed vendor/schema version; exercise a real restricted
account, denial, token rotation, changed definition, scope removal and native
permission behavior. DataHub's creator/owner and dbt's group metadata are
provenance, not local authorization. Use deliberately separate approved scopes
and grants until native ACL mapping is qualified. Hosted documents and additional
catalog families remain separate adapters, not implied capabilities.
