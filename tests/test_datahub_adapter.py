import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from knowledge_layer.adapters import datahub
from knowledge_layer.adapters.datahub import (
    INVENTORY_QUERY,
    QUERY,
    discover_glossary,
    inventory_records,
)
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.inventory import _encoded, inventory_digest

URN = "urn:li:glossaryTerm:Revenue"


def _discover(payload, status=200):
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["query"] == QUERY and "mutation" not in body["query"]
        assert body["variables"] == {"urn": URN}
        return httpx.Response(status, json=payload)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = discover_glossary(endpoint="https://catalog.example/api/graphql", bearer_token="fixture-token",
                                   namespace="finance", urns=[URN], client=client)
    assert len(calls) == 1
    return result


def test_native_term_identity_ownership_and_description_survive():
    term = {"urn": URN, "properties": {"name": "Revenue", "description": "Recognized revenue",
            "termSource": "INTERNAL", "sourceRef": None},
            "ownership": {"owners": [{"owner": {"urn": "urn:li:corpuser:finance-owner"}, "type": "BUSINESS_OWNER"}]}}
    result = _discover({"data": {"glossaryTerm": term}})
    upstream = result["assets"][0]["upstream_definition"]
    assert upstream["id"] == URN and upstream["version"] is None
    assert json.loads(upstream["formula"]) == term
    assert upstream["review_status"] is None
    assert result["native_permission_propagation"] == "not_qualified"


@pytest.mark.parametrize("payload,status", [({"data": {"glossaryTerm": None}}, 200),
    ({"data": {"glossaryTerm": {}}, "errors": [{"message": "denied"}]}, 200), ({}, 403), ({}, 302)])
def test_partial_missing_denied_redirect_scopes_refuse(payload, status):
    with pytest.raises(ConnectorError):
        _discover(payload, status)


def test_operator_scope_never_accepts_a_write_or_arbitrary_query():
    with pytest.raises(ConnectorError):
        discover_glossary(endpoint="https://catalog.example/api/graphql", bearer_token="fixture-token",
                          namespace="finance", urns=["mutation { deleteDataset }"])


ENDPOINT = "https://catalog.example/api/graphql"


def _term(urn=URN):
    # These are fields selected by the actual fixed native query, including nulls.
    return {"urn": urn, "properties": {
        "name": "Revenue", "description": "Recognized revenue", "termSource": "INTERNAL",
        "sourceRef": None, "sourceUrl": None, "rawSchema": None, "customProperties": None,
        "createdOn": None,
    }, "ownership": {"owners": [{
        "owner": {"urn": "urn:li:corpuser:finance-owner"}, "type": "BUSINESS_OWNER",
        "ownershipType": None, "source": None, "associatedUrn": urn,
    }], "lastModified": {"time": 1750000000000, "actor": None}}}


class _Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, before_chunk=None):
        self.chunks = chunks
        self.before_chunk = before_chunk
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for index, chunk in enumerate(self.chunks):
            if self.before_chunk:
                self.before_chunk(index)
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


def _raw(term):
    return _encoded({"data": {"glossaryTerm": term}})


def _inventory(*, terms=None, namespace="finance", urns=None, raw=None, status=200,
               headers=None, before_chunk=None, deadline=None):
    terms = terms or {URN: _term()}
    urns = list(terms) if urns is None else urns
    calls, streams = [], []

    def respond(request):
        body = json.loads(request.content)
        calls.append(request)
        assert request.method == "POST" and str(request.url) == ENDPOINT
        assert body["query"] == INVENTORY_QUERY and "mutation" not in body["query"]
        assert set(body) == {"query", "variables"}
        assert set(body["variables"]) == {"urn"}
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["authorization"] == "Bearer fixture-token"
        content = _raw(terms[body["variables"]["urn"]]) if raw is None else raw
        stream = _Chunks([content], before_chunk)
        streams.append(stream)
        return httpx.Response(status, stream=stream, headers=headers)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = inventory_records(endpoint=ENDPOINT, bearer_token="fixture-token",
                                   namespace=namespace, urns=urns, client=client, deadline=deadline)
        assert not client.is_closed
    assert all(stream.closed for stream in streams)
    return result, calls


