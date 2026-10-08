# Optional Cloud Run / IAP qualification

This profile uses a durable PostgreSQL registry and the optional application IAP
boundary. Google-managed IAP supplies browser sign-in and the HTTPS `run.app`
address. See [application configuration](../../docs/iap-cloud-run.md) and
[Google's direct IAP setup](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run).

Use an isolated project/repository, one standard build with a finite timeout,
explicit runtime identities and named Secret Manager versions. The runtime needs
Cloud SQL connection permission and access only to the application database URL,
IAP session key, identity policy and source policy. The build identity needs only
its private build bucket and image repository. Neither receives customer source
credentials or a project administrator role. Use a dedicated database; the
one-time `python -m knowledge_layer.registry_bootstrap` operator job receives a
separate bootstrap URL, creates a bounded nonsuperuser application role and
refuses connection overrides, unexpected database identity or retained user
objects. PostgreSQL 18 ownership transfers use a temporary role-assumption grant
and schema authority, revoked transactionally before completion. This is not an
upgrade or an existing-database rotation command. The local PostgreSQL 18.6
privilege test passes; Cloud SQL and real IAP sign-in remain unqualified.

Run `python -m knowledge_layer.migrate upgrade` explicitly using the application
role. Migration success and schema comparison are required before managed app
startup. Then deploy the immutable image digest:

```sh
gcloud run deploy knowledge-layer \
  --project=YOUR_PROJECT --region=us-central1 \
  --image=YOUR_REPOSITORY/app@sha256:YOUR_VERIFIED_IMAGE_DIGEST \
  --service-account=YOUR_RUNTIME_SERVICE_ACCOUNT \
  --add-cloudsql-instances=YOUR_PROJECT:us-central1:YOUR_REGISTRY_INSTANCE \
  --env-vars-file=cloud-run.env.yaml \
  --set-secrets=DATABASE_URL=database-url:1,KNOWLEDGE_IAP_SESSION_KEY=session-key:1,KNOWLEDGE_IAP_IDENTITIES=iap-identities:1,KNOWLEDGE_SOURCE_POLICY=source-policy:1 \
  --port=8000 --cpu=1 --memory=512Mi --concurrency=20 \
  --min-instances=0 --max-instances=1 --timeout=30s \
  --no-allow-unauthenticated --iap
```

Copy [the YAML example](cloud-run.env.yaml.example) to `cloud-run.env.yaml` and
replace its operator values; put only non-secret settings in it. Grant invocation on this service to the IAP service
agent, and resource-scoped IAP access to explicit browser and agent callers.
The application allowlists are an additional independent requirement. Discover
the service URL, configure its exact public origin, and verify real sign-in,
signed subject mapping, unauthenticated denial, browser CSRF, source denials and
explicit agent access before enabling any source. A successful deployment alone
is not qualification. [Google's service-account signed JWT method](https://docs.cloud.google.com/iap/docs/authentication-howto)
works without distributing a service-account key; authorize signing on only the
chosen agent identity. A Google-managed IAP client does not provide ordinary
user programmatic OAuth exchange.

Cloud Run is not a Docker host. Keep source IO disabled in this profile until a
separately qualified container worker can enforce its source credential,
filesystem, egress and metadata-service boundaries. The self-hosted worker
profile is in [source-worker-isolation](../../docs/source-worker-isolation.md).
A small zonal database is a cost-bounded qualification choice, not an HA promise.
Strict image vulnerability, license, exact-commit CI and signed release gates
still apply; do not describe a failing candidate as a production release.

## Ephemeral cost cleanup

For testing, record every created resource before starting. Use job deadlines
and zero retries; do not schedule recurring jobs or retain an always-on service.
Disable build-bucket soft deletion before removing its objects, otherwise deleted
objects can continue to incur storage charges. At test completion:

1. Cancel outstanding executions/builds and delete only test-owned jobs/services.
2. Disable deletion protection and backup retention on the test registry, then
   delete it without a final backup. Verify no retained test backups remain.
3. Delete the test image repository, all build-bucket objects and the bucket.
4. Delete the test secrets, remove the temporary IAM bindings and service accounts.
5. Query each inventory again and retain a non-secret cleanup receipt. Preserve
   unrelated services, databases, source roles and objects.

Cleanup prevents continued test-resource billing; usage incurred during the test
is still billable. Enabled APIs and free control-plane records alone are not a
retained running deployment. This project's 2026-10-05 test was explicitly
requested to be ephemeral; do not infer permission to keep its paid resources.
