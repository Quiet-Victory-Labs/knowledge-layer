# Secret candidate scanning

The public repository enables GitHub secret scanning and push protection. The `python` CI job also runs the open-source `detect-secrets` hook against tracked files without network verification and without printing candidate values to CI logs. The pinned `.secrets.baseline` contains hashes of reviewed synthetic test fixtures, schema commit IDs, and configuration key names; it contains no raw secret values. A new candidate fails CI.

Run `python3 scripts/check_secrets.py` before a pull request. If it fails, run the pinned detector locally to inspect the candidate, remove any real credential, rotate it at the source, and review Git history before updating the baseline. Do not add an actual credential to the baseline or share the detector's raw output in a pull request.

The gate never changes the reviewed `.secrets.baseline`. It scans with a private temporary copy, discards detector diagnostics, and removes the copy after success, refusal, or timeout. Baselines are bounded to 5 MiB. With pinned detector 1.5.0, exit 0 passes; exit 3 passes only after validating that the temporary baseline adds no `(filename, hashed_secret, type)` identities. Known candidates moving lines or disappearing are allowed. New candidates, added hashes, malformed baselines, and other detector exits fail with a redacted message.

This check is one control, not a proof that the repository contains no secrets. It does not verify live credentials or replace a full history scan and GitHub secret scanning and push protection.

The manual alpha release workflow additionally runs `scripts/check_release_history.py`
against every reachable commit tree and commit/annotated-tag message in a complete
fetched clone. It deduplicates blob/path pairs, compares candidates against the
reviewed tracked-file hashes, blocks environment/key/database/private-download
paths and unclassified data fixtures, and refuses binary or oversized objects for
manual review. Its console and release receipt contain aggregate counts only;
optional local candidate receipts contain hashes and object identities with mode
0600, never raw candidate values or customer-like paths. The current checkout is
scanned separately with `scripts/check_secrets.py`; ignored files and unreachable
or deleted remote refs require separate investigation. Pattern scanning cannot
establish customer consent or source-data rights.

```bash
uv run --no-project --with detect-secrets==1.5.0 python scripts/check_release_history.py \
  --output .local/release/history-scan.json \
  --private-candidates .local/history-candidates.json
```

New historical candidates fail publication even if the current tracked-file gate
passes. Review the exact historical fixture and remove/rotate any real credential
before updating an approved synthetic baseline. Do not rewrite shared Git history
or automatically bless candidates just to obtain a passing release gate.

`.release-history-baseline.json` records independently inspected synthetic
environment-variable references and deliberately malformed/refusal/redaction test
canaries found in older test blobs. Each exemption binds the exact immutable blob,
path hash, candidate hash and detector rule. A renamed or changed blob receives no
historical exemption. It contains no raw values and never approves live credentials.
