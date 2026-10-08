# Enterprise withdrawal qualification

On October 3, 2026, the platform closed authority-reduction and consumer-currentness gaps found while qualifying reviewed publication delegation. The implementation is `e4cee5d`; disposable PostgreSQL removal additionally uses the bounded wait in `beae6f1`, and safe cold-start diagnostics are at `6998956`. No runtime service, model dependency, schema migration or source execution permission was added. The registry remains at `0013_autonomy`.

## Behavior

| Situation | Result |
| --- | --- |
| Operator removes connector approval | Refresh, policy draft/approval and delegated apply refuse. A currently granted source manager can inspect retained policy; a real steward with source manage authority can withdraw it. Removing connector approval does not itself revoke retained metadata grants. |
| Policy revision content is corrupt | Inspection and consumption refuse integrity failure. Withdrawal remains available with the exact approved active head revision/digest, without returning corrupt content or granting new authority. |
| Steward loses read access to a separate guidance dependency | Withdrawal still requires the published audience, real steward and current manage grants for every declared source scope. It returns only a publication handle and withdrawal state, without retained instructions, dependency identifiers or rationale. |
| Document service fails or returns incomplete discovery | The last complete snapshot and completion clock remain available for inspection. Dependent reviewed assertions become ineffective, cited glossary context disappears and publication readiness suspends. |
| Harness resolves a fresh blocked packet | Revalidation refuses even when the signed context pin is unchanged. A successful check additionally reports context status and answerability; an unmatched question remains `not_established`. |

The guidance change preserves immutable revision, review and source-publication history. Idempotent withdrawal records one audit event; an audit failure rolls the mutation back. Consumer receipt refusal is exercised through HTTP and both MCP surfaces.

## Disposable runtime evidence

The PostgreSQL drill completed **16 checks** against the clean `beae6f1` checkout. It uses a nonce-scoped table and SELECT-only role through the native connector. Database readbacks verify one attributed run/publication and exact budget usage, concurrent retry behavior, grant-revocation lock ordering, late-expiry rollback, successful inspection/withdrawal after connector approval removal, preserved reviews/source clocks, populated registry backup/restore covering all 23 persisted tables, and complete fixture cleanup. Receipt: ignored `.local/withdrawal-qualification/postgres-beae6f1.json`.

The first stricter teardown run passed all database checks but reported failure because container absence was not yet verified after `docker stop`. Its receipt is retained separately. Teardown now uses bounded polling for Docker's automatic removal and still requires verified absence. Offline tests cover delayed removal, persistent residue, failed inspection/listing, changed ownership and failed stop. The replay passed; only unrelated pre-existing containers remained.

The real disposable Keycloak 26.7.5/PostgreSQL 18.6 drill passed against the clean `6998956` checkout. Its separate confidential `knowledge-delegation-service` client uses actual `client_credentials` JWTs with the exact service subject, direct source manage grant and operator subject approval. Browser authorization code/PKCE plus CSRF authenticates an owner who independently reviews meaning and approves the delegation. HTTP creates one publication; compact and advanced MCP retry the same run using protocol `2026-07-28`. Missing autonomy scope, wrong subject, withdrawn direct grant, manual publication withdrawal and policy withdrawal refuse across all three surfaces. Persisted usage, run and independent review counts each remain one; publication/run actors match the service subject.

That IdP receipt verifies the source fixture is unchanged, both ownership-labeled containers are absent and temporary credentials are removed. It is mode `0600`: ignored `.local/withdrawal-qualification/oidc-6998956.json`. The harness is a model-free custom HTTP/MCP ASGI client against a real disposable IdP, not native Codex/Claude activation or deployed customer HTTPS qualification. Existing login, logout, invalid signature/audience, fresh role changes and natural token expiry checks remain. Old signed role claims remain effective until normal token expiry unless registry grants are withdrawn.

## Regression and distribution evidence

The detached, clean `6998956` checkout passed **1,289 tests**. The imported module path was read back from that checkout. Ruff and the secret gate passed; scanner rules and candidate hashes remained unchanged, with only baseline line locations updated. SQLite emits the existing Alembic expression-index reflection warning; it did not skip test collection.

Fresh unsigned wheel/source archives built from `beae6f1` passed independent base-only installation and the packaged synthetic consumer example outside the checkout. The example performs independently read-only fixture queries, preserves zero versus unknown measures, refuses combining incompatible cohorts and rejects a withdrawn receipt. Checksums and install receipts are retained in ignored `.local/withdrawal-distributions-beae6f1/`. No artifact was published or signed.

The first implementation CI run passed Python, packaging, local integration and autonomy integration; its IdP job returned a generic setup `RuntimeError`. That log cannot distinguish image download, launch, readiness or cleanup. Follow-up drill diagnostics use fixed safe phase/error codes and separate 180-second pulls of the exact two images from 45-second launches with `--pull=never`; 38 offline tests and the final local replay passed. Final CI qualification is recorded separately on the published PR head.

## Remaining enterprise gates

This increment adds no customer source reads or writes. The runtime sources are isolated synthetic fixtures. Native document/field/row ACL propagation, actual customer IdP/provider mappings, named harness discovery/activation and unattended operation, generic Linux/cloud operating qualification, seven-day endurance, public distribution/contact/license/security review and measured total footprint remain separate gates. The private draft alpha is not an enterprise release certification.

Current workflows: [delegation](autonomy-policies.md), [company guidance](company-guidance.md), [bounded consumption](bounded-agent-consumption.md), [OIDC pilot](oidc-pilot.md). The broader [completion plan](platform-completion-plan-2026-10-03.md) retains those unclosed acceptance gates.
