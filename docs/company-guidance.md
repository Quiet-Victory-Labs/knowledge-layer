# Company skills and rules

Company guidance gives customer-selected harnesses a shared, reviewed way to work with company knowledge. A bundle contains procedural skills and normative rules, an authenticated audience, explicit applicability, and optional pinned knowledge dependencies. The existing PostgreSQL registry stores it; no extra service, model or scheduler is required.

The [qualification report](company-guidance-qualification-2026-10-03.md) records the clean-checkout regression and PostgreSQL/MCP runtime drill, including the remaining deployment and harness qualification limits.

Knowledge describes what a source means. Skills describe how to perform a task. Rules describe requirements for that task. This registry does not compile rules into source policies or authorize a harness's other tools. Read-only source credentials, tool authorization and publication gates remain enforceable controls.

## Review and publication

An operator or authorized source manager drafts a bundle. Every revision stores immutable validated text and its SHA-256 digest. Creating another draft preserves the prior publication. A steward publishes the latest revision using its exact revision/digest and a rationale. The agent API and MCP expose drafting and retrieval; they expose no publication or withdrawal tool. Browser publication uses the existing identity, CSRF and steward boundary. The offline CLI assumes a trusted operator with registry credentials.

Publication is an accountable decision about instructions. Source documents, imported catalog policies and an agent's confidence do not automatically become approved rules. The operator must review their authority, audience and applicability before publishing them. Withdrawal leaves revision and audit history; reactivation requires a new draft and review.

The [synthetic analytics template](../examples/company-guidance-synthetic.json) describes an illustrative research procedure. Its business unit and task scope are fictional. It is a draft example, not an adopted company policy or a claim of historical data completeness. It contains no source records or invented source bindings. To bind an actual reviewed source, add its current `connection_id`, `snapshot_id` and `snapshot_version` under `dependencies` before review.

For a managed PostgreSQL installation, first perform the [schema upgrade](schema-migrations.md). Review and submit a local JSON payload:

```sh
python -m knowledge_layer.guidance_cli draft --slug synthetic-sales --file examples/company-guidance-synthetic.json --expected-revision 0
python -m knowledge_layer.guidance_cli get --slug synthetic-sales --revision 1
```

The response returns the immutable payload and digest. After operator review, substitute that exact digest and the actual rationale:

```sh
python -m knowledge_layer.guidance_cli publish --slug synthetic-sales --revision 1 --expected-digest '<returned SHA-256>' --rationale 'The steward reviewed this procedure and its applicability.'
python -m knowledge_layer.guidance_cli resolve --business-unit synthetic-analytics --task sales-analysis
python -m knowledge_layer.guidance_cli export --business-unit synthetic-analytics --task sales-analysis --client portable
python -m knowledge_layer.guidance_cli withdraw --slug synthetic-sales --revision 1 --reason 'The steward withdrew this procedure pending another review.'
```

Supply registry credentials through `DATABASE_URL_FILE` or the operator secret mechanism. Export prints an exact JSON manifest/file map; it does not write into a harness, execute a script or enable tools. Review the manifest before distributing the files.

## Applicability and permissions

`business_units`, `tasks` and `connection_ids` declare applicability. Empty arrays mean a general baseline. Omitted consumer context does not select narrow guidance. Caller-supplied business-unit/task labels choose context; they do not confer permission. Source-scoped guidance also requires source metadata access.

`audience` explicitly chooses company-wide visibility or bounded subjects/groups. A caller cannot broaden the audience by supplying another role or business unit. In a managed deployment, the verified principal and source grants determine access. Hidden audiences do not leak revision names, content or counts through resolution. Customer IdP group mappings and source-native permissions remain separately qualified deployment responsibilities.

Dependencies are evidence bindings, not applicability. Every pinned source must still be accessible, current and published for agent use. A new snapshot, source error, withdrawal or revoked source grant prevents dependent guidance from being used. Effective dates are aware clocks evaluated in UTC. Revision history remains available for authorized review and is explicitly unsuitable for current instructions when not effective.

The resolver returns `ready`, `blocked` or `empty`, the evaluation clock/context, and exact authorized published bundles. It covers that current authorized context; it does not claim that the entire company policy estate is connected or that a client will obey the guidance.

Rules declare a stable `key`, a typed JSON scalar `value` and a human-readable `statement`. Applicable published bundles assigning different values to the same key produce an explicit conflict. No priority winner is invented. This detects declared key/value disagreement, not every contradiction in natural language. A responsible owner must revise or withdraw the conflicting publication. Blocked resolution cannot be exported.

Payloads, registry size and responses have explicit budgets. An overflow refuses resolution or export rather than silently dropping company rules. A source-free procedural bundle can have no dependencies; that absence is explicit and does not certify any source facts.

The current alpha allows 100 bundles per organization, 100 immutable revisions per bundle, 8 skills per revision, 32 KiB per payload, and 256 KiB per complete resolution. Portable export additionally allows at most 20 applicable bundles and 100 skills, within its own 256 KiB complete-response budget. Reaching a capacity requires operator review and a future retention/scale policy; it is not silently treated as a successful update. A revoked publication stays a visible blocker for its applicable audience until an owner publishes a reviewed replacement.

Withdrawal requires a real steward in the published audience with current
manage grants for every declared source scope. Loss of read access to a
separately pinned dependency does not disable withdrawal. Its acknowledgement
contains only the publication handle and withdrawal state; retained instructions,
dependency identifiers and review rationale require ordinary authorized history
inspection. Revision, decision and source-publication history stays intact.

## Harness interfaces

Compact and advanced MCP expose:

- `resolve_guidance`: current applicable published instructions, conflicts and blockers.
- `get_guidance`: authorized revision review with explicit state and `use_as_instructions`.
- `draft_guidance`: an immutable proposal requiring management privileges and optimistic latest-revision checking.
- `export_guidance_bundle`: complete portable files from a ready resolution.

The compact workspace advertises the resolver and exporter. Agent HTTP provides `GET /agent/api/guidance/resolve`, `GET /agent/api/guidance/export`, `GET /agent/api/guidance/{slug}` and `POST /agent/api/guidance/{slug}/draft`. Resolve/export accept `business_unit`, `task`, and repeated `connection_ids` query parameters. The browser equivalent uses `/api/knowledge/guidance`; only that authenticated surface additionally offers `POST /{slug}/publish` and `POST /{slug}/withdraw`. Draft bodies contain `payload` and `expected_revision`; publication requires `revision`, `expected_digest`, and `rationale`; withdrawal requires `revision` and `reason`.

Portable exports use the open [Agent Skills specification](https://agentskills.io/specification), with named `SKILL.md` packages and supporting references. Required capabilities remain descriptive; an export does not generate `allowed-tools` permissions. The [client implementation guidance](https://agentskills.io/client-implementation/adding-skills-support) supports filesystem or remote discovery. Use our [installation guide](agent-skill-install.md) for optional client layouts. This build tests formats and tool boundaries, not every named harness's actual discovery/activation behavior.

Exports carry revisions, digests, validity, dependencies, and a requirement to resolve guidance again before use. A copied file cannot revoke itself or guarantee it contains the latest publication; the customer harness must perform the current check. Offline fallback, policy compilation, universal harness enforcement, automatic distribution/revocation, fine-grained business-unit stewardship and semantic natural-language conflict detection remain future work.
