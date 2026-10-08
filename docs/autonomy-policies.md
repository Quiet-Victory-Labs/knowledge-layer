# Reviewed publication policies

Customers may delegate the publication of **unchanged reviewed metadata** to a
named service identity. Execution is disabled by default. This first policy
class extends the existing source publication gate; it adds no model runner,
approval shortcut, source SQL execution or upstream writes.

An operator enables `KNOWLEDGE_AGENT_AUTONOMY=1` in the OIDC deployment, or
`connectors.agentAutonomy: true` in Helm. The platform refuses this flag in local
token/development mode. Apply migrations before startup (current head:
`0013_autonomy`). No additional service or dependency is required.

## Establish authority

1. Connect an operator-approved source, complete discovery, and review at least
   one current evidence-backed assertion. Failed or incomplete discovery and
   changed review/evidence cannot establish a policy.
2. Grant the exact service subject a **direct user manage grant** for this source
   through the existing operator grant workflow. Also include the exact subject
   in the source policy's `allowed_subjects`. Group membership alone is
   insufficient for delegation. Its verified agent token needs the normal agent
   scope, source-management authority and `knowledge.autonomy` scope. It needs
   no steward membership.
3. A source manager drafts a policy. A real steward separately approves its
   revision and digest with a rationale. Company skills, upstream descriptions,
   model confidence and native catalog approval cannot grant this authority.
4. The customer's harness refreshes through the existing authorized workflow,
   waits for completed discovery/curation and applies the policy against the
   exact current snapshot ID/version. It may poll maintenance packets and use
   its own scheduler; the platform does not automatically invoke the harness.

Browser control endpoints (existing authenticated session and CSRF boundary):

| Method | Path | Authority |
| --- | --- | --- |
| GET | `/api/knowledge/connections/{id}/autonomy` | Source manager |
| POST | `/api/knowledge/connections/{id}/autonomy/draft` | Source manager |
| POST | `/api/knowledge/connections/{id}/autonomy/approve` | Steward |
| POST | `/api/knowledge/connections/{id}/autonomy/revoke` | Steward |

Example draft body, with a customer-selected future expiry within 30 days:

```json
{
  "expected_revision": 0,
  "payload": {
    "schema_version": "knowledge-autonomy-policy-v1",
    "action": "publish_reviewed_unchanged",
    "subject": "customer-service-subject",
    "expires_at": "2026-10-04T12:00:00Z",
    "max_publications": 10,
    "purpose": "Republish unchanged reviewed order definitions after successful refresh."
  }
}
```

Use the actual returned revision/digest for approval and subsequent calls;
never copy a placeholder digest. The generated OpenAPI schema specifies strict
request bodies. Expiry is checked against server UTC and publication budget is
an integer from 1 to 1000. Policy revisions are immutable. New drafts use the
expected latest revision and leave the approved active revision in effect until
replacement approval or withdrawal.

## Consume the delegation

Opt-in agent HTTP exposes GET `/agent/api/sources/{id}/autonomy` and POST
`/agent/api/sources/{id}/autonomy/apply`. Compact and advanced MCP add only
`get_autonomy_policy` and `apply_autonomy_policy`; their default tool lists stay
unchanged. MCP apply takes `connection_id` and a strict nested `request`:

```json
{
  "revision": 1,
  "expected_digest": "<actual returned 64-character policy digest>",
  "expected_snapshot_id": "<actual current snapshot ID>",
  "expected_snapshot_version": 4
}
```

HTTP apply uses that request as its body. Agents cannot draft, approve or revoke
through the agent API/MCP. Apply derives subject from verified authentication;
there is no caller-selected tenant, actor, approval flag or execution switch.

The binding includes canonical source identity, operator source approvals, all
current assets/concepts/relationships/metrics/evidence/proposals, and the exact
sorted set of effective approved assertion IDs, digests and review timestamps.
A same-count replacement approval still changes the binding. Changed meaning,
new candidates, changed evidence or operator approvals require a new policy and
review. Complete discovery alone is insufficient. This class excludes reviewed
cross-source joins and external evidence dependencies. Conservative byte-exact
metadata comparison may also require review for harmless upstream changes; it
does not infer semantic equivalence. Binding capture refuses canonical metadata
larger than 1 MiB; larger semantic snapshots require the normal review route.

