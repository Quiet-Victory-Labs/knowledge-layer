# Optional direct Cloud Run IAP authentication

`KNOWLEDGE_AUTH_MODE=iap` is an alternative managed authentication boundary for one organization. Demo and OIDC remain available. IAP performs upstream Google authentication; the application independently verifies the signed `X-Goog-IAP-JWT-Assertion` on every browser, static-file, agent HTTP and MCP request. Cookies and bearer tokens cannot authenticate a direct request that lacks that assertion. Only GET/HEAD requests to `/api/health` and `/api/ready` are exempt.

Google specifies ES256, issuer `https://cloud.google.com/iap`, its public IAP signing keys and a direct Cloud Run audience of `/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME`. The implementation verifies signature, exact audience, issuer, subject, issued-at and expiry; it allows 30 seconds of clock skew and refuses lifetimes above 660 seconds. Its fixed-origin key cache has a five-minute lifetime, finite request/response budgets, and failure backoff. See [Google's signed-header requirements](https://docs.cloud.google.com/iap/docs/signed-headers-howto).

Configure these operator settings, preferably through owner-only secret files:

| Setting | Purpose |
| --- | --- |
| `KNOWLEDGE_IAP_AUDIENCE` | Exact direct Cloud Run service audience, using the project number. |
| `KNOWLEDGE_IAP_SESSION_KEY_FILE` | At least 32 random characters for the signed browser CSRF session. |
| `KNOWLEDGE_IAP_IDENTITIES_FILE` | The explicit application identity policy below; regular file, owner-only permissions, at most 64 KiB. |
| `KNOWLEDGE_OIDC_ALLOWED_GROUP` | Local general-access group name, also used by the existing source-grant system. |
| `KNOWLEDGE_OIDC_STEWARD_GROUP` | Distinct local steward group name. It is assigned only to an explicitly listed verified identity. |
| `KNOWLEDGE_PUBLIC_URL` | Exact HTTPS application origin. It may be omitted during initial service discovery; browser writes and remote MCP host acceptance remain disabled until configured. |
| `DATABASE_URL_FILE`, `KNOWLEDGE_SOURCE_POLICY_FILE` | Customer-managed PostgreSQL and the existing operator source policy. The schema must already be migrated. |
| `KNOWLEDGE_ENABLED_KINDS` | Explicit connector allowlist. Existing agent source-management and allowed-kind settings remain separate. |

No OIDC client ID, OAuth client secret or application callback is needed by this mode. Cloud-side IAP setup and IAM are separate deployment requirements. Enable IAP directly on the Cloud Run service, grant the IAP service agent Cloud Run invocation, and grant intended callers IAP access. Google notes that initial setup without an organization can require the Cloud Console; enabling IAP does not automatically prove that an outside identity can sign in. See [Google's direct Cloud Run IAP instructions](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run).

The identity file requires exactly this schema:

```json
{
  "version": 1,
  "allowed_subjects": [],
  "allowed_emails": ["owner@example.com"],
  "steward_subjects": [],
  "steward_emails": ["owner@example.com"],
  "agent_subjects": [],
  "agent_emails": ["approved-agent@example-project.iam.gserviceaccount.com"]
}
```

Each array accepts at most 256 unique bounded selectors, with a combined budget of 512. A browser audience is required; the agent audience may be empty to disable that surface. Subject matching is exact. Email matching uses only the signed JWT email and compares case-insensitively. The unsigned `X-Goog-Authenticated-User-*` convenience headers never supply identity or roles, and signed `groups` or `scope` claims cannot add local authority. The application reloads this file on each request, so withdrawing an entry denies the next request.

A signed-email entry can bootstrap access before the stable subject is known. Read `/auth/me`, then provision source approvals and direct source grants with its returned canonical `subject`. The application never replaces that subject with the email. Queued refresh requires both a direct user `manage` grant and explicit subject approval in the source policy; a steward group alone is insufficient. Registry grants and optional source permission attestations continue to control metadata access.

Browser clients obtain an identity-bound CSRF token through `/auth/me`. The session cookie is signed, secure, HttpOnly and SameSite=Lax. Every browser write requires that cookie, its matching `X-Knowledge-CSRF` token and an `Origin` exactly equal to the configured public URL. Changing the asserted subject invalidates the previous token. `/auth/logout` clears this application session; it does not sign the user out of upstream IAP.

Agent requests need their own verified assertion and an explicit agent audience entry. They receive only the configured agent scope and explicitly mapped local roles. Source reads still require source grants; source management and human review retain their existing gates. A browser identity does not implicitly gain agent access. The upstream agent client authentication needed to reach IAP must be configured independently.

This document describes reusable local implementation and deployment configuration. A test deployment does not establish a retained production installation. For ephemeral cloud qualification, use a bounded build and delete task-created services, jobs, databases, secrets, image artifacts and storage afterward; preserve unrelated resources. Local cryptographic and request tests do not substitute for a real IAP browser/agent journey or confirm cloud IAM and cleanup.