def test_inventory_preserves_large_full_native_record_and_unknown_authority():
    term = _term()
    term["properties"].update({
        "description": "Revenue 🚀 " * 500,
        "rawSchema": "native schema " * 500,
        "sourceUrl": "https://source.example/definition",
        "customProperties": [{"key": "currency", "value": "USD", "associatedUrn": URN}],
        "createdOn": {"time": 1750000000000, "actor": {"urn": "urn:li:corpuser:author"}},
        "nativeExtra": {"keep": "exact 🚀"},
    })
    term["ownership"]["owners"][0].update({
        "ownershipType": {"urn": "urn:li:ownershipType:businessOwner"},
        "source": {"type": "SERVICE", "url": "https://source.example/owners"},
        "nativeExtra": {"role": "native-owner"},
    })
    before = datetime.now(UTC)
    result, calls = _inventory(terms={URN: term})
    after = datetime.now(UTC)
    entity, proof = result["entities"][0], result["evidence"][0]
    assert len(_encoded(term)) > 4000
    assert proof["payload"]["native_record"] == term
    assert entity["payload"]["definition"] == term["properties"]["description"]
    assert entity["kind"] == "semantic_definition"
    assert proof["kind"] == "upstream_semantic_definition"
    assert entity["evidence_ids"] == [proof["id"]]
    for record in (entity, proof):
        payload = record["payload"]
        assert payload["native_id"] == URN and payload["namespace"] == "finance"
        assert payload["authority"] == "datahub" and payload["native_kind"] == "glossary_term"
        assert payload["revision"] == inventory_digest(term)
        assert payload["native_version"] is None and payload["locally_executable"] is False
        assert before <= datetime.fromisoformat(payload["source_observed_at"]) <= after
        assert not {"local_grants", "approved", "agent_ready", "meaning_certified"} & set(payload)
        assert len(_encoded(record)) <= 64 * 1024
    assert set(result) == {"entities", "evidence", "watermark"}
    assert result["watermark"]["kind"] == "selected_terms_content_digest"
    assert len(result["watermark"]["value"]) == 64 and len(calls) == 1


def test_inventory_namespace_identity_content_revision_and_stable_watermark():
    first, _ = _inventory()
    again, _ = _inventory()
    other, _ = _inventory(namespace="another-business-unit")
    changed = _term()
    changed["properties"]["description"] = "Restated revenue"
    updated, _ = _inventory(terms={URN: changed})
    assert first["watermark"] == again["watermark"]
    assert first["entities"][0]["id"] == again["entities"][0]["id"] == updated["entities"][0]["id"]
    assert first["entities"][0]["id"] != other["entities"][0]["id"]
    assert first["evidence"][0]["id"] != other["evidence"][0]["id"]
    assert first["watermark"] != other["watermark"] != updated["watermark"]
    assert first["entities"][0]["payload"]["revision"] != updated["entities"][0]["payload"]["revision"]


def test_inventory_accepts_thousand_explicit_terms_and_canonical_scope_order():
    urns = [f"urn:li:glossaryTerm:term-{index:04d}" for index in range(1000)]
    terms = {urn: _term(urn) for urn in urns}
    result, calls = _inventory(terms=terms, urns=list(reversed(urns)))
    assert len(result["entities"]) == len(result["evidence"]) == len(calls) == 1000
    assert [record["payload"]["native_id"] for record in result["entities"]] == urns
    assert len({record["id"] for record in result["entities"]}) == 1000
    assert len(_encoded(result)) <= 16 * 1024 * 1024


@pytest.mark.parametrize("overrides", [
    {"urns": []}, {"urns": [URN, URN]}, {"urns": ["mutation { deleteDataset }"]},
    {"urns": [f"urn:li:glossaryTerm:{index}" for index in range(1001)]},
    {"urns": ["urn:li:glossaryTerm:"]}, {"urns": ["urn:li:glossaryTerm:x\n"]},
    {"urns": ["urn:li:glossaryTerm:\ud800"]}, {"urns": [None]},
    {"namespace": ""}, {"namespace": "x\n"}, {"namespace": "\ud800"},
    {"bearer_token": "secret\x00value"}, {"bearer_token": "secret\nvalue"},
    {"bearer_token": " "},
    {"endpoint": "http://catalog.example/api/graphql"},
    {"endpoint": "https://user:secret@catalog.example/api/graphql"},  # pragma: allowlist secret - invalid public URL fixture
    {"endpoint": "https://catalog.example/api/graphql?query=mutation"},
    {"endpoint": "https://catalog.example:not-a-port/api/graphql"},
    {"endpoint": "\nhttps://catalog.example/api/graphql"},
    {"deadline": datetime(2026, 10, 3, tzinfo=None)},  # noqa: DTZ001 - refusal of naive clock
])
def test_inventory_invalid_operator_inputs_refuse_before_http(overrides):
    calls = []
    settings = {"endpoint": ENDPOINT, "bearer_token": "fixture-token", "namespace": "finance", "urns": [URN]}
    settings.update(overrides)
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
        with pytest.raises(ConnectorError):
            inventory_records(**settings, client=client)
        assert not client.is_closed
    assert calls == []


