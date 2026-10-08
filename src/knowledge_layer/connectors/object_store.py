"""Prefix-scoped object metadata and operator-enabled document extraction."""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import suppress
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.config import Config

from knowledge_layer.connectors.common import ConnectorError, stable_id
from knowledge_layer.connectors.remote_documents import (
    DOCUMENT_SUFFIXES as EXTRACTABLE_SUFFIXES,
)
from knowledge_layer.connectors.remote_documents import (
    DocumentLimitExceeded,
    RemoteBudget,
    extract_document,
    metadata_profile,
    operator_content_policy,
)
from knowledge_layer.connectors.secrets import read_source_secret

TABLE_SUFFIXES = {".csv", ".json", ".jsonl", ".parquet", ".avro", ".orc"}
DOCUMENT_SUFFIXES = {".md", ".txt", ".pdf", ".docx", ".pptx", ".html", ".htm"}
BUCKET_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,61}[a-zA-Z0-9]$")


def _profile(secret_ref: str) -> dict[str, Any]:
    raw = read_source_secret(secret_ref, unavailable_code="object_credentials_unavailable")
    try:
        profile = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ConnectorError("object_profile_invalid", "The configured object-store profile is invalid") from exc
    if not isinstance(profile, dict) or not isinstance(profile.get("bucket"), str) or not BUCKET_NAME.fullmatch(profile["bucket"]):
        raise ConnectorError("object_profile_invalid", "The configured object-store profile is invalid")
    endpoint = profile.get("endpoint_url")
    if endpoint is not None:
        try:
            parsed = urlparse(endpoint) if isinstance(endpoint, str) else None
        except ValueError as exc:
            raise ConnectorError("object_profile_invalid", "The configured object-store endpoint is invalid") from exc
        if parsed is None or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/"):
            raise ConnectorError("object_profile_invalid", "The configured object-store endpoint is invalid")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and os.environ.get("KNOWLEDGE_ALLOW_INSECURE_OBJECT_ENDPOINTS") == "1"):
            raise ConnectorError("object_endpoint_tls_required", "The configured object-store endpoint must use HTTPS")
    for field in ("access_key_id", "secret_access_key", "session_token", "region"):
        if field in profile and not isinstance(profile[field], str):
            raise ConnectorError("object_profile_invalid", "The configured object-store profile is invalid")
    if bool(profile.get("access_key_id")) != bool(profile.get("secret_access_key")):
        raise ConnectorError("object_profile_invalid", "The configured object-store profile is invalid")
    operator_content_policy(profile)
    return profile


def _download(client: Any, bucket: str, item: dict[str, Any], maximum: int, budget: RemoteBudget) -> bytes:
    body = None
    try:
        budget.timeout()
        result = client.get_object(Bucket=bucket, Key=item["Key"], IfMatch=item["ETag"])
        body = result.get("Body")
        if (body is None or result.get("ETag") != item["ETag"]
                or type(result.get("ContentLength")) is not int or result["ContentLength"] != item.get("Size")
                or item.get("LastModified") is not None and result.get("LastModified") != item["LastModified"]):
            raise ConnectorError("remote_revision_changed", "Object content differs from its listed revision; retry the source")
        if result["ContentLength"] > maximum:
            raise DocumentLimitExceeded
        chunks, size = [], 0
        while True:
            budget.timeout()
            chunk = body.read(min(64 * 1024, maximum - size + 1))
            if not isinstance(chunk, bytes):
                raise ConnectorError("object_content_unavailable", "The object source returned invalid document bytes")
            if not chunk:
                break
            size += len(chunk)
            budget.consume(len(chunk), content=True)
            if size > maximum:
                raise DocumentLimitExceeded
            chunks.append(chunk)
        if size != result["ContentLength"]:
            raise ConnectorError("remote_revision_changed", "Object content differs from its declared revision; retry the source")
        return b"".join(chunks)
    except (ConnectorError, DocumentLimitExceeded):
        raise
    except Exception as exc:
        if getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode") == 412:
            raise ConnectorError("remote_revision_changed", "Object revision changed during discovery; retry the source") from exc
        raise ConnectorError("object_content_unavailable", "Object document content could not be read") from exc
    finally:
        if body is not None:
            with suppress(Exception):
                body.close()


