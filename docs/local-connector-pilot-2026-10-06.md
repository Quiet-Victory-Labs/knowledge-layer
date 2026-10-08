# Local connector and speed pilot — 2026-10-06

The repeatable customer path is clone, start locally, claim the owner, connect a
bounded source, refresh, let an agent draft, inspect the exact evidence, accept
and publish, then ask the agent for the published contract. An unsupported
source must say so clearly. A successful file import or exported catalog is
not proof of a live native connector to the system that produced it.

## What ran

`scripts/drill_self_host.py --mixed-corpus` ran against disposable local API,
worker and PostgreSQL containers. It passed 58 checks in 82.9 seconds and
removed its containers, installation, credentials, source files and image
aliases. The corpus had three structured assets (CSV, JSON, JSONL) and five
documents (Markdown, text, HTML, extractable PDF, DOCX). Each produced a
discoverable asset with the expected schema or extracted text. The worker's
refresh completed, a deterministic agent harness made eight evidence-linked
description proposals and abstained on unsupported additional meaning. The
owner previewed and accepted the exact eight proposals; agent readback,
restart, cold backup/restore and withdrawal checks passed. No model was called,
so this is transport, evidence and governance proof, not model quality proof.
The owner steps used real HTTP routes and credentials, rather than browser
clicks; visual UI usability is a separate test.
The previous two-file default drill remains the CI path.

| Local operation | Observed |
| --- | ---: |
| Initial start to ready | 15.17 s |
| Queued refresh to complete, 8 files | 3.18 s |
| Bounded agent build including one stop/restart | 19.25 s |
| Exact owner review/publish request | 85 ms |
| Warm native MCP contract read, 20 requests | p50 56 ms; p95 100 ms |
| Warm HTTP search, 20 requests | p50 64 ms; p95 139 ms |
| Warm HTTP context, 20 requests | p50 102 ms; p95 129 ms |

These single-host samples are diagnostic, not a latency SLO or large-estate
benchmark. The image was an existing local arm64 build
`sha256:faaf5e839e846acd5acb09e9c4ab688003d62bd47505233682b2634a7cd1aecc`;
the test driver ran from checkout `f1110307351ae86cb1794b579ab43c344c683610`
plus this uncommitted drill change. No image built from the changed checkout was
qualified. The report is `/tmp/knowledge-mixed-corpus-20261006.json` on the
test host; the drill refuses to overwrite it.

The native BigQuery connector also completed a direct, read-only metadata
discovery of `bigquery-public-data.samples.shakespeare` using an operator-provisioned
short-lived token: one asset, four fields, complete discovery,
zero reported errors, and `application_rows_read=false` in 0.75 seconds. It
made fixed metadata requests only; no query job, dataset, key or cloud resource
was created. This is a local connector call, not a self-hosted worker/owner
qualification for that Google identity.