@pytest.mark.parametrize("raw", [
    b'{"data":{"glossaryTerm":null},"data":{"glossaryTerm":null}}',
    b'{"data":{"glossaryTerm":{"extra":{"x":1,"x":2}}}}',
    b'{"data":{"glossaryTerm":null},"extra":NaN}',
    b'{"data":{"glossaryTerm":null},"extra":1e999}',
    b'{"data":{"glossaryTerm":null},"extra":{"\\ud800":"x"}}',
    b'{"data":{"glossaryTerm":null},"extra":{"x":"\\udfff"}}',
    b'{"data":{"glossaryTerm":null},"extra":"\xff"}',
    b'{"data":',
])
def test_inventory_strict_native_json_refuses(raw):
    with pytest.raises(ConnectorError) as raised:
        _inventory(raw=raw)
    assert raised.value.code == "datahub_response_invalid"


def test_inventory_accepts_valid_escaped_non_bmp_exactly():
    term = _term()
    term["properties"]["description"] = "Revenue 🚀"
    raw = json.dumps({"data": {"glossaryTerm": term}}, ensure_ascii=True).encode()
    result, _ = _inventory(raw=raw)
    assert result["evidence"][0]["payload"]["native_record"] == term


@pytest.mark.parametrize("description", [None, ""])
def test_inventory_preserves_native_missing_description_without_fallback(description):
    term = _term()
    term["properties"]["description"] = description
    term["ownership"] = None
    result, _ = _inventory(terms={URN: term})
    assert result["entities"][0]["payload"]["definition"] is description
    assert result["evidence"][0]["payload"]["native_record"]["ownership"] is None


@pytest.mark.parametrize("field", ["name", "description", "termSource", "sourceRef", "sourceUrl", "rawSchema",
                                  "customProperties", "createdOn"])
def test_inventory_missing_selected_properties_refuse(field):
    term = _term()
    del term["properties"][field]
    with pytest.raises(ConnectorError) as raised:
        _inventory(terms={URN: term})
    assert raised.value.code == "datahub_partial"


@pytest.mark.parametrize("field", ["owner", "type", "ownershipType", "source", "associatedUrn"])
def test_inventory_partial_owner_fields_refuse(field):
    term = _term()
    del term["ownership"]["owners"][0][field]
    with pytest.raises(ConnectorError) as raised:
        _inventory(terms={URN: term})
    assert raised.value.code == "datahub_partial"


@pytest.mark.parametrize("term", [None, {}, {"urn": "urn:li:glossaryTerm:other", "properties": {}},
                                  {"urn": URN, "properties": None}])
def test_inventory_missing_or_wrong_term_never_establishes_removal(term):
    with pytest.raises(ConnectorError) as raised:
        _inventory(raw=_raw(term))
    assert raised.value.code == "datahub_scope_incomplete"
    assert "removal is not established" in raised.value.public_message


@pytest.mark.parametrize("status", [302, 401, 403, 404, 410, 429, 500])
def test_inventory_denial_and_redirect_do_not_follow_or_read_body(status):
    calls = []
    stream = _Chunks([b"secret upstream diagnostics"])

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(status, stream=stream, headers={"location": "https://other.example/write"})

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        with pytest.raises(ConnectorError) as raised:
            inventory_records(endpoint=ENDPOINT, bearer_token="fixture-token", namespace="finance", urns=[URN], client=client)
        assert not client.is_closed
    assert calls == [ENDPOINT] and stream.reads == 0 and stream.closed
    assert raised.value.code == "datahub_unavailable"
    assert "secret" not in str(raised.value) and raised.value.__cause__ is None


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity,gzip"])
def test_inventory_rejects_compression_before_consuming_body(encoding):
    stream = _Chunks([_raw(_term())])
    with (httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream, headers={"content-encoding": encoding}))) as client,
          pytest.raises(ConnectorError) as raised):
        inventory_records(endpoint=ENDPOINT, bearer_token="fixture-token", namespace="finance", urns=[URN], client=client)
    assert raised.value.code == "datahub_encoding_refused"
    assert stream.reads == 0 and stream.closed


