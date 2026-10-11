<p align="center">
  <img src="assets/knowledge_layer_logo.jpg" alt="Knowledge Layer" width="80" height="80" />
</p>

# Knowledge Layer documentation

Connect scoped sources, build semantic context with your agents, and review the
evidence before publishing knowledge for reuse. This is a self-hosted source
alpha; the [capability matrix](capability-support-matrix.md) distinguishes
available features, local checks and customer qualification.

## Start here

1. [Install and build your first reviewed knowledge](getting-started.md) with
   bundled synthetic examples, then connect the agent client you already use.
2. [Follow the build, review and consumption flow](agent-build-flow.md) to move
   from evidence-backed drafts to current published context.
3. [Explore the knowledge graph](collective-knowledge-graph.md) to trace terms,
   relationships and decisions back to their sources.
4. [Choose a connector](catalog-integrations.md) and check its
   [qualification limits](native-connector-qualification.md) before adding your data.
5. [Ask a usage question](https://github.com/Quiet-Victory-Labs/knowledge-layer/discussions),
   [report a bug or propose a feature](https://github.com/Quiet-Victory-Labs/knowledge-layer/issues/new/choose),
   or [contribute](../CONTRIBUTING.md). Report vulnerabilities
   [privately](../SECURITY.md).

## Install and choose an agent client

- [Self-hosting](self-host.md) — owner setup, local source folders, client configuration, backup and recovery.
- [Agent skill and Model Context Protocol (MCP) setup](agent-skill-install.md) — portable skills and client configuration; confirm discovery and trust in your chosen client.
- [Durable agent builds](durable-agent-builds.md) — selected source scope, saved work, leases and optional customer-owned workers.
- [Agent build, review and consumption](agent-build-flow.md) — draft claims, inspect exact evidence, accept selected claims and publish current context.
- [Bounded agent consumption](bounded-agent-consumption.md) — scoped retrieval and response budgets.
- [Model-neutral curation](model-neutral-curation.md) — optional decision adapters and curation runners.

## Understand semantics and evidence

- [Architecture and trust boundaries](architecture.md) — source access, retained evidence, publication and organization scope.
- [Collective knowledge graph](collective-knowledge-graph.md) — observed structure, semantic bindings, provenance and candidate shared terms.
- [Semantic contract proposal loop](semantic-contract-agent-loop.md) — typed agent proposals for glossary, grain, joins and metrics.
- [Composite and governed metrics](composite-metrics.md) — typed definitions and query guidance; source tools still execute and verify results.
- [Offline Resource Description Framework (RDF) validation](rdf-validation.md) — Shapes Constraint Language (SHACL) checks graph structure, independently of business meaning and approval.
- [Source publication](source-publication.md) — human review, current snapshot eligibility and withdrawal.
- [Company guidance](company-guidance.md) — versioned procedures and rules with a separate review and publication path.
- [Autonomy and delegation policies](autonomy-policies.md) — bounded delegated work and approval boundaries.
- [Audit events and ledger export](audit-events.md) — recorded registry decisions; database administrators can alter the ledger.

## Connect sources

- [Catalog and lakehouse integrations](catalog-integrations.md) — native metadata paths and selected catalog export translations.
- [Cloud catalog connectors](cloud-catalog-connectors.md) — Snowflake and BigQuery setup, transport and qualification boundaries.
- [Remote documents and Google Drive](remote-document-connectors.md) — supported document paths and explicit retention choices.
- [Pinned GitHub adapter](github-adapter.md) — bounded repository metadata and source evidence.
- [Native semantic adapters](native-semantic-adapters.md) — observed database metadata and dialect-specific limits.
- [Source metadata grants](source-access.md) — registry audiences and source-level grants; these do not mirror upstream row or document permissions.
- [External connector protocol](external-connector-protocol.md) — versioned worker responses, provenance and bounded coverage.
- [Offline adapter conformance](adapter-conformance.md) — validate a synthetic or protected response without a live vendor account.

## Operate your installation

- [Container runtime](container-runtime.md) — runtime profiles and health checks.
- [Cloud and Helm deployment](cloud-deployment.md) — evaluation templates and deployment responsibilities.
- [Organization authentication](oidc-pilot.md) — OpenID Connect (OIDC) pilot setup and identity qualification limits.
- [Durable refresh jobs](refresh-jobs.md) — discovery workers, scheduling and failure handling.
- [Source worker isolation](source-worker-isolation.md) — keep restricted credentials on approved workers.
- [Schema migrations](schema-migrations.md) — controlled upgrades and drift checks.
- [Secret candidate scanning](secret-scanning.md) — tracked-file and history checks with explicit limits.
- [Repository operations](repository-operations.md) — public source controls and separation from private development history.

## Contribute and assess readiness

- [Contributing](../CONTRIBUTING.md), [support](../SUPPORT.md) and [governance](../GOVERNANCE.md) — development, community routes and maintainer decisions.
- [Local qualification runbook](local-qualification-runbook.md) — reproduce the scoped integration checks.
- [Capability matrix](capability-support-matrix.md) — support and evidence by capability, without treating a local drill as customer deployment proof.
- [Enterprise release hardening evidence](enterprise-release-hardening-2026-10-04.md) — dated stress and conformance results.
- [Release readiness](release-readiness.md) and [release process](../RELEASING.md) — exact-commit checks and separate signed binary requirements.
- [Dependency license review](dependency-license-review.md) — notice inventory and rights review inputs; collection is not legal approval.
- [Public source alpha record](public-alpha-readiness.md) — sanitized public scope and remaining review work.
- [Public fixture provenance](public-fixture-provenance.md) — synthetic examples, excluded private material and the limits of sanitation.
- [Platform completion and research plan](platform-completion-plan-2026-10-03.md) — dated roadmap and open technical work.
