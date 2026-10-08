"""A narrow read-only DataHub glossary adapter for explicit operator URNs.

Only fixed GraphQL reads are available. Null, denied, partial and errored
responses refuse the scope rather than inventing a term or establishing deletion.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_adapter import (
    _metadata_string,
    _response_object,
    _upstream_definition,
    decode_response,
)
from knowledge_layer.inventory import _encoded, _records, inventory_digest
from knowledge_layer.store import KnowledgeValidationError

QUERY = """query KnowledgeGlossary($urn: String!) {
  glossaryTerm(urn: $urn) {
    urn
    properties { name description termSource sourceRef }
    ownership { owners { owner { ... on CorpUser { urn } ... on CorpGroup { urn } } type } }
  }
}"""
MAX_RESPONSE_BYTES = 64 * 1024
MAX_INVENTORY_TERMS = 1000
MAX_INVENTORY_BYTES = 16 * 1024 * 1024
# OwnerType is a union, so owner URNs require concrete inline fragments.
# https://github.com/datahub-project/datahub/blob/master/datahub-graphql-core/src/main/resources/entity.graphql
INVENTORY_QUERY = """query KnowledgeGlossaryInventory($urn: String!) {
  glossaryTerm(urn: $urn) {
    urn
    properties {
      name description termSource sourceRef sourceUrl rawSchema
      customProperties { key value associatedUrn }
      createdOn { time actor { urn } }
    }
    ownership {
      owners {
        owner { ... on CorpUser { urn } ... on CorpGroup { urn } }
        type ownershipType { urn } source { type url } associatedUrn
      }
      lastModified { time actor }
    }
  }
}"""


def _inventory_controls(endpoint, bearer_token, namespace, urns, deadline):
    try:
        parsed = urlparse(endpoint)
        if (not isinstance(endpoint, str) or any(ord(char) <= 32 or ord(char) == 127 for char in endpoint)
                or parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path != "/api/graphql"):
            raise ValueError
        # Force validation of malformed ports before creating a client.
        _ = parsed.port
    except (ValueError, TypeError, AttributeError):
        raise ConnectorError("datahub_endpoint_invalid", "Use an operator-approved HTTPS DataHub GraphQL endpoint") from None
    text = [namespace, *urns] if isinstance(urns, list) else []
    if (not isinstance(namespace, str) or not 1 <= len(namespace) <= 120 or not namespace.strip()
            or not isinstance(bearer_token, str) or not bearer_token.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in bearer_token)
            or not isinstance(urns, list) or not 1 <= len(urns) <= MAX_INVENTORY_TERMS
            or any(not isinstance(urn, str) or not urn.startswith("urn:li:glossaryTerm:")
                   or not urn.removeprefix("urn:li:glossaryTerm:").strip() or len(urn) > 256 for urn in urns)
            or len(set(urns)) != len(urns)
            or any(not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value)
                   for value in text)
            or deadline is not None and (not isinstance(deadline, datetime) or deadline.utcoffset() is None)):
        raise ConnectorError("datahub_scope_invalid", "Select a bounded namespace and 1-1000 distinct glossary URNs")
    try:
        for value in [endpoint, bearer_token, *text]:
            value.encode("utf-8")
    except UnicodeError:
        raise ConnectorError("datahub_scope_invalid", "DataHub scope must contain valid Unicode") from None


def _remaining(deadline):
    if deadline is None:
        return 15.0
    seconds = (deadline - datetime.now(UTC)).total_seconds()
    if seconds <= 0:
        raise ConnectorError("datahub_inventory_expired", "DataHub inventory capture expired")
    return min(15.0, seconds)


def _inventory_term(payload, urn):
    """Required selected fields must be present, even when natively nullable."""
    if (not isinstance(payload, dict) or set(payload) - {"data", "extensions"}
            or not isinstance(payload.get("data"), dict) or set(payload["data"]) != {"glossaryTerm"}):
        raise ConnectorError("datahub_partial", "DataHub returned partial or invalid metadata")
    term = payload["data"]["glossaryTerm"]
    if (not isinstance(term, dict) or term.get("urn") != urn
            or not isinstance(term.get("properties"), dict) or "ownership" not in term):
        raise ConnectorError("datahub_scope_incomplete", "A selected term is unavailable; removal is not established")
    properties = term["properties"]
    if (not {"name", "description", "termSource", "sourceRef", "sourceUrl", "rawSchema",
             "customProperties", "createdOn"} <= set(properties)
            or not isinstance(properties["name"], str) or not properties["name"].strip()
            or not isinstance(properties["termSource"], str) or not properties["termSource"].strip()
            or any(properties[key] is not None and not isinstance(properties[key], str)
                   for key in ("description", "sourceRef", "sourceUrl", "rawSchema"))):
        raise ConnectorError("datahub_partial", "DataHub returned incomplete glossary properties")
    custom = properties["customProperties"]
    if custom is not None and (not isinstance(custom, list) or any(
            not isinstance(item, dict) or not {"key", "value", "associatedUrn"} <= set(item)
            or not isinstance(item["key"], str) or not isinstance(item["associatedUrn"], str)
            or item["value"] is not None and not isinstance(item["value"], str) for item in custom)):
        raise ConnectorError("datahub_partial", "DataHub returned incomplete custom properties")
    created = properties["createdOn"]
    if created is not None and (not isinstance(created, dict) or not {"time", "actor"} <= set(created)
            or type(created["time"]) is not int
            or created["actor"] is not None and (not isinstance(created["actor"], dict)
                                                 or not isinstance(created["actor"].get("urn"), str))):
        raise ConnectorError("datahub_partial", "DataHub returned incomplete creation metadata")
    ownership = term["ownership"]
    if ownership is not None:
        if (not isinstance(ownership, dict) or not {"owners", "lastModified"} <= set(ownership)
                or not isinstance(ownership["lastModified"], dict)
                or not {"time", "actor"} <= set(ownership["lastModified"])
                or type(ownership["lastModified"]["time"]) is not int
                or ownership["lastModified"]["actor"] is not None
                and not isinstance(ownership["lastModified"]["actor"], str)):
            raise ConnectorError("datahub_partial", "DataHub returned incomplete ownership metadata")
        owners = ownership["owners"]
        if owners is not None:
            if not isinstance(owners, list):
                raise ConnectorError("datahub_partial", "DataHub returned incomplete owner metadata")
            for owner in owners:
                if (not isinstance(owner, dict)
                        or not {"owner", "type", "ownershipType", "source", "associatedUrn"} <= set(owner)
                        or not isinstance(owner["owner"], dict)
                        or not isinstance(owner["owner"].get("urn"), str)
                        or not isinstance(owner["associatedUrn"], str)
                        or owner["type"] is not None and not isinstance(owner["type"], str)
                        or owner["ownershipType"] is not None
                        and (not isinstance(owner["ownershipType"], dict)
                             or not isinstance(owner["ownershipType"].get("urn"), str))
                        or owner["source"] is not None and (not isinstance(owner["source"], dict)
                            or not {"type", "url"} <= set(owner["source"])
                            or not isinstance(owner["source"]["type"], str)
                            or owner["source"]["url"] is not None and not isinstance(owner["source"]["url"], str))):
                    raise ConnectorError("datahub_partial", "DataHub returned incomplete owner metadata")
    return term


def inventory_records(*, endpoint: str, bearer_token: str, namespace: str,
                      urns: list[str], client: httpx.Client | None = None,
                      deadline: datetime | None = None) -> dict:
    """Capture full selected records, without a vendor atomic-snapshot claim.

    The caller owns the namespace/source boundary. Native ownership is retained
    as evidence and grants no local permission or executable semantic authority.
    A null, partial, denied or malformed term refuses the entire capture.
    """
    _inventory_controls(endpoint, bearer_token, namespace, urns, deadline)
    _remaining(deadline)
    own_client = client is None
    transport = client or httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False)
    entities, evidence, revisions = [], [], []
    response_bytes, record_bytes = 0, 0
    try:
        for urn in sorted(urns):
            remaining = _remaining(deadline)
            with transport.stream("POST", endpoint, follow_redirects=False,
                    timeout=httpx.Timeout(remaining, connect=min(5, remaining)),
                    headers={"Authorization": f"Bearer {bearer_token}", "Accept": "application/json",
                             "Accept-Encoding": "identity"},
                    json={"query": INVENTORY_QUERY, "variables": {"urn": urn}}) as response:
                if response.status_code != 200:
                    raise ConnectorError("datahub_unavailable", "DataHub scope is unavailable or denied")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ConnectorError("datahub_encoding_refused", "DataHub inventory requires an uncompressed response")
                raw = bytearray()
                for chunk in response.iter_raw():
                    _remaining(deadline)
                    response_bytes += len(chunk)
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES or response_bytes > MAX_INVENTORY_BYTES:
                        raise ConnectorError("datahub_budget_exceeded", "DataHub inventory exceeds its response budget")
                    raw.extend(chunk)
            _remaining(deadline)
            try:
                payload = decode_response(bytes(raw), max_bytes=MAX_RESPONSE_BYTES)
            except ConnectorError:
                raise ConnectorError("datahub_response_invalid", "DataHub returned invalid bounded JSON") from None
            term = _inventory_term(payload, urn)
            name = term["properties"].get("name")
            revision = inventory_digest(term)
            observed = datetime.now(UTC).isoformat()
            identity = inventory_digest({"namespace": namespace, "urn": urn})
            proof_id = "datahub.definition." + identity
            provenance = {"native_id": urn, "namespace": namespace, "authority": "datahub",
                          "native_kind": "glossary_term", "revision": revision,
                          "source_observed_at": observed, "native_version": None,
                          "locally_executable": False}
            entity = {"id": "datahub.term." + identity, "kind": "semantic_definition",
                      "payload": {**provenance, "name": name,
                                  "definition": term["properties"].get("description")},
                      "evidence_ids": [proof_id]}
            proof = {"id": proof_id, "kind": "upstream_semantic_definition",
                     "payload": {**provenance, "native_record": deepcopy(term)}}
            _records([entity])
            _records([proof], evidence=True)
            record_bytes += len(_encoded(entity)) + len(_encoded(proof))
            if record_bytes > MAX_INVENTORY_BYTES:
                raise ConnectorError("datahub_budget_exceeded", "DataHub inventory exceeds its complete-record budget")
            entities.append(entity)
            evidence.append(proof)
            revisions.append({"urn": urn, "revision": revision})
        _remaining(deadline)
        result = {"entities": entities, "evidence": evidence,
                  "watermark": {"kind": "selected_terms_content_digest",
                                "value": inventory_digest({"namespace": namespace, "terms": revisions})}}
        if len(_encoded(result)) > MAX_INVENTORY_BYTES:
            raise ConnectorError("datahub_budget_exceeded", "DataHub complete capture exceeds its byte budget")
        _remaining(deadline)
        return result
    except ConnectorError:
        raise
    except KnowledgeValidationError:
        raise ConnectorError("datahub_budget_exceeded", "DataHub full inventory record exceeds its byte budget") from None
    except (httpx.HTTPError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ConnectorError("datahub_unavailable", "DataHub did not return valid bounded metadata") from None
    finally:
        if own_client:
            transport.close()


def discover_glossary(*, endpoint: str, bearer_token: str, namespace: str,
                      urns: list[str], client: httpx.Client | None = None) -> dict:
    try:
        parsed = urlparse(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != "/api/graphql"):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ConnectorError("datahub_endpoint_invalid", "Use an operator-approved HTTPS DataHub GraphQL endpoint") from exc
    if (not isinstance(bearer_token, str) or not bearer_token or "\n" in bearer_token or "\r" in bearer_token
            or not isinstance(urns, list) or not 1 <= len(urns) <= 100
            or any(not isinstance(urn, str) or not urn.startswith("urn:li:glossaryTerm:")
                   or not 1 <= len(urn) <= 256 or any(ord(char) < 32 for char in urn) for urn in urns)
            or len(set(urns)) != len(urns)):
        raise ConnectorError("datahub_scope_invalid", "Select 1-100 distinct native glossary URNs with a read-only token")
    own_client = client is None
    transport = client or httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False)
    assets = []
    try:
        for urn in urns:
            with transport.stream("POST", endpoint, follow_redirects=False,
                                  headers={"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"},
                                  json={"query": QUERY, "variables": {"urn": urn}}) as response:
                if response.status_code != 200:
                    raise ConnectorError("datahub_unavailable", "DataHub scope is unavailable or denied")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ConnectorError("datahub_budget_exceeded", "DataHub term exceeds its response budget")
                    raw.extend(chunk)
            payload = json.loads(raw, object_pairs_hook=_response_object)
            if (not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data"), dict)):
                raise ConnectorError("datahub_partial", "DataHub returned partial or invalid metadata")
            term = payload["data"].get("glossaryTerm")
            if not isinstance(term, dict) or term.get("urn") != urn or not isinstance(term.get("properties"), dict):
                raise ConnectorError("datahub_scope_incomplete", "A selected DataHub term is unavailable; removal is not established")
            properties = term["properties"]
            _metadata_string(properties.get("name"), "DataHub term name")
            native = json.dumps(term, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            if len(native.encode()) > 4000:
                raise ConnectorError("datahub_budget_exceeded", "DataHub term exceeds its opaque native definition budget")
            digest = hashlib.sha256(native.encode()).hexdigest()
            observed = datetime.now(UTC).isoformat()
            upstream = {"id": urn, "kind": "glossary_term", "namespace": namespace,
                        "definition": properties.get("description") or native, "version": None,
                        "formula": native, "authority": "datahub", "source_observed_at": observed,
                        "review_status": None, "reference": "datahub/glossary/" + hashlib.sha256(urn.encode()).hexdigest()}
            _upstream_definition(upstream, properties.get("name"))
            assets.append({"name": properties["name"], "pointer": upstream["reference"],
                           "asset_type": "semantic_definition", "schema": [], "revision": digest,
                           "upstream_definition": upstream})
    except ConnectorError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise ConnectorError("datahub_unavailable", "DataHub did not return valid bounded metadata") from exc
    finally:
        if own_client:
            transport.close()
    return {"protocol_version": "1.2", "listing_complete": True, "next_cursor": None,
            "assets": assets, "coverage": "explicit_operator_glossary_urns",
            "native_permission_propagation": "not_qualified", "locally_executable": False}