A public [country-codes repository](https://github.com/datasets/country-codes)
was cloned at commit `2e9d6498a0456f4afc2f6cf367d1e12a9ed4afe1` and
profiled locally. Narrow selectors found the README in 1.0 ms and its
249-row, 56-field CSV in 39.8 ms, with no source errors; the temporary clone
was removed. An unbounded repository selector hit `file_limit_exceeded` at
`max_files=30`, despite finding supported assets. This is a Git clone plus
`local_files` import, **not** a native GitHub API connector or commit-aware
sync. The [pinned GitHub adapter](github-adapter.md) supports an exact-file API
path; it does not provide automatic branch tracking or whole-repository sync.

Focused local connector tests finished with **65 passed, three skipped and one
deselected**. The deselected Iceberg credential test imports optional
`pyiceberg`, which is absent from this lean local virtual environment. A
separate optional-extra Iceberg run is still required before calling that
connector locally exercised in this pilot.

## Next acceptance cases

Run every case with a public or synthetic seed, a source-scoped read-only
identity, and a saved source revision. Record discovery time, asset/field
counts, incomplete/error flags, agent draft/abstain counts, owner decisions,
source writes, and warm read p50/p95. Test both a correct answer with exact
evidence and a question that should be refused. Never count an imported
description, a source column name, or a model guess as an approved metric.

| Case | Seed and expected result | Current route |
| --- | --- | --- |
| L01 local structured | CSV, JSON and JSONL with IDs, nullable fields and one documented join; field types, samples and join candidates retain provenance; agent abstains on undocumented metric definitions. | Native `local_files`; mixed drill passed the three formats. |
| L02 local documents | Markdown, text, HTML, text PDF and DOCX with conflicting policy wording; citations retain line/page/paragraph basis, and the owner rejects a false proposal. | Native `local_files`; extraction passed. Scanned PDF/OCR is still unsupported. |
| L03 review and drift | Change one file after publication; stale snapshot/proposals must refuse and the owner must accept a new exact revision. Withdrawn meaning must stop appearing in agent-ready results. | Existing review/recovery drill; add a mixed-source drift case. |
| L04 public GitHub | Pin a public commit, select README and CSV, refresh twice, then advance the commit; compare identity, completeness and change signals. | Local clone was tested; the later GitHub adapter supports exact `.md`, `.txt`, and `.py` files at pinned commits. CSV and revision-advance checks remain pending. |
| L05 SQL | Seed a local PostgreSQL database or a Neon Free project with sales tables and a documentation table; read metadata as a restricted role, then deny a private schema. | Native PostgreSQL metadata. Rows and metric calculations require the agent's separate authorized source tool. |
| L06 catalog | Seed a local Iceberg REST catalog with two tables and a schema change; verify field IDs, snapshot IDs and drift. | Native Iceberg metadata; local catalog run pending. |
| L07 object documents | Put text PDF and JSON in a disposable S3-compatible bucket; verify selected prefix, no out-of-prefix access, and no silent OCR. | Native `object_store`; local S3-compatible run pending. |
| L08 BigQuery | Use one public dataset/table and short-lived read-only token; verify exactly selected fields, no row read/query, and expired-token refusal. | Native metadata discovery of one public table passed; self-hosted worker path pending. |
| L09 Google Drive | Share one test folder to a dedicated read-only service account; include Google Doc and text PDF plus an inaccessible sibling. Verify exact folder scope and extraction limits. | Native connector; offline transport tests exist, real folder test pending. |
| L10 cross-source meaning | Same customer ID in SQL, local CSV and a policy document; ask for a join, a metric with explicit grain, a glossary definition and an unsupported claim. Review exact evidence before agent consumption. | Multi-source curation is supported; a fresh mixed live agent quality run is pending. |
| L11 scale | Repeat at 10, 40 and 100 assets with 1/5/10 concurrent reads. Report refresh duration, warm p50/p95, memory, error rate and restore time; state host and image IDs. | `scripts/measure_installation.py` supplies bounded HTTP and resource measurements. No larger run was started while this host's swap was growing. |

## What can be tested without an enterprise account

| Requested source | No-cost pilot route | Direct connector today? |
| --- | --- | --- |
| Snowflake | A [30-day trial](https://docs.snowflake.com/en/user-guide/admin-trial-account) is time-limited; leave it for the final vendor qualification after local cases. | Yes, schema metadata; live account pending. |
| BigQuery | [Sandbox](https://docs.cloud.google.com/bigquery/docs/sandbox) can use public datasets without a billing account. The local pilot read one public table's metadata. | Yes, metadata only; one direct live call passed. |
| Parquet, Avro | Create files locally for free. Test a conversion to CSV/JSON as an interim import, but preserve the original-format gap. | No local reader for either format. Iceberg metadata does not read their data files. |
| JSON, PDF and other raw files | Use local files; CSV, JSONL, Markdown, text, HTML and DOCX are also recognized. | Yes for listed formats; PDF requires extractable text and has no OCR. |
| Iceberg | Use a disposable local REST catalog and small object store. | Yes, table/schema metadata only. |
| Redshift | Avoid metered serverless resources in the first pilot; import a scoped catalog export if needed. | No native policy/connector. |
| MySQL | Run a disposable local Community database, export a scoped catalog manifest, or implement and qualify a read-only native policy. | No; the generic SQL provider deliberately refuses MySQL. |
| MongoDB | [Atlas Free](https://www.mongodb.com/docs/atlas/tutorial/deploy-free-tier-cluster/) or local Community Server; import a scoped export for now. | No native connector. |
| Databricks | [Free Edition](https://docs.databricks.com/aws/en/getting-started/free-edition) has quotas; export table/semantic metadata with an authorized adapter. | No direct connector; portable catalog/semantic import exists. |
| Looker / Looker Studio | Use a public or personal model export. Treat enterprise Looker and no-cost Looker Studio as separate products. | No direct connector. |
| Power BI | [Free personal workspace](https://learn.microsoft.com/en-us/power-bi/fundamentals/end-user-license) can hold a test model, but sharing needs paid capacity/license rules; import an export. | No direct connector. |
| GitHub repository | Public read-only clone is free and needs no account. Pin a commit and select file globs. | Local clone or the later exact-file GitHub API adapter; neither offers whole-repository sync. |
| Google Drive | A personal account has [15 GB shared storage](https://support.google.com/accounts/answer/6374270); share only a test folder to a read-only service account. | Yes; live folder qualification pending. |
| OneDrive | A [personal free account](https://www.microsoft.com/en-us/microsoft-365/onedrive/online-cloud-storage) is enough to test an export or mounted folder. | No native connector. |
| Neon | [Free Postgres plan](https://neon.com/pricing) is suitable for a small scoped SQL seed; a local PostgreSQL instance is simpler for the first repeatable run. | Yes through PostgreSQL metadata, not a Neon-specific adapter. |
| S3-compatible storage | Run MinIO locally, with a disposable test bucket and selected prefix. | Yes, object metadata and opt-in supported document extraction. |

The pilot used one disposable Knowledge Layer stack at a time, respected host memory pressure and preserved unrelated services. These local measurements do not establish a customer hardware footprint. No billed cloud resources were provisioned.