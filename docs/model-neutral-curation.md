# Model-neutral curation loop

The platform owns source access, bounded profiling, provenance, versioned candidates, and validation. A connected model supplies interpretation. This keeps the knowledge layer independent of Grok, Claude, Codex, Cursor, or a cloud model provider.

## One work cycle

1. A source connects or refreshes. Changed file revisions are reprofiled; unchanged profiles are reused. The current snapshot contains candidate entities, measures, document terms, explicit source assertions, evidence handles, and drift.
2. An agent calls `list_curation_work` over authenticated MCP, or `GET /agent/api/curation-work`. It receives up to three small work packets by default. Changed assets and cross-source label matches are prioritized. Each packet includes the current snapshot ID, target concept, bounded evidence, and a requested action.
3. The agent reasons over one packet and either abstains or calls `propose_semantic_change` / `POST /agent/api/proposals` with a definition or mapping, rationale, cited evidence IDs, and `expected_snapshot_id`. Relationship work includes an exact `proposed_mapping_shape` for a current directed source edge. The platform rejects stale snapshots, invented evidence IDs, and mappings to nonexistent or mismatched edges. Proposals stay pending review; a model cannot certify meaning by submitting one.
4. The next refresh builds new work identities when content changes. Exact label matches across sources appear as `possible_shared_term` suggestions, never as silent merges.

For a desktop agent harness, point its MCP client at the authenticated gateway and instruct it to process the work queue. The MCP tool names and packet schema do not depend on the model. Whether the client runs unattended, and how often it polls, depends on that harness's scheduler and permissions. A model API by itself does not execute tools; it needs a runner. The queue skips generic identifier fields and favors changed assets, shared-term candidates, document terms, and source relationships before less urgent measure candidates.

For a cloud or local model with an OpenAI-compatible Chat Completions endpoint, `scripts/run_model_curator.py` is the optional runner. It polls the same work API, sends one compact packet per model call, caps output at 350 tokens, validates the JSON answer against the packet's evidence, and submits a pending proposal. Set these values through the deployment's secret store or environment:

| Variable | Purpose |
| --- | --- |
| `KNOWLEDGE_GATEWAY_URL` | Authenticated gateway base URL |
| `KNOWLEDGE_AGENT_TOKEN` or `KNOWLEDGE_AGENT_TOKEN_FILE` | Platform bearer credential |
| `MODEL_CHAT_COMPLETIONS_URL` | Full Chat Completions endpoint URL |
| `MODEL_NAME` | Provider's model identifier |
| `MODEL_API_KEY` or `MODEL_API_KEY_FILE` | Provider credential |
| `KNOWLEDGE_MODEL_ALLOW_REMOTE=1` | Explicit permission for the reference worker to send evidence packets to a remote generative model |

`uv run --no-sync python scripts/run_model_curator.py` handles one batch. Add `--watch --interval 30` for a polling worker. Both URLs must use HTTPS, except loopback HTTP for local testing. The CLI requires the remote setting above; direct library callers must pass `allow_remote_model=True`. The bridge does not put credentials in the repository, store model responses as source truth, or send full customer tables. Opt-in semantic samples may still contain source excerpts or identifiers and are sent to the selected model provider, so deployment policy must decide which sources and providers may exchange that content.

## Optional decision triage

The runner first applies small, reproducible rules. Missing evidence is deferred. A relationship with **complete** source and target profiles but zero matching sampled keys goes to steward review; this does not reject the relationship. Other packets proceed to the generative model. These rules add no model dependency or network call.

An optional [Jev-compatible System One endpoint](https://docs.typesafe.ai/primitives/choice) can classify a bounded subset of the packet into `curate`, `needs_steward`, or `defer`. The adapter works with TypeSafe Jev or a separately deployed [compatible Laya server](https://huggingface.co/convaiinnovations/laya). Laya, Torch, and model weights are not part of the core package or container. The adapter never submits proposals or grants approval. It sends up to three short evidence summaries and candidate facts, without explicit source-pointer fields or full rows. A summary can still contain confidential source text; use an external endpoint only when the source's data policy permits that egress.

| Variable | Purpose |
| --- | --- |
| `KNOWLEDGE_DECISION_URL` | Full `/v1/systemone` endpoint; HTTPS or loopback HTTP |
| `KNOWLEDGE_DECISION_MODEL` | Pinned model or checkpoint identifier |
| `KNOWLEDGE_DECISION_API_KEY` or `KNOWLEDGE_DECISION_API_KEY_FILE` | Bearer credential; HTTPS requires one; file must be owner-only |
| `KNOWLEDGE_DECISION_ALLOW_REMOTE=1` | Explicit permission to send bounded metadata to a remote decision endpoint; direct adapter callers pass `allow_remote=True` |
| `KNOWLEDGE_DECISION_CALIBRATION_ID` | Operator's label for the held-out, source-relevant evaluation reviewed before gating |
| `KNOWLEDGE_DECISION_MIN_PROBABILITY` | Selected-option probability floor for gating, from `0.95` to `1`; default `0.98` |

With an endpoint configured, the default `--decision-mode shadow` records its proposed route, provider model, selected probability, input token count, and whether it affected execution. The generative model still handles packets that pass deterministic rules. `--decision-mode gate` lets a high-probability `needs_steward` or `defer` route skip generation **only** when a calibration reference is configured. Lower-probability or malformed responses fall back to the existing generative path. Neither mode can publish a claim. A gated packet remains available in the knowledge work queue for the external agent or steward; the reference worker remembers the skip only for its current process lifetime.

Build a held-out, steward-labeled JSONL file locally. Each record needs a current `packet` and an `expected_route` (`curate`, `needs_steward`, or `defer`). Run `uv run --no-sync python scripts/evaluate_decisions.py /private/labeled-work.jsonl --provider rules` for the baseline, then repeat with `--provider system-one` and a configured endpoint. The report under ignored `.local/` includes confusion counts, false skipped work that needed generation, the number of high-probability skips that would have been wrong, calibration bins, Brier score when probabilities exist, tokens, and latency. The dataset hash is for traceability; a report or calibration ID alone does not certify performance. Use enough real ambiguous and changed-source cases to set an organization-specific threshold, and keep labeled packets outside Git.

[Amazon Bedrock documents an OpenAI-compatible Chat Completions endpoint](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html), including API-key and SigV4 authentication. The reference worker currently accepts a bearer API key; SigV4 needs a separate adapter. [Vertex AI also supports a Chat Completions compatible endpoint](https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/call-vertex-using-openai-library), but production Google authentication needs token refresh or a managed proxy. These provider routes are documented integration paths, not live-tested connections in this demo.

## Current boundary

The platform now prepares work and accepts evidence-bound model proposals. It does **not** silently approve them, host a language model, or launch Codex/Claude/Cursor sessions on its own. The reference runner and decision adapter were tested against fake compatible model endpoints; no live Jev, Laya, OpenAI Decisions API, Grok Bot, Bedrock, Vertex, or Claude account was used. The OpenAI Decisions API has no adapter here yet. Full unattended enterprise operation still needs a configured model provider or harness scheduler, provider-specific credential rotation, scheduled source refresh or change notifications, persisted work receipts, policy-based review/grading, and workload controls.
