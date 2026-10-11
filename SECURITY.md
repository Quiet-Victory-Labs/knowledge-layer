# Security policy

This repository includes a local demonstration and an OIDC pilot profile. It is not yet qualified for multi-tenant or source-ACL-sensitive enterprise deployment. See [the pilot limits](docs/oidc-pilot.md) before connecting customer systems.

Do not include credentials, connection URLs, access tokens, source records, or private documents in issues, pull requests, screenshots, or agent prompts. Store deployment secrets in the customer's secret manager and pass only named references to connectors.

Optional decision or generative model providers may receive bounded evidence summaries, which can still contain confidential source text. The reference worker requires explicit `KNOWLEDGE_DECISION_ALLOW_REMOTE=1` for a remote decision endpoint and `KNOWLEDGE_MODEL_ALLOW_REMOTE=1` for a remote generative endpoint. Operators must approve provider destinations and source egress under their own policy; the model's choice or confidence never authorizes publication.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/Quiet-Victory-Labs/knowledge-layer/security/advisories/new)
to report a vulnerability privately to the repository maintainer, Eric Kittelson
(@erickittelson). Send the affected commit or version, the violated boundary,
and a minimal reproduction using synthetic data. Do not include credentials or
customer records. Keep vulnerability details private until a fix and disclosure
plan are ready.

The alternative email contact is **[hello@quietvictorylabs.com](mailto:hello@quietvictorylabs.com)** with the
subject `Knowledge Layer security report`. This is the existing contact
published on [QVL's website](https://www.quietvictorylabs.com/); Eric Kittelson is
the current repository administrator. Send the affected commit or version,
the violated boundary, and a minimal reproduction using synthetic data.
Do not send credentials or customer records. Do not put a vulnerability in an
issue, pull request, discussion, or ordinary support ticket. The administrator
coordinates a restricted fix and rotates any exposed credential.

This address is publicly documented; inbox delivery, monitoring and escalation
have **not yet been verified for this project**. Before relying on email, the
maintainer must send the content-free test in
[the release review packet](docs/ga-review-packet.md), confirm private receipt,
record the monitoring owner and complete the acknowledgment check. Use the
GitHub private reporting route in the meantime. Enabling that route establishes
private submission availability, not a guaranteed response time.

For a suspected credential leak, rotate the affected credential at its source and review access logs. For a suspected authorization bypass, disable remote exposure until the boundary is verified. The local demo bearer token protects only its agent endpoints; never publish demo mode to the internet.
