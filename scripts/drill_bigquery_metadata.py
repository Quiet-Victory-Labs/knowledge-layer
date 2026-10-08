"""Qualify one real restricted BigQuery table without rows, writes or key files.

Uses an existing operator-approved service account and short-lived impersonation.
This command never creates accounts or IAM bindings. Receipt contents are counts,
permission outcomes and digests; metadata, tokens and driver errors stay private.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx

from knowledge_layer.connector_isolation import isolated_curator


def _scoped_access_token(service_account: str, project: str, client: httpx.Client) -> tuple[str, datetime]:
    """Mint a ten-minute, BigQuery-readonly token without a service-account key."""
    caller = subprocess.run(["gcloud", "auth", "print-access-token", "--quiet"],
        capture_output=True, check=True, timeout=45).stdout.decode().strip()
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,32768}", caller):
        raise ValueError("Operator credential is unavailable")
    endpoint = ("https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                + quote(service_account, safe="@.-") + ":generateAccessToken")
    with client.stream("POST", endpoint, json={"scope": ["https://www.googleapis.com/auth/bigquery.readonly"],
            "lifetime": "600s"}, headers={"Authorization": "Bearer " + caller,
            "X-Goog-User-Project": project}, timeout=15) as response:
        if response.status_code != 200:
            raise ValueError(f"Scoped BigQuery credential could not be issued (HTTP {response.status_code})")
        chunks, size = [], 0
        for chunk in response.iter_bytes(4096):
            size += len(chunk)
            if size > 16_384:
                raise ValueError("Scoped credential response is too large")
            chunks.append(chunk)
    payload = json.loads(b"".join(chunks))
    if not isinstance(payload, dict):
        raise TypeError("Scoped credential response is invalid")
    token, raw_expiry = payload.get("accessToken"), payload.get("expireTime")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,32768}", token) or not isinstance(raw_expiry, str):
        raise ValueError("Scoped credential response is invalid")
    expiry = datetime.fromisoformat(raw_expiry)
    remaining = (expiry.astimezone(UTC) - datetime.now(UTC)).total_seconds() if expiry.tzinfo else 0
    if not 60 < remaining <= 660:
        raise ValueError("Scoped credential lifetime is invalid")
    return token, expiry


def run(*, service_account: str, project: str, dataset: str, table: str, denied_table: str) -> dict:
    if (not re.fullmatch(r"[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com", service_account)
            or not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project)
            or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,200}", value) for value in (dataset, table, denied_table))
            or table == denied_table):
        raise ValueError("Qualification target is invalid")
    started = datetime.now(UTC)
    with httpx.Client(follow_redirects=False, trust_env=False) as client:
        token, expiry = _scoped_access_token(service_account, project, client)
    reference = "KNOWLEDGE_SOURCE_QUALIFICATION_BIGQUERY"
    original, original_file = os.environ.pop(reference, None), os.environ.pop(reference + "_FILE", None)
    try:
        with tempfile.TemporaryDirectory(prefix="knowledge-bigquery-") as directory:
            path = Path(directory) / "credential.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump({"type": "short_lived_access_token", "access_token": token,
                    "expires_at": expiry.isoformat()}, handle)
            os.environ[reference + "_FILE"] = str(path)
            config = {"provider": "bigquery", "secret_ref": reference, "schema": project + "." + dataset,
                "tables": [table], "max_tables": 2, "max_columns": 1000, "source_id": "restricted-bigquery-drill"}
            allowed = isolated_curator("sql_catalog", config)
            if allowed.get("status") == "error" or len(allowed.get("assets", [])) != 1:
                raise ValueError("Restricted metadata read failed")
            denied = isolated_curator("sql_catalog", {**config, "tables": [denied_table]})
            if denied.get("status") != "error" or not any(
                    error.get("code") == "database_access_denied" for error in denied.get("errors", [])):
                raise ValueError("Out-of-scope metadata did not deny")
            endpoint = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets/{dataset}/tables/{table}"
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                # Check authorization without reading even a successful response body.
                with client.stream("GET", endpoint + "/data", params={"maxResults": 0},
                                   headers={"Authorization": "Bearer " + token}) as response:
                    row_denial = response.status_code
                if row_denial != 403:
                    raise ValueError("Application-row access was not denied")
            # Rotate to expired authority; native discovery must refuse before HTTP.
            path.write_text(json.dumps({"type": "short_lived_access_token", "access_token": token,
                "expires_at": (started - timedelta(seconds=60)).isoformat()}))
            expired = isolated_curator("sql_catalog", config)
            if expired.get("status") != "error" or not any(
                    error.get("code") == "database_credentials_expired" for error in expired.get("errors", [])):
                raise ValueError("Expired authority did not deny")
            assets = allowed["assets"]
            return {"schema_version": "knowledge-bigquery-qualification-v1", "result": "passed",
                "at": started.isoformat(), "service_account": service_account,
                "scope_digest": hashlib.sha256(json.dumps([project, dataset, table]).encode()).hexdigest(),
                "assets": len(assets), "fields": sum(len(item.get("schema", [])) for item in assets),
                "metadata_read": "passed", "out_of_scope_metadata": "denied",
                "application_rows": "denied", "row_denial_status": row_denial,
                "expired_rotation": "denied", "source_writes": "not_attempted",
                "customer_sso": "not_qualified", "native_field_acl_mirroring": "not_qualified"}
    finally:
        os.environ.pop(reference + "_FILE", None)
        if original is not None:
            os.environ[reference] = original
        if original_file is not None:
            os.environ[reference + "_FILE"] = original_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("service-account", "project", "dataset", "table", "denied-table", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if not output.is_absolute():
        parser.exit(2, "Use a new absolute private receipt path\n")
    # Reserve before any cloud/model activity; symlinks and prior files refuse.
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            try:
                receipt = run(service_account=args.service_account, project=args.project,
                    dataset=args.dataset, table=args.table, denied_table=args.denied_table)
            except Exception:  # noqa: BLE001 - credentials and native diagnostics never enter receipts
                receipt = {"schema_version": "knowledge-bigquery-qualification-v1", "result": "failed",
                           "failure": "restricted_qualification_failed"}
            json.dump(receipt, handle, sort_keys=True)
            handle.write("\n")
        print(json.dumps({"result": receipt["result"], "receipt": str(output)}))
        return 0 if receipt["result"] == "passed" else 1
    except BaseException:
        output.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
