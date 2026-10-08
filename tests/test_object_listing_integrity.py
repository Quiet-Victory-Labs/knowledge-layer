"""Provider success cannot establish an out-of-scope or unverifiable inventory."""
import json

import pytest

from knowledge_layer.connectors.common import ConnectorError


@pytest.mark.parametrize("response", [
    {"Contents": [{"Key": "private/salary.csv"}], "IsTruncated": False},
    {"Contents": [{"Key": "finance/a.csv"}, {"Key": "finance/a.csv"}], "IsTruncated": False},
    {}, {"Contents": []}, {"Contents": [], "IsTruncated": "false"},
    {"IsTruncated": False}, {"Contents": [], "IsTruncated": False, "KeyCount": 1},
])
def test_invalid_or_out_of_scope_listing_refuses(monkeypatch, response):
    object_store = pytest.importorskip("knowledge_layer.connectors.object_store")
    monkeypatch.setenv("KNOWLEDGE_SOURCE_LISTING", json.dumps({"bucket": "approved-data"}))
    class Client:
        def list_objects_v2(self, **_kwargs):
            return response
        def close(self):
            pass
    monkeypatch.setattr(object_store.boto3, "client", lambda *_args, **_kwargs: Client())
    with pytest.raises(ConnectorError) as failure:
        object_store.discover({"secret_ref": "KNOWLEDGE_SOURCE_LISTING", "prefix": "finance/"})  # pragma: allowlist secret - public revision/operator reference
    assert failure.value.code == "object_listing_invalid"


def test_explicit_empty_native_listing_remains_complete(monkeypatch):
    object_store = pytest.importorskip("knowledge_layer.connectors.object_store")
    monkeypatch.setenv("KNOWLEDGE_SOURCE_LISTING", json.dumps({"bucket": "approved-data"}))
    class Client:
        def list_objects_v2(self, **_kwargs):
            return {"KeyCount": 0, "IsTruncated": False}
        def close(self):
            pass
    monkeypatch.setattr(object_store.boto3, "client", lambda *_args, **_kwargs: Client())
    assert object_store.discover({"secret_ref": "KNOWLEDGE_SOURCE_LISTING"})["assets"] == []  # pragma: allowlist secret - operator reference
