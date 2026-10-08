<p align="center">
  <img src="assets/knowledge_layer_logo.jpg" alt="Knowledge Layer Logo" width="80" height="80" style="border-radius: 16px" />
</p>

# Knowledge Layer Documentation

Welcome to the technical architecture, operational runbooks, and integration documentation for Knowledge Layer.

The [public fixture provenance](public-fixture-provenance.md) identifies synthetic
examples, excluded private pilot material and the limits of the sanitation record.

---

## 🏛️ Architecture & System Design
- [Product Architecture & Trust Boundaries](architecture.md) — Source access, retention choices, publication gates, and organization scope.
- [Capability Support Matrix](capability-support-matrix.md) — Comprehensive breakdown of supported connectors, protocols, and interfaces.
- [Collective Knowledge Graph](collective-knowledge-graph.md) — Multi-source semantic reconciliation and shared workspace entity graphs.
- [External Connector Adapter Contract](external-connector-protocol.md) — Protocol specifications for external data connector workers.
- [Platform Completion & Research Plan](platform-completion-plan-2026-10-03.md) — Architectural roadmap and technical milestones.

---

## 🔌 Connectors, Lakehouses & Catalogs
- [Catalog & Lakehouse Integrations](catalog-integrations.md) — Apache Iceberg REST, Polaris, Lakekeeper, Unity Catalog, Alation, Collibra, Atlan, and DataHub.
- [Cloud Catalog Connectors (Snowflake & BigQuery)](cloud-catalog-connectors.md) — Provisioning and executing warehouse discovery.
- [Remote Document Connectors & Google Drive](remote-document-connectors.md) — Parsing unstructured PDFs, Markdown, and corporate documents.
- [Pinned GitHub Repository Adapter](github-adapter.md) — Source code and repository metadata integration.
- [Native Semantic Adapters](native-semantic-adapters.md) — Dialect-specific metadata profiling adapters.
- [Source Metadata Grants](source-access.md) — Audience boundaries and granular access grants.

---

## 🤖 Agent Integrations & Model Context Protocol (MCP)
- [Agent Skill & MCP Setup](agent-skill-install.md) — Configuring Claude Desktop, Cursor, Codex, and Gemini with Knowledge Layer MCP tools.
- [Agent Build, Review & Consumption Flow](agent-build-flow.md) — End-to-end workflow for agents proposing and consuming semantic contracts.
- [Semantic Contract Agent Proposal Loop](semantic-contract-agent-loop.md) — Machine-readable protocol for agent-proposed glossary and metric assertions.
- [Model-Neutral Curation](model-neutral-curation.md) — Framework-agnostic agent curation runners and decision adapters.
- [Bounded Agent Consumption](bounded-agent-consumption.md) — Guardrails preventing unbounded token consumption and agent divergence.

---

## ⚖️ Governance, Curation & Metrics
- [Company Guidance & Operational Rules](company-guidance.md) — Normative guidelines and procedure management.
- [Source Publication for Agents](source-publication.md) — Exact owner review and publication of current source context.
- [Governance Audit Events & Ledger Export](audit-events.md) — Registry event history and exports for independently retained audit logs; database administrators can alter the ledger.
- [Composite & Governed Metrics](composite-metrics.md) — Planning and structuring typed dimensional metrics and formulas.
- [Autonomy & Delegation Policies](autonomy-policies.md) — Guardrails for automated curation and human-in-the-loop review.

---

## 🚀 Deployment & Security Operations
- [Self-Hosted Single-Box Runtime](self-host.md) — Running `./knowledge-layer start` with Docker Compose.
- [Container Runtime & Security Policy](container-runtime.md) — Rootless container architecture, security profiles, and health checks.
- [Kubernetes / Helm Deployment](cloud-deployment.md) — Deployment templates, ingress routing, and customer qualification requirements.
- [Organization OIDC Authentication](oidc-pilot.md) — OpenID Connect pilot configuration, scoped Keycloak proof, and customer identity/permission qualification limits.
- [Durable Source Refresh Jobs](refresh-jobs.md) — Background worker orchestration and scheduled discovery.
- [Secret Candidate Scanning](secret-scanning.md) — High-entropy secret scanning runbooks.

---

## 🧪 Hardening & Benchmark Reports
- [Release Readiness & GA Checklist](release-readiness.md) — Validation criteria for enterprise deployment.
- [Enterprise Release Hardening Evidence](enterprise-release-hardening-2026-10-04.md) — End-to-end stress-test and conformance receipts.
- [Dependency License Review](dependency-license-review.md) — License compliance audits across Python and Node dependencies.

- [Public source alpha preparation](public-alpha-readiness.md) — prepared scope and remaining review decisions.
