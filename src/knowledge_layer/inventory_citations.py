"""Exact raw inventory citations for the existing snapshot assertion workflow.

This is a reference adapter over InventoryStore.select, not another semantic
registry. Native metadata supplies evidence; the target assertion has its own
local review. Neither native approval nor a citation grants source execution.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from knowledge_layer.inventory import InventoryStore, _text
from knowledge_layer.store import KnowledgeConflict, KnowledgeValidationError, validate_tenant_id

INVENTORY_REF_KEYS = frozenset({"connection_id", "inventory_id", "manifest_digest", "entity_id", "evidence_id"})
_MARKERS = INVENTORY_REF_KEYS - {"connection_id", "evidence_id"}
MAX_CITATION_SELECTIONS = 64
MAX_CITATION_CHECKS = 256
_OPERATION = ContextVar("inventory_citation_operation", default=None)


class InventoryCitationBudgetExceeded(KnowledgeConflict):
    """Refuse the entire read, rather than silently omit approved dependencies."""


@contextmanager
def inventory_citation_operation():
    """Share a bounded payload cache only within one synchronous operation."""
    token = None
    if _OPERATION.get() is None:
        token = _OPERATION.set({"checks": 0, "selections": 0, "cache": {}})
    try:
        yield
    finally:
        if token is not None:
            _OPERATION.reset(token)


def is_inventory_reference(ref):
    """Recognize inventory-shaped refs, including malformed mixed envelopes."""
    return isinstance(ref, dict) and bool(_MARKERS.intersection(ref))


def validate_inventory_reference(ref):
    if not isinstance(ref, dict) or set(ref) != INVENTORY_REF_KEYS:
        raise KnowledgeValidationError("Inventory citations require exact entity, evidence and manifest pins")
    for key in ("connection_id", "inventory_id"):
        _text(ref[key], "inventory citation identity", 64)
    for key in ("entity_id", "evidence_id"):
        _text(ref[key], "inventory citation record ID", 256)
    if not isinstance(ref["manifest_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", ref["manifest_digest"]):
        raise KnowledgeValidationError("Inventory citations require an exact manifest digest")
    return deepcopy(ref)


def resolve_inventory_citation(store, tenant_id, ref):
    """Resolve one exact current entity-linked proof, preserving existing gates."""
    with inventory_citation_operation():
        return _resolve(store, tenant_id, ref)


def _resolve(store, tenant_id, ref):
    tenant = validate_tenant_id(tenant_id)
    checked = validate_inventory_reference(ref)
    operation = _OPERATION.get()
    operation["checks"] += 1
    key = (id(store), tenant, *(checked[name] for name in sorted(INVENTORY_REF_KEYS)))
    if operation["checks"] > MAX_CITATION_CHECKS:
        raise InventoryCitationBudgetExceeded("Inventory citation revalidation exceeds 256 checks; narrow the source scope")
    reader = InventoryStore(store)
    packet = operation["cache"].get(key)
    if packet is None:
        if operation["selections"] >= MAX_CITATION_SELECTIONS:
            raise InventoryCitationBudgetExceeded("Inventory citation selection exceeds 64 records; narrow the source scope")
        operation["selections"] += 1
        packet = reader.select(tenant, checked["connection_id"], checked["inventory_id"],
            expected_manifest_digest=checked["manifest_digest"], entity_ids=[checked["entity_id"]])
    entity = packet["entities"][0]
    proof = next((item for item in packet["evidence"] if item["id"] == checked["evidence_id"]), None)
    if checked["evidence_id"] not in entity["evidence_ids"] or proof is None:
        raise KnowledgeValidationError("Inventory evidence must belong to the exact selected entity")
    with reader._current_read_connection() as conn:
        source, _, _ = reader._source(conn, tenant, checked["connection_id"])
        _, head = reader._current_committed(conn, tenant, checked["connection_id"],
            checked["inventory_id"], checked["manifest_digest"])
        if source["status"] == "error" or head["generation"] != packet["inventory"]["generation"]:
            raise KnowledgeConflict("The cited inventory source is no longer current")
    operation["cache"][key] = packet
    return deepcopy(({"inventory_id": checked["inventory_id"], "manifest_digest": checked["manifest_digest"],
                      "entity": entity, "inventory": packet["inventory"]}, proof))