def _listing(config: dict[str, Any], profile: dict[str, Any], result: Any, client: Any, budget: RemoteBudget) -> dict[str, Any]:
    prefix, max_objects = config.get("prefix", ""), config.get("max_objects", 100)
    retained, max_bytes, _total_bytes = operator_content_policy(profile)
    if not isinstance(result, dict) or type(result.get("IsTruncated")) is not bool:
        raise ConnectorError("object_listing_invalid", "Object-store response lacks explicit listing completeness")
    contents = result.get("Contents")
    # AWS omits Contents for an explicitly empty listing. A missing body alone
    # cannot establish empty scope on an S3-compatible provider.
    if contents is None and result.get("KeyCount") == 0 and type(result.get("KeyCount")) is int:
        contents = []
    if not isinstance(contents, list):
        raise ConnectorError("object_listing_invalid", "Object-store response lacks a verified object listing")
    keys: set[str] = set()
    for item in contents:
        key = item.get("Key") if isinstance(item, dict) else None
        if (not isinstance(key, str) or not key or len(key.encode("utf-8")) > 1024 or not key.startswith(prefix)
                or key in keys or any(ord(char) < 32 for char in key)):
            raise ConnectorError("object_listing_invalid", "Object-store listing has invalid, duplicate or out-of-scope keys")
        keys.add(key)
    if "KeyCount" in result and (type(result["KeyCount"]) is not int or result["KeyCount"] != len(contents)):
        raise ConnectorError("object_listing_invalid", "Object-store listing count disagrees with its records")
    objects = [item for item in contents if not item["Key"].endswith("/")]
    if result["IsTruncated"] or len(contents) > max_objects:
        # An incomplete listing must not be mistaken for a deletion on refresh.
        raise ConnectorError("object_limit_exceeded", "The selected object prefix exceeds max_objects; narrow the prefix")

    source_id = config.get("source_id") or stable_id("source", "object_store", profile["bucket"], prefix, config.get("name"))
    name = config.get("name") if isinstance(config.get("name"), str) else "Object-store source"
    assets, errors = [], []
    for item in sorted(objects, key=lambda entry: str(entry["Key"])):
        key = str(item["Key"])
        path = PurePosixPath(key)
        # Content-addressed raw snapshots keep the logical filename one level up.
        logical_name = path.parent.name if re.fullmatch(r"[a-f0-9]{64}", path.name) and path.parent.suffix else path.name
        suffix = PurePosixPath(logical_name).suffix.casefold()
        asset_type = "table" if suffix in TABLE_SUFFIXES else "document" if suffix in DOCUMENT_SUFFIXES else "file"
        pointer = f"s3/{profile['bucket']}/{key}"
        revision = stable_id("object_revision", item.get("ETag"), item.get("Size"), item.get("LastModified"))
        asset = {
            "id": stable_id("asset", source_id, pointer),
            "name": logical_name,
            "asset_type": asset_type,
            "source_pointer": pointer,
            "revision": revision,
            "revision_basis": "object_listing_metadata",
            "schema": [],
            "profile": {
                "metadata_only": True,
                "size_bytes": item.get("Size") if type(item.get("Size")) is int and item["Size"] >= 0 else None,
                "row_count": None,
                "row_count_is_complete": False,
                "profile_rows": 0,
                "profile_is_sampled": False,
                "null_counts": {},
                "distinct_counts": {},
            },
            "trust_state": "unverified",
        }
        if retained:
            size = asset["profile"]["size_bytes"]
            status = None
            if suffix not in EXTRACTABLE_SUFFIXES:
                status = "unsupported_document_type"
            elif size is None:
                status = "document_size_unavailable"
            elif size > max_bytes:
                status = "file_size_limit_exceeded"
            elif not isinstance(item.get("ETag"), str) or not item["ETag"] or len(item["ETag"]) > 1024:
                status = "document_revision_unavailable"
            else:
                try:
                    data = _download(client, profile["bucket"], item, max_bytes, budget)
                    asset["profile"], gap = extract_document(logical_name, suffix, data, budget)
                    content_sha = hashlib.sha256(data).hexdigest()
                    asset["revision"] = stable_id("object_content_revision", revision, content_sha)
                    asset["revision_basis"] = "object_listing_metadata_and_content_sha256"
                    asset["observations"] = {"content_sha256": content_sha, "etag": item["ETag"],
                                             "source_instructions_executed": False}
                    if gap:
                        errors.append({**gap, "source_pointer": pointer})
                except DocumentLimitExceeded:
                    status = "file_size_limit_exceeded"
                except ConnectorError as exc:
                    if exc.code != "object_content_unavailable":
                        raise
                    status = exc.code
            if status:
                asset["profile"] = metadata_profile(size, status, enabled=True)
                if suffix in EXTRACTABLE_SUFFIXES:
                    errors.append({"code": status, "message": "Object document content was not extracted; metadata was retained",
                                   "source_pointer": pointer})
        assets.append(asset)
    discovery = {
        "source": {
            "id": source_id, "name": name, "kind": "object_store",
            "location": f"s3/{profile['bucket']}/{prefix}",
            "revision": stable_id("revision", *[(asset["source_pointer"], asset["revision"]) for asset in assets]),
            "trust_state": "unverified",
        },
        "assets": assets,
        "capabilities": {
            "metadata": True, "safe_profiling": retained, "documents": True,
            "change_signals": True, "query_execution": False, "permission_mirroring": False,
        },
        "errors": errors,
    }
    budget.consume_json(discovery)
    return discovery


