"""Shared receipt signatures never replace current registry authorization.

All sources and registries are disposable copies of the existing enterprise
example. No provider, model, listener, or real credential is involved.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import importlib.util
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.context_receipts import CONFIG_NAME
from knowledge_layer.guidance import GuidanceRegistry
from knowledge_layer.source_policy import SourceApproval, SourcePolicy, source_identity
from knowledge_layer.store import (
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)
from knowledge_layer.task_context import revalidate_context, task_context

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "enterprise_consumer.py"
spec = importlib.util.spec_from_file_location("receipt_replicas_enterprise_example", EXAMPLE)
assert spec and spec.loader
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)
enterprise_fixture = example.enterprise_fixture

# Public deterministic fixture material, never a deployed signing key.
KEYS = {"old": bytes(range(32)), "new": bytes(range(32, 64))}
READER = Principal("receipt-reader", frozenset({"analysts"}), frozenset({"knowledge.read"}))


def _ring(*, active="old", retained=("old",), registry="replica-test"):
    return json.dumps({
        "version": 1, "registry_id": registry, "active_key_id": active,
        "keys": [{"id": key_id,
                  "secret": base64.urlsafe_b64encode(KEYS[key_id]).decode().rstrip("=")}
                 for key_id in retained],
    })


def _replace(path, contents):
    replacement = path.with_name(path.name + ".replacement")
    replacement.write_text(contents, encoding="utf-8")
    replacement.chmod(0o600)
    os.replace(replacement, path)


@contextmanager
def _replica(company):
    store = KnowledgeStore(database_url=f"sqlite:///{company['root'] / 'registry.db'}",
                           local_files_root=company["root"], initialize_schema=False)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def company(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_NAME, raising=False)
    key_file = tmp_path / "context-keys"
    _replace(key_file, _ring())
    monkeypatch.setenv(CONFIG_NAME + "_FILE", str(key_file))
    with enterprise_fixture() as fixture, _replica(fixture) as peer:
        yield {**fixture, "peer": peer, "key_file": key_file,
               "sources": [item["connection_id"] for item in fixture["bindings"].values()]
                          + [fixture["document_connection_id"]]}


def _packet(company, store=None):
    return task_context(store or company["store"], tenant_id="demo", query="Revenue",
                        connection_ids=company["sources"])


def _check(company, receipt, store=None):
    return revalidate_context(store or company["peer"], tenant_id="demo", receipt=receipt)


def _signed(receipt, **changes):
    altered = copy.deepcopy(receipt)
    altered.update(changes)
    altered.pop("signature")
    payload = json.dumps(altered, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    altered["signature"] = hmac.new(KEYS["old"], payload, hashlib.sha256).hexdigest()
    return altered


def _enable_reader(company):
    store = company["store"]
    approvals = []
    for source_id in company["sources"]:
        source = store.get_connection("demo", source_id)
        store.grant_source_access("demo", source_id, principal_type="user",
                                  principal_id=READER.subject, role="read")
        approvals.append(SourceApproval("demo", source_identity(source["kind"], source["config"]),
                                        frozenset({READER.subject}), frozenset()))
    policy = SourcePolicy(tuple(approvals))
    for replica in (store, company["peer"]):
        replica.source_policy = policy
        replica.access_enforced = True


def test_same_registry_replica_and_restarted_store_revalidate_shared_receipt(company):
    packet = _packet(company)
    receipt = packet["receipt"]
    assert packet["status"] == "context_available"
    assert packet["receipt_route"] == "shared_registry_replicas"
    assert receipt["schema_version"] == "context-receipt-v2"
    assert receipt["registry_id"] == "replica-test"
    assert receipt["key_id"] == "old"
    for replica in (company["store"], company["peer"]):
        checked = _check(company, receipt, replica)
        assert checked["status"] == "current"
        assert checked["source_permissions_verified"] is False
        assert checked["rules_enforced"] is False
        assert checked["execution_state"] == "not_executed"
    company["peer"].close()
    with _replica(company) as restarted:
        assert _check(company, receipt, restarted)["status"] == "current"


def test_default_local_receipt_stays_bound_to_its_store_after_recreation(monkeypatch):
    monkeypatch.delenv(CONFIG_NAME, raising=False)
    monkeypatch.delenv(CONFIG_NAME + "_FILE", raising=False)
    with enterprise_fixture() as fixture:
        source = fixture["bindings"]["finance"]["connection_id"]
        packet = task_context(fixture["store"], tenant_id="demo", query="Revenue",
                              connection_ids=[source])
        assert packet["receipt"]["schema_version"] == "context-receipt-v1"
        assert packet["receipt_route"] == "same_registry_process_required"
        assert revalidate_context(fixture["store"], tenant_id="demo",
                                  receipt=packet["receipt"])["status"] == "current"
        fixture["store"].close()
        for _ in range(2):
            with _replica(fixture) as restarted, pytest.raises(KnowledgeNotFound, match="was not found"):
                revalidate_context(restarted, tenant_id="demo", receipt=packet["receipt"])


def test_atomic_rotation_verifies_prior_key_and_renews_using_active_key(company):
    old = _packet(company)["receipt"]
    _replace(company["key_file"], _ring(active="new", retained=("old", "new")))
    renewed = _check(company, old)["receipt"]
    assert renewed["key_id"] == "new"
    assert _packet(company)["receipt"]["key_id"] == "new"
    assert _check(company, renewed, company["store"])["status"] == "current"
    _replace(company["key_file"], _ring(active="new", retained=("new",)))
    for replica in (company["store"], company["peer"]):
        with pytest.raises(KnowledgeNotFound, match="was not found"):
            _check(company, old, replica)
        assert _check(company, renewed, replica)["status"] == "current"


@pytest.mark.parametrize("failure", ["deleted", "empty", "malformed", "public", "identity"])
def test_configured_key_failure_refuses_new_and_existing_receipts(company, failure):
    receipt = _packet(company)["receipt"]
    path = company["key_file"]
    if failure == "deleted":
        path.unlink()
    elif failure == "public":
        path.chmod(0o644)
    else:
        _replace(path, {"empty": "", "malformed": "{private-invalid-value",
                        "identity": _ring(registry="different-registry")}[failure])
    for operation in (lambda: _packet(company), lambda: _check(company, receipt)):
        with pytest.raises(KnowledgeConflict, match="configuration is unavailable") as error:
            operation()
        assert str(path) not in str(error.value)
        assert "private-invalid-value" not in str(error.value)


@pytest.mark.parametrize("foreign", [
    Principal("different-reader", READER.groups, READER.scopes),
    Principal(READER.subject, frozenset({"other-group"}), READER.scopes),
    Principal(READER.subject, READER.groups, frozenset({"other-scope"})),
])
def test_shared_signature_is_bound_to_full_current_principal(company, foreign):
    _enable_reader(company)
    with principal_scope(READER):
        receipt = _packet(company)["receipt"]
        assert _check(company, receipt)["status"] == "current"
    with principal_scope(foreign), pytest.raises(KnowledgeNotFound, match="was not found"):
        _check(company, receipt)


@pytest.mark.parametrize("dependency", ["finance", "document"])
def test_shared_receipt_refuses_after_current_source_grant_revocation(company, dependency):
    _enable_reader(company)
    with principal_scope(READER):
        receipt = _packet(company)["receipt"]
        assert _check(company, receipt)["status"] == "current"
    source_id = (company["document_connection_id"] if dependency == "document"
                 else company["bindings"]["finance"]["connection_id"])
    store = company["store"]
    store.access_enforced = False
    try:
        store.revoke_source_access("demo", source_id, principal_type="user",
                                   principal_id=READER.subject)
    finally:
        store.access_enforced = True
    with principal_scope(READER), pytest.raises(KnowledgeNotFound):
        _check(company, receipt)


def test_shared_receipt_refuses_after_operator_source_policy_revocation(company):
    _enable_reader(company)
    with principal_scope(READER):
        receipt = _packet(company)["receipt"]
        company["peer"].source_policy = SourcePolicy(())
        with pytest.raises(KnowledgeConflict, match="changed"):
            _check(company, receipt)


def test_restarted_foreign_registry_refuses_receipt_even_with_same_key_material(company):
    receipt = _packet(company)["receipt"]
    _replace(company["key_file"], _ring(registry="other-deployment"))
    with _replica(company) as foreign, pytest.raises(KnowledgeNotFound, match="was not found"):
        _check(company, receipt, foreign)


def test_unselected_guidance_dependency_revocation_blocks_receipt_without_retained_meaning(company):
    _enable_reader(company)
    selected = {**company, "sources": [company["bindings"]["finance"]["connection_id"]]}
    with principal_scope(READER):
        packet = _packet(selected)
        assert packet["guidance"]["status"] == "ready"
        assert len(packet["guidance"]["bundles"]) == 1
        assert _check(selected, packet["receipt"])["status"] == "current"
    store = company["store"]
    store.access_enforced = False
    try:
        store.revoke_source_access("demo", company["document_connection_id"], principal_type="user",
                                   principal_id=READER.subject)
    finally:
        store.access_enforced = True
    with principal_scope(READER):
        current = _packet(selected, company["peer"])
        assert current["status"] == "blocked"
        assert current["guidance"]["status"] == "blocked"
        assert current["guidance"]["bundles"] == []
        assert "Explain overlap before combining results" not in json.dumps(current)
        with pytest.raises(KnowledgeConflict, match="changed"):
            _check(selected, packet["receipt"])


@pytest.mark.parametrize("authority", ["metric-publication", "company-guidance"])
def test_shared_receipt_refuses_after_reviewed_authority_withdrawal(company, authority):
    receipt = _packet(company)["receipt"]
    assert _check(company, receipt)["status"] == "current"
    if authority == "metric-publication":
        company["store"].revoke_publication(
            "demo", company["bindings"]["finance"]["connection_id"],
            reason="The fixture owner withdrew this metric authority.")
    else:
        GuidanceRegistry(company["store"]).revoke(
            "demo", company["guidance"]["slug"], revision=company["guidance"]["revision"],
            reason="The fixture steward withdrew this company procedure.")
    with pytest.raises(KnowledgeConflict, match="changed"):
        _check(company, receipt)


@pytest.fixture
def frozen_clock(monkeypatch):
    from knowledge_layer import task_context as module

    now = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", Clock)
    return now


@pytest.mark.parametrize("invalid", [
    "future", "naive-issued", "naive-expires", "too-long", "zero", "negative",
    "missing-issued", "bad-expires",
])
def test_even_valid_shared_signatures_refuse_invalid_receipt_clocks(company, frozen_clock, invalid):
    now = frozen_clock
    changes = {
        "future": {"issued_at": (now + timedelta(seconds=1)).isoformat(),
                   "expires_at": (now + timedelta(seconds=301)).isoformat()},
        "naive-issued": {"issued_at": now.replace(tzinfo=None).isoformat()},
        "naive-expires": {"expires_at": (now + timedelta(seconds=300)).replace(tzinfo=None).isoformat()},
        "too-long": {"expires_at": (now + timedelta(seconds=301)).isoformat()},
        "zero": {"expires_at": now.isoformat()},
        "negative": {"expires_at": (now - timedelta(seconds=1)).isoformat()},
        "missing-issued": {"issued_at": None},
        "bad-expires": {"expires_at": "not-a-clock"},
    }[invalid]
    receipt = _signed(_packet(company)["receipt"], **changes)
    with pytest.raises(KnowledgeValidationError, match="Invalid context receipt"):
        _check(company, receipt)


def test_shared_receipt_expires_at_exact_deadline_and_renewal_gets_new_lifetime(company, frozen_clock):
    expired = _signed(_packet(company)["receipt"],
                      issued_at=(frozen_clock - timedelta(seconds=300)).isoformat(),
                      expires_at=frozen_clock.isoformat())
    with pytest.raises(KnowledgeConflict, match="expired"):
        _check(company, expired)
    near_expiry = _signed(_packet(company)["receipt"],
                          issued_at=(frozen_clock - timedelta(seconds=299)).isoformat(),
                          expires_at=(frozen_clock + timedelta(seconds=1)).isoformat())
    renewed = _check(company, near_expiry)["receipt"]
    assert datetime.fromisoformat(renewed["issued_at"]) == frozen_clock
    assert datetime.fromisoformat(renewed["expires_at"]) == frozen_clock + timedelta(seconds=300)


@pytest.mark.parametrize("changes", [
    {"schema_version": "context-receipt-v1"},
    {"schema_version": "context-receipt-v9"},
    {"registry_id": "foreign-registry"},
    {"key_id": "unknown-key"},
    {"tenant_id": "foreign-tenant"},
    {"unexpected": "field"},
])
def test_signed_foreign_or_ambiguous_receipt_envelopes_refuse(company, changes):
    with pytest.raises(KnowledgeNotFound, match="was not found"):
        _check(company, _signed(_packet(company)["receipt"], **changes))


def test_mutating_signed_request_without_resigning_refuses(company):
    receipt = copy.deepcopy(_packet(company)["receipt"])
    receipt["request"]["query"] = "Unreviewed invented metric"
    with pytest.raises(KnowledgeNotFound, match="was not found"):
        _check(company, receipt)


@pytest.mark.parametrize("bad_request", [None, [], {"query": "Revenue"},
                                       {"query": "Revenue", "connection_ids": [],
                                        "business_unit": None, "task": None, "extra": True}])
def test_signed_malformed_requests_refuse_before_context_use(company, bad_request):
    with pytest.raises(KnowledgeValidationError):
        _check(company, _signed(_packet(company)["receipt"], request=bad_request))


def test_revalidation_captures_one_ring_and_next_request_observes_rotation(company, monkeypatch):
    receipt = _packet(company)["receipt"]
    provider = company["peer"]._context_receipt_keys
    real_current = provider.current
    calls = []

    def rotate_after_capture():
        captured = real_current()
        calls.append(captured.active_key_id)
        _replace(company["key_file"], _ring(active="new", retained=("old", "new")))
        return captured

    monkeypatch.setattr(provider, "current", rotate_after_capture)
    renewed = _check(company, receipt)["receipt"]
    assert calls == ["old"]
    assert renewed["key_id"] == "old"
    assert _check(company, renewed)["receipt"]["key_id"] == "new"
    assert calls == ["old", "new"]


def test_issuance_uses_captured_ring_but_next_request_refuses_invalid_rotation(company, monkeypatch):
    provider = company["store"]._context_receipt_keys
    real_current = provider.current
    calls = []

    def invalidate_after_capture():
        captured = real_current()
        calls.append(captured.active_key_id)
        _replace(company["key_file"], "invalid replacement")
        return captured

    monkeypatch.setattr(provider, "current", invalidate_after_capture)
    receipt = _packet(company)["receipt"]
    assert calls == ["old"]
    assert receipt["key_id"] == "old"
    with pytest.raises(KnowledgeConflict, match="configuration is unavailable"):
        _packet(company)
    with pytest.raises(KnowledgeConflict, match="configuration is unavailable"):
        _check(company, receipt)
