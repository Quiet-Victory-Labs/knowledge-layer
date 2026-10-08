"""Folder-scoped Drive v3 metadata and operator-enabled document extraction."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

import httpx

from knowledge_layer.connectors.common import ConnectorError, stable_id
from knowledge_layer.connectors.remote_documents import (
    DOCUMENT_SUFFIXES,
    DocumentLimitExceeded,
    RemoteBudget,
    extract_document,
    metadata_profile,
    operator_content_policy,
)
from knowledge_layer.connectors.secrets import read_source_secret

FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
MAX_FILES = 100
MAX_PAGES = 20
_API = "https://www.googleapis.com/drive/v3/files"
_TOKEN = "https://oauth2.googleapis.com/token"
_FOLDER = "application/vnd.google-apps.folder"
_DOC = "application/vnd.google-apps.document"
_FIELDS = "id,name,mimeType,parents,trashed,version,modifiedTime,size,md5Checksum,headRevisionId,description,driveId,capabilities(canDownload)"


def _profile(reference: str, folder: str) -> dict[str, Any]:
    raw = read_source_secret(reference, unavailable_code="drive_credentials_unavailable")
    try:
        profile = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ConnectorError("drive_profile_invalid", "The configured Drive profile must be valid JSON") from exc
    allowed = {"credentials", "folders", "retain_document_content", "max_file_bytes", "max_total_bytes"}
    if (not isinstance(profile, dict) or not set(profile) <= allowed
            or not isinstance(profile.get("credentials"), dict)
            or not isinstance(profile.get("folders"), list) or not 1 <= len(profile["folders"]) <= 100
            or any(not isinstance(item, str) or not FILE_ID.fullmatch(item) for item in profile["folders"])
            or len(set(profile["folders"])) != len(profile["folders"])):
        raise ConnectorError("drive_profile_invalid", "The Drive profile requires credentials and explicit allowed folder IDs")
    if folder not in profile["folders"]:
        raise ConnectorError("drive_scope_denied", "The selected Drive folder is not enabled by the operator")
    operator_content_policy(profile)
    return profile


def _raw(client: httpx.Client, method: str, url: str, budget: RemoteBudget, *, maximum: int | None = None, **kwargs: Any) -> tuple[int, bytes, dict[str, str]]:
    remaining = budget.timeout()
    with client.stream(method, url, timeout=httpx.Timeout(remaining, connect=min(5, remaining)), **kwargs) as response:
        if 300 <= response.status_code < 400:
            raise ConnectorError("drive_endpoint_denied", "Drive redirects are not allowed")
        chunks = []
        size = 0
        for chunk in response.iter_bytes(64 * 1024):
            size += len(chunk)
            budget.consume(len(chunk), content=maximum is not None and response.status_code == 200)
            if maximum is not None and size > maximum:
                raise DocumentLimitExceeded
            chunks.append(chunk)
        budget.timeout()
        return response.status_code, b"".join(chunks), dict(response.headers)


def _access_token(credentials: dict[str, Any], retained: bool, budget: RemoteBudget, client: httpx.Client) -> str:
    allowed = {"type", "project_id", "private_key_id", "private_key", "client_email", "client_id", "auth_uri",
               "token_uri", "auth_provider_x509_cert_url", "client_x509_cert_url", "universe_domain"}
    required = {"type", "project_id", "private_key", "client_email", "token_uri"}
    if (not required <= set(credentials) or not set(credentials) <= allowed
            or credentials["type"] != "service_account" or credentials["token_uri"] != _TOKEN
            or any(not isinstance(value, str) or not value for value in credentials.values())
            or credentials.get("universe_domain", "googleapis.com") != "googleapis.com"):
        raise ConnectorError("drive_profile_invalid", "Drive requires a public-universe service-account JSON credential")
    try:
        from google.oauth2 import service_account
    except ImportError as exc:
        raise ConnectorError("drive_auth_unavailable", "Install the drive optional extra for native Drive authentication") from exc

    def request(url: str, method: str = "GET", body: bytes | None = None,
                headers: dict[str, str] | None = None, **_kwargs: Any) -> Any:
        if url != _TOKEN or method != "POST":
            raise ConnectorError("drive_endpoint_denied", "Drive credentials attempted an unapproved token endpoint")
        status, data, response_headers = _raw(client, method, url, budget, content=body, headers=headers)
        return SimpleNamespace(status=status, data=data, headers=response_headers)

    scope = "https://www.googleapis.com/auth/drive.readonly" if retained else "https://www.googleapis.com/auth/drive.metadata.readonly"
    auth = service_account.Credentials.from_service_account_info(credentials, scopes=[scope])
    auth.refresh(request)
    if not isinstance(auth.token, str) or not auth.token or len(auth.token) > 32_768:
        raise ConnectorError("drive_auth_unavailable", "Drive authentication returned an invalid token")
    return auth.token


def _get(client: httpx.Client, path: str, token: str, budget: RemoteBudget, params: dict[str, Any], *, maximum: int | None = None) -> bytes | dict[str, Any]:
    status, data, _headers = _raw(client, "GET", _API + path, budget, maximum=maximum,
                                headers={"Authorization": "Bearer " + token}, params=params)
    if status in {401, 403}:
        raise ConnectorError("drive_access_denied", "Drive source access was denied")
    if status == 404:
        raise ConnectorError("drive_file_unavailable", "The selected Drive file or folder is unavailable")
    if status != 200:
        raise ConnectorError("drive_source_unavailable", "Drive source discovery failed")
    if maximum is not None:
        return data
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ConnectorError("drive_metadata_invalid", "Drive returned invalid metadata JSON") from exc
    if not isinstance(payload, dict):
        raise ConnectorError("drive_metadata_invalid", "Drive returned invalid metadata JSON")
    return payload


def _metadata(row: Any, *, folder: str | None = None, exact_id: str | None = None) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ConnectorError("drive_metadata_invalid", "Drive returned invalid file metadata")
    for field, maximum in (("id", 200), ("name", 1024), ("mimeType", 256), ("version", 128), ("modifiedTime", 128)):
        value = row.get(field)
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
            raise ConnectorError("drive_metadata_invalid", "Drive returned incomplete or excessive file metadata")
    if (not FILE_ID.fullmatch(row["id"]) or row.get("trashed") is not False
            or exact_id is not None and row["id"] != exact_id
            or not row["version"].isdigit()):
        raise ConnectorError("drive_scope_mismatch", "Drive returned an unexpected or trashed file")
    if folder is not None and row.get("parents") != [folder]:
        raise ConnectorError("drive_scope_mismatch", "Drive returned a file outside the selected folder")
    for key in ("description", "headRevisionId", "md5Checksum", "driveId"):
        if key in row and (not isinstance(row[key], str) or len(row[key].encode("utf-8")) > 16_384):
            raise ConnectorError("drive_metadata_invalid", "Drive returned invalid file metadata")
    if "md5Checksum" in row and not re.fullmatch(r"[a-f0-9]{32}", row["md5Checksum"]):
        raise ConnectorError("drive_metadata_invalid", "Drive returned invalid file checksum metadata")
    size = row.get("size")
    if size is not None and (not isinstance(size, str) or not size.isdigit() or len(size) > 20):
        raise ConnectorError("drive_metadata_invalid", "Drive returned invalid file size metadata")
    if "capabilities" in row and (not isinstance(row["capabilities"], dict)
            or type(row["capabilities"].get("canDownload")) is not bool):
        raise ConnectorError("drive_metadata_invalid", "Drive returned invalid download capabilities")
    return {key: row[key] for key in _FIELDS.split(",") if key in row and key != "capabilities(canDownload)"} | {
        "capabilities": row.get("capabilities", {})}


def _suffix(row: dict[str, Any]) -> tuple[str | None, str | None]:
    if row["mimeType"] == _DOC:
        return ".txt", "text/plain"
    if row["mimeType"].startswith("application/vnd.google-apps."):
        return None, None
    suffix = PurePosixPath(row["name"]).suffix.casefold()
    if row["mimeType"] == "application/pdf":
        suffix = ".pdf"
    elif row["mimeType"] in {"text/plain", "text/markdown"} and suffix not in DOCUMENT_SUFFIXES:
        suffix = ".txt" if row["mimeType"] == "text/plain" else ".md"
    return (suffix, None) if suffix in DOCUMENT_SUFFIXES else (None, None)


def _export_locators(profile: dict[str, Any], row: dict[str, Any]) -> None:
    """An exported line is a coordinate in that representation, not Docs UI."""
    profile.get("document_extraction", {})["locator_basis"] = "drive_exported_text_lines"
    signals = profile.get("document_signals", {})
    locators = list(signals.get("candidate_term_locators", {}).values())
    locators += [item.get("source_locator", {}) for key in ("candidate_descriptions", "quantified_claims")
                 for item in signals.get(key, [])]
    for locator in locators:
        if locator.get("coordinate_basis") == "source_lines":
            locator["coordinate_basis"] = "drive_exported_text_lines"
        locator.update(drive_file_id=row["id"], drive_version=row["version"], representation="text/plain_export")


def discover(config: dict[str, Any]) -> dict[str, Any]:
    # Retention and network policy never come from the connection/agent config.
    if set(config) - {"secret_ref", "folder_id", "max_files", "source_id", "name", "previous_snapshot"}:
        raise ConnectorError("invalid_drive_config", "Drive connection config contains unsupported fields")
    reference, folder = config.get("secret_ref"), config.get("folder_id")
    if not isinstance(reference, str) or not re.fullmatch(r"KNOWLEDGE_SOURCE_[A-Z0-9_]{1,64}", reference):
        raise ConnectorError("invalid_secret_reference", "A named server-side Drive reference is required")
    if not isinstance(folder, str) or not FILE_ID.fullmatch(folder):
        raise ConnectorError("drive_scope_required", "Drive discovery requires an explicit folder ID")
    maximum = config.get("max_files", MAX_FILES)
    if type(maximum) is not int or not 1 <= maximum <= MAX_FILES:
        raise ConnectorError("invalid_scan_limit", "max_files must be between 1 and 100")
    profile = _profile(reference, folder)
    retained, max_bytes, total_bytes = operator_content_policy(profile)
    budget = RemoteBudget(total_bytes)
    source_id = config.get("source_id") or stable_id("source", "google_drive", reference, folder)
    assets, errors = [], []
    try:
        with httpx.Client(follow_redirects=False, trust_env=False) as client:
            token = _access_token(profile["credentials"], retained, budget, client)
            folder_metadata = _metadata(_get(client, "/" + folder, token, budget,
                {"fields": _FIELDS, "supportsAllDrives": "true"}), exact_id=folder)
            if folder_metadata["mimeType"] != _FOLDER:
                raise ConnectorError("drive_scope_required", "The selected Drive scope is not a folder")
            page_token = None
            seen_tokens, files = set(), {}
            for _page in range(MAX_PAGES):
                params = {"q": f"'{folder}' in parents and trashed = false", "spaces": "drive",
                          "corpora": "drive" if folder_metadata.get("driveId") else "user",
                          "pageSize": min(100, maximum - len(files) + 1),
                          "fields": f"nextPageToken,incompleteSearch,files({_FIELDS})",
                          "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
                if folder_metadata.get("driveId"):
                    params["driveId"] = folder_metadata["driveId"]
                if page_token is not None:
                    params["pageToken"] = page_token
                page = _get(client, "", token, budget, params)
                if page.get("incompleteSearch") is not False or not isinstance(page.get("files"), list):
                    raise ConnectorError("drive_listing_incomplete", "Drive did not return a complete folder listing")
                if len(page["files"]) > params["pageSize"]:
                    raise ConnectorError("drive_file_limit", "The selected Drive folder exceeds max_files")
                for row in page["files"]:
                    row = _metadata(row, folder=folder)
                    if row["id"] in files:
                        raise ConnectorError("drive_metadata_invalid", "Drive returned duplicate file identities")
                    files[row["id"]] = row
                if len(files) > maximum:
                    raise ConnectorError("drive_file_limit", "The selected Drive folder exceeds max_files")
                page_token = page.get("nextPageToken")
                if page_token in (None, ""):
                    break
                if not isinstance(page_token, str) or len(page_token) > 4096 or page_token in seen_tokens:
                    raise ConnectorError("drive_listing_incomplete", "Drive returned an invalid metadata continuation")
                seen_tokens.add(page_token)
            else:
                raise ConnectorError("drive_file_limit", "The Drive listing exceeds its finite page budget")
            for file_id, row in sorted(files.items()):
                pointer = "google_drive/" + file_id
                suffix, export_mime = _suffix(row)
                size = int(row["size"]) if "size" in row else None
                doc_profile = metadata_profile(size)
                content_sha = None
                if retained and suffix is not None:
                    status = None
                    if row.get("capabilities", {}).get("canDownload") is not True:
                        status = "download_not_permitted"
                    elif export_mime is None and size is not None and size > max_bytes:
                        status = "file_size_limit_exceeded"
                    else:
                        before = _metadata(_get(client, "/" + file_id, token, budget,
                            {"fields": _FIELDS, "supportsAllDrives": "true"}), folder=folder, exact_id=file_id)
                        if before != row:
                            raise ConnectorError("remote_revision_changed", "Drive metadata changed during discovery; retry the source")
                        try:
                            data = _get(client, "/" + file_id + ("/export" if export_mime else ""), token, budget,
                                {"mimeType": export_mime} if export_mime else {"alt": "media", "supportsAllDrives": "true"}, maximum=max_bytes)
                            if export_mime is None and size is not None and len(data) != size:
                                raise ConnectorError("remote_revision_changed", "Drive content differs from its declared size; retry the source")
                            if export_mime is None and row.get("md5Checksum") and hashlib.md5(data, usedforsecurity=False).hexdigest() != row["md5Checksum"]:
                                raise ConnectorError("remote_revision_changed", "Drive content differs from its listed revision; retry the source")
                            doc_profile, gap = extract_document(row["name"], suffix, data, budget)
                            if export_mime:
                                _export_locators(doc_profile, row)
                            content_sha = hashlib.sha256(data).hexdigest()
                            if gap:
                                errors.append({**gap, "source_pointer": pointer})
                        except DocumentLimitExceeded:
                            status = "file_size_limit_exceeded"
                        except ConnectorError as exc:
                            if exc.code not in {"drive_access_denied", "drive_file_unavailable"}:
                                raise
                            status = exc.code
                        after = _metadata(_get(client, "/" + file_id, token, budget,
                            {"fields": _FIELDS, "supportsAllDrives": "true"}), folder=folder, exact_id=file_id)
                        if after != before:
                            raise ConnectorError("remote_revision_changed", "Drive metadata changed during content extraction; retry the source")
                    if status:
                        doc_profile = metadata_profile(size, status, enabled=True)
                        errors.append({"code": status, "message": "Drive document content was not extracted; metadata was retained", "source_pointer": pointer})
                elif retained:
                    doc_profile = metadata_profile(size, "unsupported_document_type", enabled=True)
                observations = {"native_metadata": row, "folder_id": folder, "content_sha256": content_sha,
                                "export_mime_type": export_mime, "source_instructions_executed": False}
                assets.append({"id": stable_id("asset", source_id, pointer), "name": row["name"],
                    "asset_type": "folder" if row["mimeType"] == _FOLDER else "document" if suffix else "file",
                    "source_pointer": pointer, "revision": stable_id("drive_revision", row, content_sha),
                    "revision_basis": "drive_version_metadata_and_content_sha256" if content_sha else "drive_version_metadata",
                    "schema": [], "profile": doc_profile, "observations": observations, "trust_state": "unverified"})
            result = {"source": {"id": source_id, "name": config.get("name") or "Google Drive folder",
                "kind": "google_drive", "location": "google_drive/folders/" + folder,
                "revision": stable_id("revision", *[(item["source_pointer"], item["revision"]) for item in assets]),
                "trust_state": "unverified"}, "assets": assets, "errors": errors,
                "capabilities": {"metadata": True, "documents": True, "safe_profiling": retained,
                                 "change_signals": True, "query_execution": False, "permission_mirroring": False},
                "coverage": {"scope": folder, "complete_selected_scope": True, "recursive": False,
                             "visibility_basis": "direct_children_visible_to_source_credential",
                             "retention_enabled_by_operator": retained}}
            budget.consume_json(result)
            return result
    except ConnectorError:
        raise
    except Exception as exc:
        raise ConnectorError("drive_source_unavailable", "Drive source discovery failed") from exc
