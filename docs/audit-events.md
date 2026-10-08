# Governance audit events

Revision `0006_audit_events` adds an append-only application ledger in PostgreSQL. The service records source creation and configuration changes, snapshot versions, grant changes, proposal submissions and owner decisions, publication and withdrawal, and durable refresh requests, cancellation, and attempt outcomes. Revision `0007_proposal_lifecycle` also records steward proposal archives. Each event has a tenant, optional source ID, actor, UTC timestamp, type, and a small controlled detail object. The event is inserted in the same database transaction as the change it describes; a failed audit insert fails that change. Duplicate requests that make no change do not create another decision, archive, or publication event.

The detail object contains IDs, roles, status codes, and SHA-256 digests. It does not intentionally copy source records, credentials, proposal bodies, or rationale text. Source-provided identifiers can themselves be sensitive, so treat the entire export as sensitive metadata. Publication, decision, and archive records keep their own rationale or reason in the registry. Earlier actions are **not backfilled** when upgrading to v6; export coverage begins at the migration. Read-only agent calls are not logged in this database ledger.

In OIDC mode, the service writes one `knowledge.access` JSON log for each authenticated browser API, agent HTTP, or MCP request. It records UTC time, verified subject, surface, fixed operation name, HTTP method and status, plus a connection ID when it appears as a validated route parameter. Each distinct authorized connection whose registry row, snapshot, graph, or refresh job is read during that request also gets a `knowledge.source_read` event with the verified actor, tenant, connection ID, surface, and request outcome. A cross-source search records every source whose metadata it inspects, even if that source produces no result. A denied source does not produce a source-read event. Both event types exclude request and response bodies, query strings, tool arguments, headers, and tokens. MCP still identifies only its transport request, not the individual tool. Demo mode does not emit these identity-backed logs.

These access events describe reads of **stored source metadata**, including reads
made while handling a mutation. Queued workers additionally record
`source.native_read_started` before discovery and `source.native_read_finished`
after return/failure in the governance ledger. These bind the requester, exact
attempt and source identity digest. See [worker isolation](source-worker-isolation.md)
for refusal and hard-loss behavior. They do not identify every provider request
or field, prove response delivery, or cover independent direct source tools.
Protect the logs and ledger as sensitive identity/source metadata, monitor
unmatched starts and delivery loss, and configure independently retained storage.
This is not yet a complete compliance-grade source-read audit.

Only an offline operator process can export events. The browser, HTTP agent API, and MCP do not expose the export. The command streams one consistent database snapshot to a **new** JSONL file with mode `0600`, refuses an existing file or relative output path, and prints only its event count and SHA-256 digest. For the OIDC Compose pilot, mount a writable private directory owned by the configured container UID:

```sh
docker compose -f compose.oidc-pilot.yaml run --rm \
  -v /private/knowledge-audit:/audit-output \
  knowledge python -m knowledge_layer.audit_export_cli \
  --tenant demo --output /audit-output/events-2026-10-02.jsonl
```

Use `--connection conn_example` to restrict an export to one source. Protect the output as sensitive governance metadata. Verify the printed digest after transfer, then import the file into the organization's SIEM or immutable archive. Set the retention period, access rules, legal hold, and destruction procedure in the customer's policy; this alpha service does not purge audit records automatically. The offline grant command's `--actor` value is operator supplied; correlate it with independently authenticated shell or change records. Database administrators can still alter the ledger, so it is **not tamper evident** and is not a substitute for independently retained audit logs. A production deployment must qualify complete source-read attribution and both log and export delivery under its own compliance requirements.
