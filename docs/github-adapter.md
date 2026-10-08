# GitHub repository adapter

The bundled [GitHub adapter](../examples/github_adapter.py) reads an operator-approved set of files from a pinned GitHub commit through the Contents API. It uses the existing external adapter protocol 1.2; Knowledge Layer stores only the selected metadata and, when enabled, bounded source passages. It never clones or executes repository code, enumerates a repository, or writes to GitHub.

This is a separate, loopback-only reference process. Deploy it behind your own authenticated HTTPS boundary when Knowledge Layer runs on another host. Give it a GitHub token with [**Contents: read** access](https://docs.github.com/en/rest/repos/contents?apiVersion=2022-11-28) to only the selected repositories. `GITHUB_ADAPTER_TOKEN` stays in the adapter process; `ADAPTER_BEARER_TOKEN` authenticates Knowledge Layer to the adapter. Do not put either value in connection configuration or commit it to Git. The adapter does not mirror per-user GitHub permissions: everyone granted access to a Knowledge Layer source can read its retained snapshot.

Create an owner-readable JSON config containing exact files and full 40-character commit IDs:

```json
{
  "scopes": {
    "product_docs": {
      "owner": "your-org",
      "repo": "your-repo",
      "commit": "0000000000000000000000000000000000000000",
      "paths": ["README.md", "docs/architecture.md", "src/contracts.py"]
    }
  }
}
```

Start `uv run --no-sync python examples/github_adapter.py --config /private/github-scopes.json --port 4182` with both tokens provisioned as environment secrets. Provision an external adapter profile such as `KNOWLEDGE_SOURCE_GITHUB` for Knowledge Layer:

```json
{
  "endpoint": "http://127.0.0.1:4182/discover",
  "provider": "github",
  "allowed_scopes": ["product_docs"],
  "protocol_version": "1.2",
  "bearer_token": "<adapter bearer secret>"
}
```

The loopback HTTP profile needs the explicit local development setting `KNOWLEDGE_ALLOW_INSECURE_ADAPTER_ENDPOINTS=1`; use HTTPS between hosts. Enable `external_adapter` in the owner's source policy, approve the exact secret reference and scope, and create a connection with `secret_ref: KNOWLEDGE_SOURCE_GITHUB`, `scope: product_docs`, `max_assets` at least the number of selected paths, and `max_columns: 1`. Set `retain_semantic_samples: true` only when the selected file passages may be stored and shown to the source's granted readers. Refresh the connection, inspect coverage and agent drafts, then have a steward review and publish meaning.

The adapter accepts at most 24 exact files per scope, 128 KiB per file, 50 passages of at most 280 characters per file, and 2 MiB per response. A document longer than the passage budget reports incomplete retention; it cannot quietly become an agent-ready source. Python files are read as text with line locators, without AST or dependency analysis. Branch names, whole-repo crawls, GitHub Enterprise Server, pull-request history, repository ACL mirroring, and scheduled commit discovery are not implemented here. Use separate scopes and source grants at permission boundaries. The source account's read-only status and restricted repository access need independent verification for production.

Use a dedicated read-only identity and exact public or synthetic repository files for a disposable adapter validation. The [reference implementation](../examples/github_adapter.py) requires pinned commits and bounded paths. Check file identity, incomplete results, permission denials and changed revisions before applying the [agent build flow](agent-build-flow.md). A local adapter test does not establish managed source approval, upstream ACL mirroring or least-privilege production credentials.