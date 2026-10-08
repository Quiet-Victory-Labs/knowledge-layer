"""Capabilities are scoped evidence claims, never permission or health grants."""

from pathlib import Path

from knowledge_layer.capabilities import capability_manifest


def test_independent_version_axes_preserve_unversioned_contracts():
    versions = capability_manifest()["versions"]
    assert versions["api"]["contract_revision"] is None
    assert versions["semantic_contract"]["contract_revision"] is None
    assert versions["connector_protocol"]["supported"] == ["1.0", "1.1", "1.2", "1.3"]
    assert versions["connector_protocol"]["default"] == "1.0"
    assert versions["connector_protocol"]["complete_response_versions"] == ["1.0", "1.1", "1.2"]
    assert versions["connector_protocol"]["paged_inventory_version"] == "1.3"
    assert versions["mcp"]["observed_sdk_version"] == "2.2.0"
    assert "2026-07-28" in versions["mcp"]["sdk_supported_protocols"]
    assert versions["mcp"]["qualified_wire_versions"] == ["2025-11-25", "2026-07-28"]
    assert "no customer IdP" in versions["mcp"]["qualification_note"]
    assert versions["interchange"]["ossie"]["status"] == "draft_preview"
    assert versions["interchange"]["agent_skills"]["enforcement"] == "guidance_only"
    receipts = versions["task_context"]
    assert receipts["supported_receipt_versions"] == ["context-receipt-v1", "context-receipt-v2"]
    assert receipts["shared_keys"] == "operator_opt_in" and receipts["max_receipt_seconds"] == 300
    assert receipts["receipt_route"] == "same_registry_process_required"
    assert receipts["shared_receipt_route"] == "shared_registry_replicas"


def test_all_claims_have_scoped_dated_local_evidence_and_unknown_deployment():
    manifest = capability_manifest()
    assert manifest["schema_version"] == "knowledge-capabilities-v1"
    assert manifest["assessment_date"] == "2026-10-04"
    assert manifest["evidence_baseline"]["clean_regression_tests"] == 737
    claims = manifest["capabilities"]
    assert len(claims) == len({c["id"] for c in claims}) == 41
    root = Path(__file__).parents[1]
    for claim in claims:
        assert claim["availability"] in {"supported", "experimental", "unavailable"}
        assert claim["scope"] and claim["limits"]
        assert claim["verification"]["deployed"] is None
        assert type(claim["verification"]["offline_tested"]) is bool
        assert type(claim["verification"]["live_qualified"]) is bool
        assert claim["evidence"] and all((root / e["reference"]).is_file() for e in claim["evidence"])
        assert all(e["date"] == claim["verification"]["assessment_date"] for e in claim["evidence"])
        assert claim["verification"]["assessment_date"] <= manifest["assessment_date"]


def test_offline_live_availability_do_not_collapse_into_release_readiness():
    claims = {c["id"]: c for c in capability_manifest()["capabilities"]}
    assert claims["connectors.postgresql"]["availability"] == "supported"
    assert claims["connectors.postgresql"]["verification"]["offline_tested"]
    assert not claims["connectors.postgresql"]["verification"]["live_qualified"]
    assert claims["interfaces.native_skill_activation"]["availability"] == "unavailable"
    assert claims["interfaces.native_skill_activation"]["verification"]["offline_tested"]
    native = claims["interfaces.native_cli_consumption"]
    assert native["availability"] == "experimental"
    assert native["verification"]["live_qualified"] is True
    assert "synthetic" in native["limits"] and "Claude end-to-end" in native["limits"]
    assert claims["evaluation.paired_model_uplift"]["availability"] == "unavailable"
    assert claims["deployment.enterprise_cloud"]["availability"] == "unavailable"
    conformance = claims["connectors.adapter_conformance"]
    assert conformance["availability"] == "supported"
    assert conformance["verification"]["offline_tested"]
    assert not conformance["verification"]["live_qualified"]
    paged = claims["connectors.paged_external_adapter"]
    assert paged["availability"] == "supported" and paged["verification"]["offline_tested"]
    assert not paged["verification"]["live_qualified"]
    assert "protocol 1.3" in paged["scope"] and "explicit payload retention" in paged["limits"]
    assert "native cursor ownership/ACLs" in paged["limits"] and "semantic publication" in paged["limits"]
    retention = claims["refresh.aborted_inventory_retention"]
    assert retention["availability"] == "supported"
    assert retention["verification"]["offline_tested"] and retention["verification"]["live_qualified"]
    assert "Disposable PostgreSQL" in retention["limits"] and "already-aborted" in retention["limits"]
    assert capability_manifest()["versions"]["inventory_retention"]["exposure"] == "offline_operator_only"
    shacl = claims["interchange.shacl"]
    assert shacl["verification"]["offline_tested"] and not shacl["verification"]["live_qualified"]
    assert shacl["verification"]["assessment_date"] == "2026-10-04"
    versions = capability_manifest()["versions"]["interchange"]
    assert versions["rdf"]["runtime_shacl"] is False
    assert versions["shacl"]["exposure"] == "offline_optional"
    assert versions["shacl"]["inference"] == "none"
    for identifier in ["semantics.cross_source_join_planning", "semantics.typed_metric_definitions",
                       "semantics.metric_planning", "refresh.multi_worker_fencing",
                       "interfaces.task_context_receipts", "connectors.datahub_glossary",
                       "connectors.dbt_manifest"]:
        assert claims[identifier]["availability"] == "supported"
        assert claims[identifier]["verification"]["offline_tested"]
        assert not claims[identifier]["verification"]["live_qualified"]


def test_callers_cannot_mutate_registry():
    manifest = capability_manifest()
    manifest["capabilities"][0]["verification"]["deployed"] = True
    manifest["versions"]["connector_protocol"]["supported"].append("invented")
    manifest["evidence_baseline"]["clean_regression_tests"] = 0
    fresh = capability_manifest()
    assert fresh["capabilities"][0]["verification"]["deployed"] is None
    assert fresh["versions"]["connector_protocol"]["supported"] == ["1.0", "1.1", "1.2", "1.3"]
    assert fresh["evidence_baseline"]["clean_regression_tests"] == 737