def test_inventory_raw_response_exact_byte_cap_and_multibyte_overrun():
    raw = _raw(_term())
    padded = raw + b" " * (64 * 1024 - len(raw))
    result, _ = _inventory(raw=padded)
    assert result["entities"]
    with pytest.raises(ConnectorError) as raised:
        _inventory(raw=padded + b" ")
    assert raised.value.code == "datahub_budget_exceeded"
    term = _term()
    term["properties"]["rawSchema"] = "🚀" * 16400
    assert len(_raw(term)) > 64 * 1024
    with pytest.raises(ConnectorError) as raised:
        _inventory(terms={URN: term})
    assert raised.value.code == "datahub_budget_exceeded"


def test_inventory_wrapper_size_guard_refuses_a_native_record_below_wire_cap():
    term = _term()
    term["properties"]["rawSchema"] = ""
    term["properties"]["rawSchema"] = "x" * (64 * 1024 - len(_raw(term)) - 1)
    assert len(_raw(term)) == 64 * 1024 - 1
    with pytest.raises(ConnectorError) as raised:
        _inventory(terms={URN: term})
    assert raised.value.code == "datahub_budget_exceeded"


def test_inventory_aggregate_raw_budget_includes_discarded_wire_whitespace(monkeypatch):
    raw = _raw(_term()) + b" " * 2000
    monkeypatch.setattr(datahub, "MAX_INVENTORY_BYTES", len(raw) + 10)
    other = "urn:li:glossaryTerm:second"
    calls = []

    def respond(request):
        urn = json.loads(request.content)["variables"]["urn"]
        calls.append(urn)
        return httpx.Response(200, stream=_Chunks([_raw(_term(urn)) + b" " * 2000]))

    with (httpx.Client(transport=httpx.MockTransport(respond)) as client,
          pytest.raises(ConnectorError) as raised):
        inventory_records(endpoint=ENDPOINT, bearer_token="fixture-token", namespace="finance", urns=[URN, other], client=client)
    assert raised.value.code == "datahub_budget_exceeded" and len(calls) == 2


def test_inventory_complete_capture_budget_includes_wrapper_and_watermark(monkeypatch):
    result, _ = _inventory()
    record_bytes = sum(len(_encoded(record)) for record in result["entities"] + result["evidence"])
    assert record_bytes < len(_encoded(result))
    monkeypatch.setattr(datahub, "MAX_INVENTORY_BYTES", record_bytes)
    with pytest.raises(ConnectorError) as raised:
        _inventory()
    assert raised.value.code == "datahub_budget_exceeded"
    assert "complete capture" in raised.value.public_message


def test_inventory_final_term_failure_never_returns_a_partial_scope():
    other = "urn:li:glossaryTerm:second"
    calls = []

    def respond(request):
        urn = json.loads(request.content)["variables"]["urn"]
        calls.append(urn)
        payload = {"data": {"glossaryTerm": _term(urn)}}
        if urn == other:
            payload["errors"] = [{"message": "private native permission failure"}]
        return httpx.Response(200, stream=_Chunks([_encoded(payload)]))

    with (httpx.Client(transport=httpx.MockTransport(respond)) as client,
          pytest.raises(ConnectorError) as raised):
        inventory_records(endpoint=ENDPOINT, bearer_token="fixture-token", namespace="finance", urns=[URN, other], client=client)
    assert calls == [URN, other] and raised.value.code == "datahub_partial"
    assert "private" not in str(raised.value)


def _clock(monkeypatch):
    instant = [datetime(2026, 10, 3, 10, tzinfo=UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(instant[0].timestamp(), tz=tz)

    monkeypatch.setattr(datahub, "datetime", Clock)
    return instant, Clock


def test_inventory_expired_deadline_refuses_before_http(monkeypatch):
    instant, clock = _clock(monkeypatch)
    calls = []
    deadline = clock.fromtimestamp((instant[0] - timedelta(seconds=1)).timestamp(), UTC)
    with (httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request))) as client,
          pytest.raises(ConnectorError) as raised):
        inventory_records(endpoint=ENDPOINT, bearer_token="fixture-token", namespace="finance", urns=[URN], client=client, deadline=deadline)
    assert raised.value.code == "datahub_inventory_expired" and calls == []


