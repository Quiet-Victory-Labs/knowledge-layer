"""A pinned GitHub adapter must fail closed on scope, blob and excerpt provenance."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "examples"))
from github_adapter import (
    AdapterError,
    discover,
    document_terms,
    github_blob,
    load_config,
)

from knowledge_layer.connectors.external_adapter import normalize_response


def _profile(paths=None):
    return {"owner": "example-org", "repo": "example-catalog",
            "commit": "a" * 40, "paths": paths or ["docs/ARCHITECTURE.md"]}


@pytest.mark.parametrize("bad_path", ["../secret.md", "/absolute.md", "docs/../secret.md",
                                      "docs//readme.md", "code.sh"])
def test_exact_scope_refuses_path_escape_and_unsupported_files(tmp_path, bad_path):
    path = tmp_path / "scopes.json"
    path.write_text(json.dumps({"scopes": {"repo": _profile([bad_path])}}))
    with pytest.raises(ValueError, match="invalid_path_allowlist"):
        load_config(path)


def test_mutable_ref_and_nested_json_are_not_valid_scopes(tmp_path):
    path = tmp_path / "scopes.json"
    for change in ({"commit": "main"}, {"paths": [["README.md"]]}, {"owner": ".."}):
        path.write_text(json.dumps({"scopes": {"repo": _profile() | change}}))
        with pytest.raises(ValueError):
            load_config(path)


def test_github_contents_blob_is_verified_before_import(monkeypatch):
    content = b"# Registered experiment\nEvidence must be pinned.\n"
    sha = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    encoded = base64.b64encode(content).decode()
    payload = {"type": "file", "path": "docs/ARCHITECTURE.md", "encoding": "base64",
               "content": encoded[:12] + "\n" + encoded[12:], "size": len(content), "sha": sha}
    urls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _limit):
            return json.dumps(payload).encode()

    class Opener:
        def open(self, request, timeout):
            urls.append(request.full_url)
            return Response()

    monkeypatch.setattr("github_adapter.build_opener", lambda *_: Opener())
    data, observed = github_blob(_profile(), "docs/ARCHITECTURE.md", "test-token")
    assert data == content and observed == sha
    assert urls == ["https://api.github.com/repos/example-org/example-catalog/contents/docs/ARCHITECTURE.md?ref=" + "a" * 40]
    payload["sha"] = "0" * 40
    with pytest.raises(AdapterError, match="github_blob_invalid"):
        github_blob(_profile(), "docs/ARCHITECTURE.md", "test-token")


def test_document_passage_coordinates_and_coverage_are_truthful(monkeypatch):
    content = b"# Release gates\nThe platform checks independent gates.\n"
    monkeypatch.setattr("github_adapter.github_blob", lambda *_args: (content, "b" * 40))
    response = discover(_profile(), max_assets=1, max_columns=1,
                        include_document_excerpts=True, token="test-token")
    document = response["assets"][0]
    assert document["document_coverage"]["retention_complete"] is True
    assert "".join(item["excerpt"] for item in document["document_terms"]) == content.decode()
    assert document["document_terms"][0]["source_locator"]["line_end"] == 2
    normalized = normalize_response(json.dumps(response).encode(), provider="github", scope="repo",
                                    protocol_version="1.2", max_assets=1, max_columns=1,
                                    retain_semantic_samples=True)
    assert normalized["assets"][0]["source_pointer"] == "adapter/github/repo/docs/ARCHITECTURE.md"
    assert normalized["assets"][0]["profile"]["content_retained"] is True
    terms, coverage = document_terms("README.md", b"# Title\n-\nUseful text\n",
                                     include_excerpts=True)
    assert "".join(item["excerpt"] for item in terms) == "# Title\n-\nUseful text\n"
    assert coverage["retention_complete"] is True
    assert document_terms("README.md", b"X" * 15000, include_excerpts=True)[1]["retention_truncated"] is True
