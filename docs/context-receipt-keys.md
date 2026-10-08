# Context receipts across restarts and replicas

By default, each registry process signs five-minute `context-receipt-v1`
receipts with a random in-memory key. They refuse after restart or on another
process. No extra configuration is needed for a single local process.

An operator can enable `context-receipt-v2` with a shared key ring. Only replicas
of the **same registry**, using the same authentication and source policy, should
share it. Give each deployment a distinct `registry_id` and independently random
keys. The ID is an operator-declared boundary, not proof of database identity.
Never reuse a ring for a cloned registry, another environment or organization.

Generate a private file without printing its key:

```sh
install -d -m 700 "$HOME/.config/knowledge-layer"
cd "$HOME/.config/knowledge-layer"
python3 - <<'PY'
import json, os, secrets
ring = {"version": 1, "registry_id": "acme-company-registry",
        "active_key_id": "initial",
        "keys": [{"id": "initial", "secret": secrets.token_urlsafe(32)}]}
fd = os.open("context-receipt-keys.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as file:
    json.dump(ring, file)
PY
export KNOWLEDGE_CONTEXT_RECEIPT_KEYS_FILE="$PWD/context-receipt-keys.json"
```

Keep the file outside the checkout and readable only by the service's OS user.
Use an owner-only regular file; symlinks, projected Secret-volume links, FIFOs,
public permissions and files over 8 KiB refuse. Alternatively provide JSON in
`KNOWLEDGE_CONTEXT_RECEIPT_KEYS` through an existing secret manager. Configure
one source. Invalid configuration stops startup before database creation.
The Helm chart accepts an existing Secret key through
`existingSecret.contextReceiptKeysKey`; its single-replica qualification scope
is unchanged. Environment-backed rotation requires an application restart.

The exact JSON fields above are required. There may be 1–3 keys with distinct
IDs and distinct canonical unpadded base64url 32-byte secrets. Generate every
key randomly. The active ID must exist. Receipts expose the registry/key IDs,
principal and policy digests, request, clocks and signature; they never expose
keys. Treat receipts as private context references, not public bearer tokens.

The startup mode, registry ID and file location are fixed for that process.
A file-backed ring is read once for each issue/revalidation operation. Atomic
replacement at the service-visible regular-file path permits rotation without
restart. A missing or malformed file refuses; cached keys never substitute.
Qualify the mount's replacement behavior: replacing a host file does not
necessarily update a container's existing single-file bind mount.

For a rolling rotation, first distribute both old and new keys while the old
key remains active. Then switch every signer to the new active key while
retaining the old verifier. Wait at least five minutes after the last old signer
stops, then remove the old key everywhere. Removing it refuses its receipts on
the next request using that ring. For emergency revocation, remove immediately;
environment-backed replicas need restart. In-flight operations use one coherent
ring snapshot. Changing the registry ID requires restart. Keep UTC clocks
synchronized: future issue times, naive clocks and lifetimes over five minutes
refuse, without an implicit clock-skew allowance.

Cryptographic verification is followed by current principal, grants, policy,
source publication, supporting evidence and company-guidance checks. A removed
grant or withdrawn meaning still refuses across replicas. Revalidation returns
a newly issued receipt only when that context is current. A valid receipt does
not authorize SQL execution or establish source-native row/document access.
There is no key-management API or agent tool, and keys never enter registry rows,
snapshots, exports or audit payloads. Holders of a ring are trusted signers.

The regression fixtures cover independent stores/restarts, key rotation/removal,
malformed files, tampering, expiry and authorization/meaning revocation. They
qualify this bounded registry behavior; customer clouds, secret managers and
distributed operational SLOs need their own runtime qualification.

Inventory-aware task packets also use these signing modes. `task-context-v2`
separates exact published catalog meaning from dataset snapshot authority; it
does not change the signing-version definitions above. An optional signed
`inventory_selection` freezes 1–20 exact concept IDs across the selected sources.
Current manifests, local decisions/publications, grants and source bindings are
revalidated. Definitions remain non-executable and source-wide readiness stays
separate. See the [inventory consumer workflow](inventory-concepts.md).