def test_inventory_http_timeout_is_bounded_by_remaining_capture_deadline(monkeypatch):
    instant, clock = _clock(monkeypatch)
    deadline = clock.fromtimestamp((instant[0] + timedelta(seconds=2)).timestamp(), UTC)
    _, calls = _inventory(deadline=deadline)
    assert calls[0].extensions["timeout"] == {"connect": 2.0, "read": 2.0, "write": 2.0, "pool": 2.0}


def test_inventory_expiry_during_second_raw_chunk_refuses_and_closes(monkeypatch):
    instant, clock = _clock(monkeypatch)
    deadline = clock.fromtimestamp((instant[0] + timedelta(seconds=2)).timestamp(), UTC)
    raw = _raw(_term())

    def advance(index):
        if index == 1:
            instant[0] += timedelta(seconds=3)

    stream = _Chunks([raw[:10], raw[10:]], advance)
    with (httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))) as client,
          pytest.raises(ConnectorError) as raised):
        inventory_records(endpoint=ENDPOINT, bearer_token="fixture-token", namespace="finance", urns=[URN], client=client, deadline=deadline)
    assert raised.value.code == "datahub_inventory_expired" and stream.closed


def test_inventory_deadline_rechecks_after_assembly_before_return(monkeypatch):
    instant, clock = _clock(monkeypatch)
    deadline = clock.fromtimestamp((instant[0] + timedelta(seconds=2)).timestamp(), UTC)
    original = datahub._records

    def advance_after_record(value, **kwargs):
        result = original(value, **kwargs)
        if kwargs.get("evidence"):
            instant[0] += timedelta(seconds=3)
        return result

    monkeypatch.setattr(datahub, "_records", advance_after_record)
    with pytest.raises(ConnectorError) as raised:
        _inventory(deadline=deadline)
    assert raised.value.code == "datahub_inventory_expired"


def test_inventory_deadline_rechecks_after_final_serialization(monkeypatch):
    instant, clock = _clock(monkeypatch)
    deadline = clock.fromtimestamp((instant[0] + timedelta(seconds=2)).timestamp(), UTC)
    original = datahub._encoded

    def expire_during_final_serialization(value):
        result = original(value)
        if isinstance(value, dict) and set(value) == {"entities", "evidence", "watermark"}:
            instant[0] += timedelta(seconds=3)
        return result

    monkeypatch.setattr(datahub, "_encoded", expire_during_final_serialization)
    with pytest.raises(ConnectorError) as raised:
        _inventory(deadline=deadline)
    assert raised.value.code == "datahub_inventory_expired"


def test_inventory_deadline_rechecks_before_next_term_io(monkeypatch):
    instant, clock = _clock(monkeypatch)
    deadline = clock.fromtimestamp((instant[0] + timedelta(seconds=2)).timestamp(), UTC)
    original = datahub._records
    calls = []

    def expire_after_record(value, **kwargs):
        result = original(value, **kwargs)
        if kwargs.get("evidence"):
            instant[0] += timedelta(seconds=3)
        return result

    def respond(request):
        urn = json.loads(request.content)["variables"]["urn"]
        calls.append(urn)
        return httpx.Response(200, stream=_Chunks([_raw(_term(urn))]))

    monkeypatch.setattr(datahub, "_records", expire_after_record)
    with (httpx.Client(transport=httpx.MockTransport(respond)) as client,
          pytest.raises(ConnectorError) as raised):
        inventory_records(endpoint=ENDPOINT, bearer_token="fixture-token", namespace="finance", urns=[URN, "urn:li:glossaryTerm:second"], client=client, deadline=deadline)
    assert raised.value.code == "datahub_inventory_expired" and calls == [URN]


def test_inventory_owned_client_closes_on_failure_and_redacts_native_error(monkeypatch):
    def fail(request):
        raise httpx.ReadError("fixture-token private diagnostics")

    client = httpx.Client(transport=httpx.MockTransport(fail))
    monkeypatch.setattr(datahub.httpx, "Client", lambda **kwargs: client)
    with pytest.raises(ConnectorError) as raised:
        inventory_records(endpoint=ENDPOINT, bearer_token="fixture-token", namespace="finance", urns=[URN])
    assert client.is_closed
    assert raised.value.__cause__ is None
    assert "fixture-token" not in str(raised.value) and "private" not in str(raised.value)


def test_fixed_native_queries_use_schema_valid_owner_union_fragments():
    for query in (QUERY, INVENTORY_QUERY):
        assert "... on CorpUser { urn } ... on CorpGroup { urn }" in query
        assert "owner { urn }" not in query