def discover(config: dict[str, Any]) -> dict[str, Any]:
    if set(config) - {"secret_ref", "prefix", "max_objects", "source_id", "name", "previous_snapshot"}:
        raise ConnectorError("invalid_object_config", "Object connection config contains unsupported fields")
    secret_ref = config.get("secret_ref")
    if not isinstance(secret_ref, str) or not re.fullmatch(r"KNOWLEDGE_SOURCE_[A-Z0-9_]{1,64}", secret_ref):
        raise ConnectorError("invalid_secret_reference", "A named server-side object-store reference is required")
    prefix = config.get("prefix", "")
    if not isinstance(prefix, str) or prefix.startswith("/") or ".." in PurePosixPath(prefix).parts or "\\" in prefix or len(prefix) > 256:
        raise ConnectorError("invalid_object_prefix", "prefix must be a bounded relative key prefix")
    max_objects = config.get("max_objects", 100)
    if type(max_objects) is not int or not 1 <= max_objects <= 500:
        raise ConnectorError("invalid_scan_limit", "max_objects must be between 1 and 500")
    profile = _profile(secret_ref)
    _retained, _max_bytes, total_bytes = operator_content_policy(profile)
    budget, client = RemoteBudget(total_bytes), None
    try:
        client = boto3.client("s3", region_name=profile.get("region", "us-east-1"), endpoint_url=profile.get("endpoint_url"),
            aws_access_key_id=profile.get("access_key_id"), aws_secret_access_key=profile.get("secret_access_key"),
            aws_session_token=profile.get("session_token"),
            config=Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 1}))
        budget.timeout()
        result = client.list_objects_v2(Bucket=profile["bucket"], Prefix=prefix, MaxKeys=max_objects + 1)
        budget.timeout()
        return _listing(config, profile, result, client, budget)
    except ConnectorError:
        raise
    except Exception as exc:
        raise ConnectorError("object_metadata_unavailable", "Object-store metadata discovery failed") from exc
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()
