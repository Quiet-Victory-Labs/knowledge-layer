"""Page one explicitly approved adapter scope into the existing inventory registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update

from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_adapter import _profile
from knowledge_layer.connectors.external_inventory import (
    INVENTORY_PROTOCOL,
    MAX_SCAN_SECONDS,
    assert_inventory_deadline,
    fetch_inventory_page,
    make_request,
)
from knowledge_layer.inventory import InventoryStore, inventory_digest
from knowledge_layer.refresh_fence import (
    RefreshFenceLost,
    assert_refresh_fence,
    current_refresh_claim,
)
from knowledge_layer.source_policy import SourcePolicy, source_identity
from knowledge_layer.store import CurationFailed, KnowledgeAccessDenied, KnowledgeConflict, utc_now

DEFAULT_INVENTORY_LIMITS = {"max_pages": 100, "max_entities": 10_000,
                            "max_evidence": 20_000, "max_total_bytes": 16 * 1024 * 1024}


def inventory_profile_digest(profile):
    """Hash public target/scope controls, excluding bearer credentials."""
    pin = {key: profile[key] for key in ("endpoint", "provider", "protocol_version", "revision", "source_identity")}
    pin["allowed_scopes"] = sorted(profile["allowed_scopes"])
    if "inventory_scope_bindings" in profile:
        pin["inventory_scope_bindings"] = profile["inventory_scope_bindings"]
    return inventory_digest(pin)


def adapter_profile(config):
    profile = _profile(config["secret_ref"], config["scope"])
    if profile.get("protocol_version") != INVENTORY_PROTOCOL:
        raise ConnectorError("adapter_inventory_mode_required", "Inventory mode requires a paged profile")
    return profile, inventory_profile_digest(profile)


def require_inventory_requester(store, conn, tenant, source, subject):
    """Persisted job ownership needs a direct grant and explicit subject approval."""
    if subject == "operator":
        if store.source_policy is not None or store.access_enforced:
            raise KnowledgeAccessDenied("Inventory operator jobs require the trusted demo boundary")
        return
    grants = store.source_grants
    direct = conn.execute(select(grants.c.principal_id).where(
        grants.c.tenant_id == tenant, grants.c.connection_id == source["id"],
        grants.c.principal_type == "user", grants.c.principal_id == subject,
        grants.c.role == "manage").limit(1)).first()
    identity = source_identity(source["kind"], source["config"])
    approved = (store.source_policy is not None and any(
        item.tenant_id == tenant and item.identity == identity and subject in item.subjects
        for item in store.source_policy.approvals))
    if direct is None or not approved:
        raise KnowledgeAccessDenied("Inventory requester authorization is no longer current")


def assert_inventory_execution(store, conn, tenant, source):
    """Direct requester grants and operator subjects, without invented IdP groups."""
    loader = store.source_policy_loader
    if loader is not None:
        try:
            policy = loader()
            if not isinstance(policy, SourcePolicy):
                raise TypeError
            store.source_policy = policy
        except Exception:  # noqa: BLE001 - fixed refusal, never credential-bearing policy diagnostics
            raise KnowledgeAccessDenied("Current inventory source policy is unavailable") from None
    store._require_source_approval(tenant, source["kind"], source["config"])
    claim = current_refresh_claim()
    if claim is None:
        return
    assert_refresh_fence(store, conn, tenant, source["id"])
    job = conn.execute(select(store.refresh_jobs).where(
        store.refresh_jobs.c.id == claim["job_id"], store.refresh_jobs.c.tenant_id == tenant,
        store.refresh_jobs.c.connection_id == source["id"],
    ).with_for_update()).mappings().one()
    subject = job["requested_by"]
    require_inventory_requester(store, conn, tenant, source, subject)
    if job["schedule_id"] is not None:
        schedules = store.refresh_schedules
        schedule = conn.execute(select(schedules).where(
            schedules.c.id == job["schedule_id"], schedules.c.tenant_id == tenant,
            schedules.c.connection_id == source["id"],
        ).with_for_update()).mappings().first()
        if schedule is None or schedule["paused"] or schedule["owner_subject"] != subject:
            raise KnowledgeAccessDenied("Inventory refresh schedule is no longer current")


def _boundary(inventory, tenant, source_id, manifest=None, *, limits=None):
    with inventory._write_transaction() as conn:
        if manifest is None:
            source, _, _ = inventory._source(conn, tenant, source_id, manage=True)
            inventory._fence(conn, tenant, source_id, current_refresh_claim())
        else:
            row = inventory._run(conn, tenant, source_id, manifest["id"], manage=True,
                                 claim=current_refresh_claim())
            if row["status"] != "staging" or row["page_count"] != manifest["page_count"]:
                raise KnowledgeConflict("Inventory staging session changed")
            source, _, _ = inventory._source(conn, tenant, source_id, manage=True)
        if limits is not None and source["config"].get("inventory_limits") != limits:
            raise KnowledgeConflict("Inventory scan budgets changed")
        return source


def _abort_failed_unclaimed(inventory, tenant, source_id, manifest):
    """Retire only this local failed generation, even after its approval is withdrawn."""
    store = inventory.store
    with inventory._write_transaction() as conn:
        conn.execute(select(store.connections.c.id).where(
            store.connections.c.tenant_id == tenant, store.connections.c.id == source_id,
        ).with_for_update()).one()
        row = conn.execute(select(inventory.runs).where(
            inventory.runs.c.id == manifest["id"], inventory.runs.c.tenant_id == tenant,
            inventory.runs.c.connection_id == source_id,
        ).with_for_update()).mappings().one()
        if (row["claim"] is not None or row["status"] != "staging"
                or row["owner_digest"] != inventory._owner()[0]
                or any(row[key] != manifest[key] for key in
                       ("manifest_digest", "page_count", "scope_digest", "policy_digest"))):
            raise KnowledgeConflict("Failed local inventory generation changed")
        if (conn.execute(select(inventory.heads.c.inventory_id).where(
                inventory.heads.c.inventory_id == manifest["id"]).limit(1)).first() is not None
                or conn.execute(select(inventory.runs.c.id).where(
                    inventory.runs.c.previous_inventory_id == manifest["id"]).limit(1)).first() is not None
                or conn.execute(select(store.refresh_jobs.c.id).where(
                    store.refresh_jobs.c.result_inventory_id == manifest["id"]).limit(1)).first() is not None):
            raise KnowledgeConflict("Failed local inventory generation is referenced")
        conn.execute(update(inventory.runs).where(inventory.runs.c.id == manifest["id"]).values(status="aborted"))
        store._audit(conn, tenant, source_id, "inventory.failed_local_attempt_retired", "service",
                     {"inventory_id": manifest["id"]})


def refresh_inventory(store, tenant, source_id):
    """No legacy snapshot, model, source SQL or semantic publication is produced."""
    inventory = InventoryStore(store)
    source = _boundary(inventory, tenant, source_id)
    config = source["config"]
    started = datetime.now(UTC)
    start_clock = started.isoformat().replace("+00:00", "Z")
    expires_clock = (started + timedelta(seconds=MAX_SCAN_SECONDS)).isoformat().replace("+00:00", "Z")
    scan_id, manifest, request = uuid4().hex, None, None
    with inventory._write_transaction() as conn:
        inventory._source(conn, tenant, source_id, manage=True)
        inventory._fence(conn, tenant, source_id, current_refresh_claim())
        conn.execute(update(store.connections).where(store.connections.c.tenant_id == tenant,
                     store.connections.c.id == source_id).values(last_refresh_attempt_at=utc_now()))
    try:
        profile, profile_pin = adapter_profile(config)
        watermark_digest, cursor, index = None, None, 0
        while True:
            _boundary(inventory, tenant, source_id, manifest, limits=config["inventory_limits"])
            profile, observed_pin = adapter_profile(config)
            if observed_pin != profile_pin:
                raise KnowledgeConflict("Inventory adapter profile changed")
            request = make_request(provider=profile["provider"], scope=config["scope"],
                profile_digest=profile_pin, scan_id=scan_id, page_index=index, cursor=cursor,
                scan_started_at=start_clock, scan_expires_at=expires_clock,
                inventory_limits=config["inventory_limits"], expected_watermark_digest=watermark_digest)
            if store.source_policy is not None or store.access_enforced:
                from knowledge_layer.connector_isolation import isolated_inventory_page

                remaining = (datetime.fromisoformat(expires_clock)
                             - datetime.now(UTC)).total_seconds()
                page = isolated_inventory_page(config, request, timeout_seconds=remaining)
            else:
                page = fetch_inventory_page(profile, request)
            _boundary(inventory, tenant, source_id, manifest, limits=config["inventory_limits"])
            if adapter_profile(config)[1] != profile_pin:
                raise KnowledgeConflict("Inventory adapter profile changed")
            if manifest is None:
                watermark_digest = inventory_digest(page["watermark"])
                manifest = inventory.begin(tenant, source_id,
                    scope={"namespace": "external_adapter", "selectors": ["provider:" + profile["provider"], "scope:" + config["scope"]]},
                    policy={"id": "external-adapter.inventory", "revision": profile_pin},
                    watermark=page["watermark"], limits=config["inventory_limits"])
            manifest = inventory.append(tenant, source_id, manifest["id"], page_index=index,
                cursor=cursor, next_cursor=page["next_cursor"], entities=page["entities"], evidence=page["evidence"],
                **{key: manifest[key] for key in ("scope_digest", "policy_digest", "watermark_digest")})
            if page["listing_complete"]:
                break
            cursor, index = page["next_cursor"], index + 1
        _boundary(inventory, tenant, source_id, manifest, limits=config["inventory_limits"])
        assert_inventory_deadline(request)
        if adapter_profile(config)[1] != profile_pin:
            raise KnowledgeConflict("Inventory adapter profile changed")
        manifest = inventory.commit(tenant, source_id, manifest["id"], source_refresh=True, completion={
            "listing_complete": True, "pages": manifest["page_count"], "entities": manifest["entity_count"],
            "evidence": manifest["evidence_count"], "manifest_digest": manifest["manifest_digest"]})
        response = store._refresh_response(tenant, source_id)
        return {"connection": response["connection"], "snapshot": None,
                "inventory": None if response["connection"].get("metadata_unavailable") else manifest}
    except RefreshFenceLost:
        raise
    except Exception as exc:
        if manifest is not None:
            try:
                if current_refresh_claim() is None:
                    _abort_failed_unclaimed(inventory, tenant, source_id, manifest)
                else:
                    inventory.abort(tenant, source_id, manifest["id"])
            except Exception:  # noqa: BLE001, S110 - retired claims cannot mutate staging or log source details
                # A retired claim cannot mutate staging. Parent queue reconciliation
                # aborts only that fenced-out attempt after cancellation/takeover.
                pass
        store._mark_error(tenant, source_id)
        if isinstance(exc, (KnowledgeAccessDenied, KnowledgeConflict)):
            raise
        raise CurationFailed("Paged inventory discovery failed; source details are not stored") from None
