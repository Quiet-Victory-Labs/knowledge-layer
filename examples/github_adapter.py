"""Read-only, exact-file GitHub adapter for the external source protocol.

The operator pins a commit and an allowlist of files per scope. The adapter
reads those blobs through the GitHub Contents API; it cannot enumerate a repo,
follow links, execute code, or write to GitHub. Run behind an operator-controlled
network boundary and grant snapshot access only to readers of those files.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from reference_adapter import BoundedHTTPServer

OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
SCOPE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,62}\Z")
COMMIT = re.compile(r"[a-f0-9]{40}\Z")
CODE_SYMBOL = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
MAX_FILE_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_FILES_PER_SCOPE = 24
MAX_TERMS = 50


class AdapterError(Exception):
    """A stable public failure code; never include upstream text."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def load_config(path: Path) -> dict[str, dict]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError("invalid_config_file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError("invalid_config_file") from exc
    scopes = raw.get("scopes") if isinstance(raw, dict) else None
    if not isinstance(scopes, dict) or not 1 <= len(scopes) <= 20:
        raise ValueError("invalid_scopes")
    for scope, profile in scopes.items():
        if not isinstance(scope, str) or not SCOPE.fullmatch(scope) or not isinstance(profile, dict):
            raise ValueError("invalid_scope")
        if set(profile) != {"owner", "repo", "commit", "paths"}:
            raise ValueError("invalid_scope_profile")
        if (not isinstance(profile["owner"], str) or not OWNER.fullmatch(profile["owner"])
                or not isinstance(profile["repo"], str) or not REPOSITORY.fullmatch(profile["repo"])
                or profile["repo"] in {".", ".."}
                or not isinstance(profile["commit"], str) or not COMMIT.fullmatch(profile["commit"])):
            raise ValueError("invalid_repo_revision")
        paths = profile["paths"]
        if (not isinstance(paths, list) or not 1 <= len(paths) <= MAX_FILES_PER_SCOPE
                or any(not isinstance(name, str) for name in paths)
                or len(set(paths)) != len(paths)):
            raise ValueError("invalid_path_allowlist")
        for name in paths:
            if not isinstance(name, str) or not 1 <= len(name) <= 240 or "\\" in name:
                raise ValueError("invalid_path_allowlist")
            parsed = PurePosixPath(name)
            if (parsed.is_absolute() or any(part in {".", "..", ""} for part in name.split("/"))
                    or parsed.suffix.lower() not in {".md", ".txt", ".py"}):
                raise ValueError("invalid_path_allowlist")
    return scopes


def github_blob(profile: dict, path: str, token: str) -> tuple[bytes, str]:
    """Fetch one allowlisted file at the configured commit and verify Git's blob ID."""
    url = ("https://api.github.com/repos/" + quote(profile["owner"], safe="") + "/"
           + quote(profile["repo"], safe="") + "/contents/" + quote(path, safe="/")
           + "?ref=" + profile["commit"])
    request = Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "knowledge-layer-github-adapter",
    })
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(MAX_FILE_BYTES * 2 + 1)
    except (HTTPError, URLError, OSError) as exc:
        raise AdapterError("github_source_unavailable") from exc
    if len(raw) > MAX_FILE_BYTES * 2:
        raise AdapterError("github_file_budget_exceeded")
    try:
        payload = json.loads(raw)
        if (not isinstance(payload, dict) or payload.get("type") != "file"
                or payload.get("path") != path or payload.get("encoding") != "base64"
                or not isinstance(payload.get("content"), str)
                or not isinstance(payload.get("sha"), str)):
            raise ValueError("invalid_blob")
        # GitHub's JSON Contents response wraps base64 with literal newlines.
        content = base64.b64decode("".join(payload["content"].split()), validate=True)
        git_sha = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if (len(content) > MAX_FILE_BYTES or payload.get("size") != len(content)
                or not hmac.compare_digest(payload["sha"], git_sha)):
            raise ValueError("blob_mismatch")
    except (ValueError, TypeError, binascii.Error) as exc:
        raise AdapterError("github_blob_invalid") from exc
    return content, git_sha


