"""Compact authorized task packets with short-lived, revalidatable receipts.

Receipts record registry context. They grant no source execution permission and
cannot attest that a customer harness followed the selected instructions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from knowledge_layer.context_receipts import MAX_RECEIPT_SECONDS, ReceiptKeys
from knowledge_layer.guidance import GuidanceRegistry
from knowledge_layer.inventory_citations import inventory_citation_operation
from knowledge_layer.inventory_concepts import InventoryConcepts
from knowledge_layer.mcp import _compact_term_context
from knowledge_layer.store import KnowledgeConflict, KnowledgeNotFound, KnowledgeValidationError

_RECEIPT_FIELDS = {"schema_version", "tenant_id", "principal_digest", "policy_digest", "context_digest",
                   "request", "issued_at", "expires_at", "signature"}
_REQUEST_FIELDS = {"query", "connection_ids", "business_unit", "task"}
_INVENTORY_ID = re.compile(r"inventory_concept_[a-f0-9]{32}")


def _encoded(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise KnowledgeValidationError("Context must contain finite UTF-8 JSON") from exc


def _identity(store: Any) -> str:
    principal = store._principal()
    value = ({"subject": principal.subject, "groups": sorted(principal.groups), "scopes": sorted(principal.scopes)}
             if principal else {"operator": True})
    return hashlib.sha256(_encoded(value)).hexdigest()


def _policy(store: Any) -> str:
    policy = store.source_policy
    value = [{"tenant": entry.tenant_id, "identity": entry.identity,
              "subjects": sorted(entry.subjects), "groups": sorted(entry.groups)}
             for entry in policy.approvals] if policy else None
    return hashlib.sha256(_encoded(value)).hexdigest()


def _keys(store: Any) -> ReceiptKeys:
    try:
        return store._context_receipt_keys.current()
    except ValueError as exc:
        raise KnowledgeConflict("Context receipt key configuration is unavailable; contact the operator") from exc


def task_context(store: Any, *, tenant_id: str, query: str, connection_ids: list[str],
                 business_unit: str | None = None, task: str | None = None,
                 inventory_selection: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return _task_context(store, keys=_keys(store), tenant_id=tenant_id, query=query,
                         connection_ids=connection_ids, business_unit=business_unit, task=task,
                         inventory_selection=inventory_selection)


def _selection(value: Any, connection_ids: list[str]) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise KnowledgeValidationError("Select 1-20 inventory concepts from the selected task sources")
    selected, handles = {}, set()
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"connection_id", "concept_ids"}
                or not isinstance(item["connection_id"], str)
                or item["connection_id"] not in connection_ids or item["connection_id"] in selected
                or not isinstance(item["concept_ids"], list) or not 1 <= len(item["concept_ids"]) <= 20):
            raise KnowledgeValidationError("Inventory selections require distinct selected source connections")
        for handle in item["concept_ids"]:
            if not isinstance(handle, str) or not _INVENTORY_ID.fullmatch(handle) or handle in handles:
                raise KnowledgeValidationError("Select distinct exact published inventory concept IDs")
            handles.add(handle)
        selected[item["connection_id"]] = list(item["concept_ids"])
    if len(handles) > 20:
        raise KnowledgeValidationError("Select at most 20 inventory concepts per task context")
    return selected


def _digest(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _source_binding(source: dict[str, Any]) -> dict[str, Any]:
    return {key: source.get(key) for key in ("kind", "config", "status", "last_error", "discovery_mode")}


def _inventory_packets(context: dict[str, Any]) -> list[dict[str, Any]]:
    packets = {item["id"]: item for item in context.get("matches", []) if item.get("basis") == "inventory"}
    for candidate in context.get("retrieval_candidates", []):
        item = candidate.get("context") or {}
        if item.get("basis") == "inventory":
            packets[item["id"]] = item
    return list(packets.values())


def _inventory_pin(cid: str, packets: list[dict[str, Any]]) -> dict[str, Any]:
    return {"connection_id": cid, "basis": "inventory", "concept_ids": [item["id"] for item in packets],
            "concept_pins": [{"concept_id": item["id"], "evidence_ref": item["evidence_ref"],
                "proposal_digest": item["proposal_digest"], "entity_digest": item["entity_digest"],
                "evidence_digest": item["evidence_digest"], "inventory": item["inventory"],
                "publication_digest": _digest(item["publication"]), "decision_digest": _digest(item["decision"]),
                "observed_at": item["inventory"].get("last_complete_scope_at")} for item in packets],
            "effective_digest": _digest(packets), "source_agent_ready": False, "locally_executable": False}


def _without_inventory(context: dict[str, Any]) -> dict[str, Any]:
    result = {**context, "matches": [item for item in context.get("matches", []) if item.get("basis") != "inventory"],
              "retrieval_candidates": [item for item in context.get("retrieval_candidates", [])
                    if (item.get("context") or {}).get("basis") != "inventory"]}
    # A selected-ID request does not report counts for unselected inventory meaning.
    for key in tuple(result):
        if key.startswith("inventory_"):
            result.pop(key)
    result["total_matches"] = len(result["matches"])
    result["retrieval_candidate_total_matches"] = len(result["retrieval_candidates"])
    return result


def _task_term_context(context: dict[str, Any]) -> dict[str, Any]:
    inventory = _inventory_packets(context)
    if not inventory:
        return _compact_term_context(context)
    compact = _compact_term_context(_without_inventory(context))
    return {**context, **{key: compact[key] for key in (
        "metric_matches", "cited_glossary", "cited_upstream_definitions") if key in compact},
        "matches": compact["matches"] + [item for item in context.get("matches", [])
                                         if item.get("basis") == "inventory"],
        "retrieval_candidates": compact["retrieval_candidates"] + [item for item in context.get("retrieval_candidates", [])
                                             if (item.get("context") or {}).get("basis") == "inventory"],
        "more_available": bool(context.get("more_available") or compact["more_available"]),
        "retrieval_candidate_more_available": bool(context.get("retrieval_candidate_more_available")
                                                   or compact["retrieval_candidate_more_available"])}


@inventory_citation_operation()
def _task_context(store: Any, *, keys: ReceiptKeys, tenant_id: str, query: str,
                  connection_ids: list[str], business_unit: str | None, task: str | None,
                  inventory_selection: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 120
            or any(ord(char) < 32 for char in query)):
        raise KnowledgeValidationError("Task query must be 1-120 characters")
    if (not isinstance(connection_ids, list) or not 1 <= len(connection_ids) <= 8
            or any(not isinstance(cid, str) or not 1 <= len(cid) <= 64 for cid in connection_ids)
            or len(set(connection_ids)) != len(connection_ids)):
        raise KnowledgeValidationError("Select 1-8 distinct source connections")
    selected = _selection(inventory_selection, connection_ids)
    connection_ids = list(connection_ids)
    principal_digest, policy_digest = _identity(store), _policy(store)
    guidance = GuidanceRegistry(store).resolve(tenant_id, business_unit=business_unit, task=task,
                                              connection_ids=connection_ids)
    sources, contexts, blockers = [], [], []
    inventory_packets, candidate_pins, bindings = {}, {}, {}
    inventory_aware = inventory_selection is not None
    snapshot_matches, meaning_available = False, False
    for cid in connection_ids:
        source = store.get_connection(tenant_id, cid)
        if source is None:
            raise KnowledgeNotFound("Selected task source was not found")
        store._require_source_approval(tenant_id, source["kind"], source["config"])
        snapshot = source.get("snapshot") or {}
        if not snapshot and source.get("status") == "error":
            raise KnowledgeConflict("Task inventory source is unavailable; resolve after a complete refresh")
        if not snapshot:
            with InventoryConcepts(store).inventory._current_read_connection() as conn:
                InventoryConcepts(store)._no_active_refresh(conn, tenant_id, cid)
        packets = []
        if cid in selected:
            packets = [InventoryConcepts(store).get(tenant_id, cid, concept_id=handle) for handle in selected[cid]]
            context = {"query": query.strip(), "matches": packets, "total_matches": len(packets),
                       "more_available": False, "retrieval_candidates": [], "match_basis": "explicit_selection"}
            if snapshot:
                from knowledge_layer.retrieval import term_context

                legacy = term_context(store, tenant_id=tenant_id, term=query.strip(), connection_id=cid,
                                      concepts=store.get_concepts(tenant_id, connection_id=cid), include_inventory=False)
                legacy = _without_inventory(legacy or {})
                context = {**legacy, "matches": legacy.get("matches", []) + packets,
                           "total_matches": legacy.get("total_matches", 0) + len(packets),
                           "match_basis": "explicit_selection"}
        else:
            context = store.get_context(tenant_id, term=query.strip(), connection_id=cid) or {}
            packets = _inventory_packets(context)
            candidate_pin = context.get("inventory_candidate_digest")
            if candidate_pin is not None:
                candidate_pins[cid] = candidate_pin
        inventory_aware |= bool(packets) or source.get("discovery_mode") == "inventory" or bool(candidate_pins.get(cid))
        if packets:
            inventory_packets[cid] = packets
            if sum(len(items) for items in inventory_packets.values()) > 20:
                raise KnowledgeValidationError("Task context selects more than 20 inventory concepts; narrow the scope")
            sources.append(_inventory_pin(cid, packets))
        elif not snapshot and source.get("discovery_mode") == "inventory" and cid not in candidate_pins:
            blockers.append({"connection_id": cid, "reason": "inventory_not_current"})
        # An inventory-only source has no dataset snapshot to publish or execute.
        if snapshot or not (cid in selected or cid in candidate_pins or source.get("discovery_mode") == "inventory"):
            sources.append({"connection_id": cid, "snapshot_id": snapshot.get("id"),
                        "snapshot_version": snapshot.get("version"), "agent_ready": snapshot.get("agent_ready") is True,
                        "observed_at": source.get("last_complete_refresh_at"),
                        "effective_digest": _digest(snapshot)})
            if snapshot.get("agent_ready") is not True:
                blockers.append({"connection_id": cid, "reason": "source_not_published_current"})
        bindings[cid] = _digest(_source_binding(source))
        meaning_available |= any(item.get("basis") == "inventory" for item in context.get("matches", []))
        snapshot_matches |= bool(snapshot) and any(
            item.get("basis") != "inventory" for item in context.get("matches", []))
        snapshot_matches |= bool(snapshot) and any(context.get(key) for key in (
            "metric_matches", "cited_glossary", "cited_upstream_definitions"))
        if context:
            contexts.append({"connection_id": cid, "context": _task_term_context(context)})
    if guidance["status"] == "blocked":
        blockers.append({"reason": "company_guidance_blocked"})
    matches = any(item["context"].get(key) for item in contexts
                  for key in ("matches", "metric_matches", "cited_glossary", "cited_upstream_definitions"))
    packet = {"schema_version": "task-context-v2" if inventory_aware else "task-context-v1", "query": query.strip(),
              "status": "blocked" if blockers else "context_available" if matches else "no_direct_match",
              "answerability": "requires_source_execution" if matches and not blockers else "not_established",
              "blockers": blockers, "sources": sources, "contexts": contexts,
              "guidance": guidance, "coverage": "selected_authorized_sources_only",
              "execution_state": "not_executed", "rules_enforced": False}
    if inventory_aware:
        packet.update(meaning_available=meaning_available, query_coverage="not_established",
                      inventory_selection_mode="explicit_handles" if inventory_selection is not None else "bounded_term_lookup")
        packet["answerability"] = ("requires_source_execution" if snapshot_matches and not blockers else
            "business_meaning_available" if meaning_available and not blockers else "not_established")
        for captured in sources:
            if captured.get("basis") != "inventory":
                captured.update(basis="snapshot", source_binding_digest=bindings[captured["connection_id"]])
            else:
                captured["source_binding_digest"] = bindings[captured["connection_id"]]
    if len(_encoded(packet)) > 256 * 1024:
        raise KnowledgeValidationError("Task context exceeds 256 KiB; narrow the selected scope")
    observed_guidance = GuidanceRegistry(store).resolve(tenant_id, business_unit=business_unit, task=task,
                                                      connection_ids=connection_ids)
    if ({key: value for key, value in observed_guidance.items() if key != "evaluated_at"}
            != {key: value for key, value in guidance.items() if key != "evaluated_at"}):
        raise KnowledgeConflict("Company guidance changed during assembly; resolve current context")
    if candidate_pins:
        from knowledge_layer.inventory_concept_search import InventoryConceptSearch
        for cid, digest in candidate_pins.items():
            if InventoryConceptSearch(store).candidates(
                    tenant_id, query.strip(), connection_id=cid)["candidate_digest"] != digest:
                raise KnowledgeConflict("Inventory search changed during assembly; resolve current context")
    for captured in sources:
        cid = captured["connection_id"]
        if captured.get("basis") == "inventory":
            observed_packets = [InventoryConcepts(store).get(tenant_id, cid, concept_id=handle)
                                for handle in captured["concept_ids"]]
            if _digest(observed_packets) != captured["effective_digest"]:
                raise KnowledgeConflict("Inventory meaning changed during assembly; resolve current context")
        else:
            observed = store.get_connection(tenant_id, cid)
            if (not observed or _digest(observed.get("snapshot") or {}) != captured["effective_digest"]
                    or (inventory_aware and _digest(_source_binding(observed)) != bindings[cid])):
                raise KnowledgeConflict("Task source changed during assembly; resolve current context")
    for cid in connection_ids:
        observed = store.get_connection(tenant_id, cid)
        if observed is None:
            raise KnowledgeNotFound("Selected task source was not found")
        store._require_source_approval(tenant_id, observed["kind"], observed["config"])
        if observed.get("snapshot") is None:
            with InventoryConcepts(store).inventory._current_read_connection() as conn:
                InventoryConcepts(store)._no_active_refresh(conn, tenant_id, cid)
        if inventory_aware and _digest(_source_binding(observed)) != bindings[cid]:
            raise KnowledgeConflict("Task source binding changed during assembly; resolve current context")
    if _identity(store) != principal_digest or _policy(store) != policy_digest:
        raise KnowledgeConflict("Task authority changed during assembly; resolve current context")
    # Resolve clocks are observations, not revisions; exclude them from the pin.
    stable = {**packet, "guidance": {key: value for key, value in guidance.items() if key != "evaluated_at"}}
    now = datetime.now(UTC)
    receipt = {"schema_version": "context-receipt-v2" if keys.shared else "context-receipt-v1", "tenant_id": tenant_id,
               "principal_digest": principal_digest, "policy_digest": policy_digest,
               "context_digest": hashlib.sha256(_encoded(stable)).hexdigest(),
               "request": {"query": query.strip(), "connection_ids": connection_ids,
                           "business_unit": business_unit, "task": task},
               "issued_at": now.isoformat(), "expires_at": (now + timedelta(seconds=MAX_RECEIPT_SECONDS)).isoformat()}
    if inventory_selection is not None:
        receipt["request"]["inventory_selection"] = [{"connection_id": cid, "concept_ids": list(handles)}
                                                      for cid, handles in selected.items()]
    if keys.shared:
        receipt.update(registry_id=keys.registry_id, key_id=keys.active_key_id)
    receipt["signature"] = hmac.new(keys.keys[keys.active_key_id], _encoded(receipt), hashlib.sha256).hexdigest()
    if len(_encoded(receipt)) > 4096:
        raise KnowledgeValidationError("Context receipt exceeds 4096 bytes; narrow the selected scope")
    return {**packet, "receipt": receipt, "revalidate_before_use": True,
            "receipt_route": "shared_registry_replicas" if keys.shared else "same_registry_process_required"}


def revalidate_context(store: Any, *, tenant_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, dict) or len(_encoded(receipt)) > 4096:
        raise KnowledgeValidationError("Invalid context receipt")
    keys = _keys(store)
    fields = _RECEIPT_FIELDS | ({"registry_id", "key_id"} if keys.shared else set())
    if (set(receipt) != fields
            or receipt.get("schema_version") != ("context-receipt-v2" if keys.shared else "context-receipt-v1")
            or (keys.shared and (receipt.get("registry_id") != keys.registry_id
                                 or not isinstance(receipt.get("key_id"), str)))):
        raise KnowledgeNotFound("Context receipt was not found")
    key = keys.keys.get(receipt.get("key_id"))
    unsigned = {key: value for key, value in receipt.items() if key != "signature"}
    signature = receipt.get("signature")
    if (not isinstance(signature, str) or not re.fullmatch(r"[a-f0-9]{64}", signature)
            or key is None or not hmac.compare_digest(signature, hmac.new(key, _encoded(unsigned), hashlib.sha256).hexdigest())
            or receipt.get("tenant_id") != tenant_id or receipt.get("principal_digest") != _identity(store)):
        raise KnowledgeNotFound("Context receipt was not found")
    try:
        issued = datetime.fromisoformat(receipt["issued_at"])
        expires = datetime.fromisoformat(receipt["expires_at"])
        now = datetime.now(UTC)
        if (issued.utcoffset() is None or expires.utcoffset() is None or issued > now
                or not 0 < (expires - issued).total_seconds() <= MAX_RECEIPT_SECONDS):
            raise ValueError("invalid clock")
        if now >= expires:
            raise KnowledgeConflict("Context receipt expired; resolve fresh context")
    except (KeyError, TypeError, ValueError) as exc:
        raise KnowledgeValidationError("Invalid context receipt") from exc
    if (not isinstance(receipt["request"], dict)
            or set(receipt["request"]) not in (_REQUEST_FIELDS, _REQUEST_FIELDS | {"inventory_selection"})):
        raise KnowledgeValidationError("Invalid context receipt")
    if _policy(store) != receipt["policy_digest"]:
        raise KnowledgeConflict("Context changed after source policy update; resolve fresh context")
    current = _task_context(store, keys=keys, tenant_id=tenant_id, **receipt["request"])
    if any(current["receipt"][key] != receipt[key] for key in ("policy_digest", "context_digest")):
        raise KnowledgeConflict("Context changed; resolve fresh context")
    if current["status"] == "blocked":
        raise KnowledgeConflict("Context blocked; resolve current published knowledge before use")
    result = {"status": "current", "context_status": current["status"],
            "answerability": current["answerability"], "blockers": current["blockers"],
            "receipt": current["receipt"], "execution_state": "not_executed",
            "source_permissions_verified": False, "rules_enforced": False}
    if current["schema_version"] == "task-context-v2":
        result.update(meaning_available=current["meaning_available"], query_coverage=current["query_coverage"])
    return result
