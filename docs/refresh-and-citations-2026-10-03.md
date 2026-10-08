# Refresh scheduling and semantic citation validation

Knowledge Layer supports customer-controlled hourly, daily, weekly and monthly
source schedules through its PostgreSQL queue and worker. Imported definitions
can support a different source's contract only through current local reviews and
explicit evidence references. The public checks below replace a private pilot
narrative; no new live account or continuous-operation qualification is claimed.

## Repeatable validation plan

Provision a disposable registry and a synthetic adapter such as the
[reference adapter](../examples/reference_adapter.py). Review the native scope and
explicit observation/completeness contract before refreshing it. Use the
[scheduling guide](refresh-jobs.md) and [local qualification runbook](local-qualification-runbook.md)
for operator-controlled execution and recovery boundaries.

1. Upgrade a backed-up older schema and verify that legacy unknown completion
   clocks remain unknown. Compare physical schema and preserved records.
2. Store a supported recurrence through HTTP/MCP. Inject a past due instant,
   dispatch a separate worker and verify the exact schedule/job link and next due
   time. An injected overdue instant is not evidence of continuous operation.
3. Review one synthetic native definition and its use on another source's field.
   Verify exact namespace, source revision, target review and citation pins through
   both HTTP and MCP. Leave unavailable native clocks and versions unknown.
4. Change or withdraw the imported definition. Confirm stale context refuses
   consumption until the affected meaning is reviewed again.
5. Inject a failed refresh, expire a claim and revoke direct source access.
   Attempt clocks must not advance completed-inventory freshness, and revoked
   access must deny the next read.
6. Verify restart and cold restore, then remove only the resources created for
   this validation. Retain private receipts under the operator's policy.

## Qualification limits

Run the current offline scheduling and citation regressions from the checkout:

```sh
uv run --no-sync python -m pytest -q tests/test_refresh_schedules.py tests/test_upstream_semantic_citations.py
```

This command uses test fixtures; it replaces the removed private runtime recipe.
No execution result for this revised public guide is claimed here.

Offline fixtures and a disposable PostgreSQL run establish only their recorded
scope. They do not prove native account visibility, upstream ACL mirroring,
source query execution, seven-day endurance, customer recovery objectives,
calendar/SLO semantics or semantic answer quality. The [publication guide](source-publication.md)
keeps review, publication and discovery completeness separate.