def document_terms(path: str, content: bytes, *, include_excerpts: bool) -> tuple[list[dict], dict]:
    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdapterError("github_document_not_utf8") from exc
    lines = source.splitlines(keepends=True)
    if len(lines) > 4000:
        raise AdapterError("github_document_line_budget_exceeded")
    markers: list[tuple[int, str]] = [(0, PurePosixPath(path).name)]
    offset = 0
    for raw in lines:
        markdown = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", raw)
        symbol = CODE_SYMBOL.match(raw) if path.endswith(".py") else None
        if markdown:
            markers.append((offset, markdown.group(1).strip()[:96]))
        elif symbol:
            markers.append((offset, symbol.group(1)[:96]))
        offset += len(raw)
    terms: list[dict] = []
    if not include_excerpts:
        terms = [{"term": name} for _, name in markers[:MAX_TERMS]]
    else:
        position = 0
        while position < len(source) and len(terms) < MAX_TERMS:
            maximum = min(position + 280, len(source))
            following = next((at for at, _ in markers if position + 4 <= at < maximum), None)
            if following is not None:
                end = following
            else:
                window = source[position:maximum]
                line_break = window.rfind("\n", 140)
                space = window.rfind(" ", 140)
                end = position + (line_break + 1 if line_break >= 0 else
                                  space + 1 if space >= 0 else len(window))
            if 0 < len(source) - end < 4:
                end = len(source) if len(source) - position <= 280 else position + 276
            excerpt = source[position:end]
            if len(excerpt.strip()) < 4:
                break
            heading = next(name for at, name in reversed(markers) if at <= position)
            start_line = source.count("\n", 0, position) + 1
            end_line = source.count("\n", 0, max(position, end - 1)) + 1
            start_column = position - source.rfind("\n", 0, position)
            end_column = end - 1 - source.rfind("\n", 0, end - 1)
            terms.append({"term": heading, "excerpt": excerpt, "line": start_line,
                "source_locator": {
                    "format": "txt" if path.endswith(".py") else "md" if path.endswith(".md") else "txt",
                    "coordinate_basis": "source_lines", "line_start": start_line,
                    "line_end": end_line, "column_start": start_column, "column_end": end_column,
                }})
            position = end
    full_retention = include_excerpts and "".join(item["excerpt"] for item in terms) == source
    coverage = {
        "extraction_complete": True, "extraction_truncated": False,
        "retention_complete": include_excerpts and full_retention,
        "retention_truncated": include_excerpts and not full_retention,
    }
    return terms, coverage


def discover(profile: dict, *, max_assets: int, max_columns: int,
             include_document_excerpts: bool, token: str) -> dict:
    if len(profile["paths"]) > max_assets:
        raise AdapterError("scope_exceeds_asset_budget")
    assets = []
    for path in profile["paths"]:
        content, blob_sha = github_blob(profile, path, token)
        terms, coverage = document_terms(path, content, include_excerpts=include_document_excerpts)
        assets.append({
            "name": PurePosixPath(path).name, "pointer": path, "asset_type": "document",
            "revision": blob_sha, "schema": [], "document_terms": terms,
            "document_coverage": coverage,
        })
    response = {"protocol_version": "1.2", "listing_complete": True, "assets": assets}
    if len(json.dumps(response, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
        raise AdapterError("scope_exceeds_response_budget")
    return response


class Handler(BaseHTTPRequestHandler):
    scopes: dict[str, dict]

    def do_POST(self) -> None:
        token = os.environ.get("ADAPTER_BEARER_TOKEN", "")
        source_token = os.environ.get("GITHUB_ADAPTER_TOKEN", "")
        if not token or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
            self._reply(401, {"error": "unauthorized"})
            return
        if self.path != "/discover":
            self._reply(404, {"error": "not_found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 4096:
                raise AdapterError("invalid_request")
            body = json.loads(self.rfile.read(size))
            if (not isinstance(body, dict) or body.get("protocol_version") != "1.2"
                    or type(body.get("max_assets")) is not int or not 1 <= body["max_assets"] <= 100
                    or type(body.get("max_columns")) is not int or not 1 <= body["max_columns"] <= 5000
                    or type(body.get("include_document_excerpts")) is not bool):
                raise AdapterError("invalid_request")
            scope = body.get("scope")
            if not isinstance(scope, str) or scope not in self.scopes:
                raise AdapterError("scope_denied")
            if not source_token:
                raise AdapterError("github_token_unavailable")
            response = discover(self.scopes[scope], max_assets=body["max_assets"],
                                max_columns=body["max_columns"],
                                include_document_excerpts=body["include_document_excerpts"],
                                token=source_token)
        except (AdapterError, ValueError, TypeError) as exc:
            self._reply(422, {"error": str(exc) if isinstance(exc, AdapterError) else "invalid_request"})
            return
        except Exception:  # noqa: BLE001 -- Never relay GitHub response or token details.
            self._reply(503, {"error": "github_source_unavailable"})
            return
        self._reply(200, response)

    def _reply(self, status: int, body: dict) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=4182)
    args = parser.parse_args()
    Handler.scopes = load_config(args.config)
    if not os.environ.get("ADAPTER_BEARER_TOKEN") or not os.environ.get("GITHUB_ADAPTER_TOKEN"):
        parser.error("ADAPTER_BEARER_TOKEN and GITHUB_ADAPTER_TOKEN must be set")
    BoundedHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