Each apply rechecks current direct grant, operator approval, policy approval,
subject, expiry, budget, exact snapshot and publication eligibility under source
and policy locks. Refresh generation fencing applies when a worker claim is
present. Publication, budget charge, attributed run and audit commit together.
Late expiry or fencing failure rolls the entire transaction back. A retry for
the same policy/version consumes no extra budget, preserves the original actor
and refuses if the publication was withdrawn. An already human-published
version is recorded as a no-op and remains human-owned.

## Withdraw and recover

Steward withdrawal immediately blocks later applies and withdraws all
publications owned by this source's delegation up to the active policy revision,
including publications made by superseded policies. Human-owned publications
remain governed by the existing human withdrawal route. Revision/run/audit
history remains intact. A withdrawn approved policy cannot be reactivated;
establish a new draft/review and refresh a withdrawn source version before
publishing it again.

Inspection still requires source-management authority and a current metadata
manage grant. Withdrawal additionally requires a real steward. Both remain
available after the operator removes connector approval, so stopping connector
access cannot disable the kill switch. Draft, approval and apply still require
current connector approval. Withdrawal uses the captured active revision and
head digest; corrupt revision content blocks consumption but cannot prevent
authority reduction with that exact approved handle.

Expiry and budget exhaustion stop future actions. They do not retrospectively
withdraw previously authorized publications; explicit withdrawal does that.
Source/grant revocation and changed review continue to affect consumer
readiness through the existing live checks. Previously delivered exports
cannot be recalled and require live receipt revalidation before use.

Operator source policies are loaded at process startup. Roll changes out
consistently to all replicas; this implementation does not mirror native
document/field ACL changes or propagate identity-provider token revocation
before the token's normal expiry. SQLite behavior fixtures establish decisions
and rollback logic; PostgreSQL is the supported concurrency boundary.

## Qualification

`tests/test_autonomy.py`, `tests/test_autonomy_schema.py` and
`tests/test_autonomy_transport.py` cover behavior, schema and HTTP/MCP boundaries.
`scripts/drill_autonomy.py` requires an owner-only `DATABASE_URL_FILE` for a
disposable loopback `knowledge_build_*` database. It uses a nonce-scoped table
and SELECT-only role, refreshes through the native PostgreSQL connector, checks
concurrent duplicate publication, persisted actor/budget/run, source-clock and
review preservation, exhausted budgets, queued refresh refusal, replacement
policy withdrawal, actual grant-revocation lock ordering, and late-expiry atomic
rollback. Every fixture is removed and all registry table totals are reconciled.
Optional `KNOWLEDGE_RECOVERY_CONTAINER` runs the existing populated backup/restore
drill before cleanup, including all 23 persisted tables at v13.

Run the same isolated qualification as CI (Docker and the existing Python
runtime required; no ambient `DATABASE_URL` or `DATABASE_URL_FILE`):

```sh
uv run --no-sync python scripts/drill_autonomy.py --managed --output .local/autonomy-drill.json
```

Managed mode generates owner-only credentials, starts one temporary PostgreSQL
container on loopback with a 256 MiB limit and runs populated recovery. Teardown
removes temporary credential files and attempts removal of the exact
ownership-labeled container, including after a launch timeout. Unverifiable
ownership or Docker cleanup failure produces a failed result and requires
operator recovery. It preserves unrelated services. The receipt contains
fixture ownership and check results, not secrets.

The [OIDC drill](oidc-pilot.md#disposable-oidc-protocol-drill) additionally checks
a real disposable Keycloak service account, browser-steward approval and all
three authenticated consumption surfaces with PostgreSQL readbacks. It uses a
custom model-free harness and synthetic source. Customer IdP/service-account
qualification, named harness unattended operation, cross-source autonomy, native ACL
mirroring, seven-day endurance and cloud operating qualification remain gates
for enterprise MVP. Novel semantic curation still uses steward review.
