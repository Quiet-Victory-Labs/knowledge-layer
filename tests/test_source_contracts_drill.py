"""The pilot must fail missing observations and unsupported authority claims."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from adapter_response_fixture import streamed_response

from knowledge_layer.curation import curate_source

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/drill_source_contracts.py"
spec = importlib.util.spec_from_file_location("source_contracts_drill", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _observations():
    receipt = {"scope": "accounts", "observed_at": "2026-10-02T12:00:00Z", "assets": [
        {"name": "CRM Orders", "pointer": "crm/orders", "schema": [
            {"name": "status", "data_type": "text", "nullable": False}]}]}
    snapshot = {"status": "discovery_complete", "discovery_complete": True,
                "meaning_certified": False, "agent_ready": False, "assets": [
                    {"id": "observed-asset", "name": "CRM Orders",
                     "source_pointer": "adapter/example/accounts/crm/orders",
                     "schema": [{"name": "status", "data_type": "text", "native_type": "text", "nullable": False}],
                     "profile": {"metadata_only": True, "row_count": None, "content_retained": False}}],
                "evidence": [{"id": "observed-evidence", "asset_id": "observed-asset",
                              "evidence_type": "schema_and_profile", "details": {"field": "status"}}],
                "proposals": [{"id": "draft", "status": "candidate", "evidence_ids": ["observed-evidence"]}]}
    snapshot["assets"][0]["revision"] = module.stable_id(
        "adapter_revision_schema", None, snapshot["assets"][0]["schema"], {})
    return snapshot, receipt


def _grade(snapshot, receipt):
    return {row["name"]: row["passed"] for row in module.grade_discovery(
        snapshot, receipt, scope="accounts")}


def test_exact_discovery_observation_passes_without_semantic_claim():
    snapshot, receipt = _observations()
    assert all(_grade(snapshot, receipt).values())


def test_question_probes_use_matching_synthetic_queries_and_fields():
    receipt = {"assets": [{"schema": [{"name": name} for name in (
        "status", "review_status", "sync_status", "forecast_revenue",
        "account_id", "crm_id", "canonical_id",
    )]}]}
    requests = []

    def respond(request):
        requests.append((request.url.path, request.url.params["q"]))
        return streamed_response(200, json={"matches": [], "total_matches": 0})

    with httpx.Client(base_url="http://127.0.0.1:4173", transport=httpx.MockTransport(respond)) as client:
        reports = module._question_probes(client, "fixture-source", receipt)

    assert [report["query"] for report in reports] == ["review approval", "forecast", "account identity"]
    assert [report["observed_relevant_fields"] for report in reports] == [
        ["review_status", "status", "sync_status"],
        ["forecast_revenue"],
        ["account_id", "canonical_id", "crm_id"],
    ]
    assert requests == [
        (path, query)
        for query in ("review approval", "forecast", "account identity")
        for path in ("/agent/api/sources/fixture-source/evidence/search", "/agent/api/document-evidence/search")
    ]
    assert all(report["assessment"] == "retrieval_measured_semantic_answer_not_evaluated"
               for report in reports)


def test_current_refresh_cannot_pass_using_old_healthy_snapshot_and_receipt():
    connection = {"status": "discovery_complete", "last_error": None,
                  "last_refreshed_at": "2026-10-02T12:00:01Z"}
    receipt = {"observed_at": "2026-10-02T12:00:00Z"}
    grade = {row["name"]: row["passed"] for row in module.grade_refresh(
        connection, receipt, "2026-10-02T12:10:00Z")}
    assert grade["current_refresh_succeeded"]
    assert not grade["receipt_and_snapshot_refreshed_this_run"]
    connection.update(status="error", last_error={"code": "adapter_unavailable"})
    assert not module.grade_refresh(connection, receipt, "2026-10-02T12:10:00Z")[0]["passed"]


def test_refresh_requires_observed_timestamps_and_native_current_state():
    connection = {"status": "discovery_complete", "last_refreshed_at": "2026-10-02T12:00:03Z"}
    assert all(row["passed"] for row in module.grade_refresh(
        connection, {"observed_at": "2026-10-02T12:00:02Z"}, "2026-10-02T12:00:01Z"))
    assert not module.grade_refresh(connection, {}, "2026-10-02T12:00:01Z")[1]["passed"]
    assert all(row["passed"] for row in module.grade_connection_refresh(connection, "2026-10-02T12:00:01Z"))
    connection["last_error"] = {"code": "catalog_unavailable"}
    assert not module.grade_connection_refresh(connection, "2026-10-02T12:00:01Z")[0]["passed"]


def test_observed_unknown_nullable_and_native_type_only_are_valid_states():
    snapshot, receipt = _observations()
    source_field = receipt["assets"][0]["schema"][0]
    source_field["native_type"] = source_field.pop("data_type")
    source_field.pop("nullable")
    snapshot["assets"][0]["schema"][0]["nullable"] = None
    snapshot["assets"][0]["revision"] = module.stable_id(
        "adapter_revision_schema", None, snapshot["assets"][0]["schema"], {})
    assert all(_grade(snapshot, receipt).values())


def test_native_grade_checks_observed_identity_not_only_generic_schema():
    fields = [{"name": "sensor_id", "data_type": "long", "field_id": 1, "nullable": False}]
    facts = {"table_uuid": "observed-uuid", "schema_id": 2, "snapshot_id": 4,
             "row_count": 0, "row_count_state": "snapshot_summary"}
    receipt = {"assets": [{"pointer": "silver.readings", "schema": fields}],
               "source_details": {"tables": {"silver.readings": facts}}}
    snapshot = {"discovery_complete": True, "curation_complete": True, "meaning_certified": False,
                "agent_ready": False, "assets": [{"source_pointer": "local.silver.readings",
                "schema": [{**fields[0], "native_type": "long", "data_type": "integer"}],
                "profile": {**facts, "row_count_basis": "snapshot_summary", "metadata_only": True, "profile_rows": 0}}]}
    grade = lambda: module.grade_native_iceberg(snapshot, receipt, catalog="local", namespace="silver", tables=["readings"])
    assert all(row["passed"] for row in grade())
    snapshot["assets"].append(deepcopy(snapshot["assets"][0]))
    assert not next(row for row in grade() if row["name"] == "native_exact_selected_assets")["passed"]
    snapshot["assets"].pop()
    snapshot["assets"][0]["profile"]["table_uuid"] = "replacement-uuid"
    assert not grade()[-1]["passed"]


def test_missing_scope_and_empty_discovery_do_not_pass():
    snapshot, receipt = _observations()
    receipt["scope"] = "denied-scope"
    snapshot["assets"] = []
    grade = _grade(snapshot, receipt)
    assert not grade["receipt_scope_matches"]
    assert not grade["nonempty_observed_scope"]
    assert not grade["exact_asset_set"]


def test_missing_nullable_is_not_treated_as_true():
    snapshot, receipt = _observations()
    del receipt["assets"][0]["schema"][0]["nullable"]
    assert not _grade(snapshot, receipt)["exact_fields_nullable_and_normalized_types"]


def test_wrong_field_or_type_and_unresolved_evidence_fail():
    snapshot, receipt = _observations()
    snapshot["assets"][0]["schema"][0]["data_type"] = "integer"
    snapshot["proposals"][0]["evidence_ids"] = ["made-up"]
    grade = _grade(snapshot, receipt)
    assert not grade["exact_fields_nullable_and_normalized_types"]
    assert not grade["proposals_have_current_evidence"]


def test_inferred_relationship_cannot_self_declare_authority():
    snapshot, receipt = _observations()
    snapshot["relationships"] = [{"id": "name-join", "status": "approved"}]
    assert not _grade(snapshot, receipt)["no_unreviewed_authority"]


def test_invented_count_and_retained_rows_fail_metadata_claim():
    snapshot, receipt = _observations()
    snapshot["assets"][0]["profile"].update(row_count=0, content_retained=True)
    assert not _grade(snapshot, receipt)["metadata_only_no_row_counts_invented"]


def test_receipt_is_not_mutated_by_grading():
    snapshot, receipt = _observations()
    before = deepcopy(receipt)
    _grade(snapshot, receipt)
    assert receipt == before


def test_same_schema_with_wrong_revision_and_missing_evidence_fails():
    snapshot, receipt = _observations()
    snapshot["assets"][0]["revision"] = "wrong-observation"
    snapshot["evidence"] = []
    grade = _grade(snapshot, receipt)
    assert not grade["revision_bound_to_observed_schema"]
    assert not grade["nonempty_observed_evidence"]


def _sample_fixture(tmp_path):
    folder = tmp_path / "bounded"
    folder.mkdir()
    data = {
        "actuals.jsonl": [{"account_id": 1001, "reporting_year": 2025, "reporting_month": 1, "actual_revenue": 12.5},
                         {"account_id": 1002, "reporting_year": 2025, "reporting_month": 1, "actual_revenue": None}],
        "projected.jsonl": [{"account_id": 1001, "reporting_year": 2025, "reporting_month": 1, "forecast_revenue": 14.0},
                           {"account_id": 1001, "reporting_year": 2025, "reporting_month": 2, "forecast_revenue": 13.0}],
    }
    for name, rows in data.items():
        path = folder / name
        path.write_text("\n".join(json.dumps(row) for row in rows))
        path.chmod(0o600)
    return module._sample_observations(tmp_path, "bounded")


def test_independent_sample_aggregates_keep_raw_values_private(tmp_path):
    observations, overlaps = _sample_fixture(tmp_path)
    assert observations["bounded/actuals.jsonl"]["null_counts"]["actual_revenue"] == 1
    assert observations["bounded/projected.jsonl"]["distinct_counts"]["account_id"] == 1
    assert all(row["full_source_population_cardinality"] == "unknown" for row in overlaps)
    assert "1001" not in json.dumps([observations, overlaps])
    single = next(row for row in overlaps if row["source_pointer"].endswith("actuals.jsonl")
                  and row["key_fields"] == ["account_id"])
    composite = next(row for row in overlaps if row["source_pointer"].endswith("actuals.jsonl")
                     and len(row["key_fields"]) == 3)
    assert single["matched_source_rows"] == 1
    assert composite["matched_source_rows"] == 1


def test_missing_platform_join_probe_is_qualification_failure(tmp_path):
    observations, overlaps = _sample_fixture(tmp_path)
    snapshot = {"discovery_complete": True, "meaning_certified": False, "agent_ready": False,
                "assets": [], "metrics": []}
    for index, (pointer, observed) in enumerate(observations.items()):
        snapshot["assets"].append({"id": f"asset-{index}", "source_pointer": pointer,
                                   "revision": observed["sha256"],
                                   "schema": [{"name": field} for field in observed["fields"]],
                                   "profile": {"row_count": observed["rows"],
                                               "profile_rows": observed["rows"],
                                               "row_count_is_complete": True, "profile_is_sampled": False,
                                               "null_counts": observed["null_counts"],
                                               "missing_counts": observed["missing_counts"],
                                               "empty_counts": observed["empty_counts"],
                                               "distinct_counts": observed["distinct_counts"],
                                               "content_retained": False}})
        field = "actual_revenue" if "actual_revenue" in observed["fields"] else "forecast_revenue"
        snapshot["metrics"].append({"id": f"metric-{index}", "source_asset_id": f"asset-{index}",
                                    "field": field, "status": "candidate", "certified": False})
    grade = module.grade_samples(snapshot, observations, overlaps)
    assert grade["profiling_passed"]
    assert not grade["join_probe_qualification_passed"]
    snapshot["assets"][0]["profile"]["row_count"] = 2000
    assert not module.grade_samples(snapshot, observations, overlaps)["profiling_passed"]


def test_sparse_numeric_oracle_matches_real_profiles_and_bounded_probes(tmp_path):
    folder = tmp_path / "bounded"
    folder.mkdir()
    bodies = {
        "actuals.jsonl": (
            '{"account_id":0,"value":0}\n'
            '{"account_id":1,"value":null}\n'
            '{"account_id":1.0}\n'
            '{"account_id":1e0,"value":""}\n'
            '{"account_id":false,"value":" "}\n'
            '{"account_id":"1","value":{"n":1.0}}\n'
            '{"account_id":null,"value":{"n":1}}\n'
            '{"value":[1,1.0]}\n'
            '{"account_id":"","value":[1.0,1]}\n'
            '{"account_id":{},"value":1.0000000000000000000000000001}\n'
            '{"account_id":[],"value":1.0000000000000000000000000002}\n'
            '{"account_id":2e9999,"value":-0.0}\n'
        ),
        "projected.jsonl": (
            '{"account_id":0.0,"value":0.0}\n'
            '{"account_id":1.000,"value":1}\n'
            '{"account_id":true,"value":1.0}\n'
            '{"account_id":"1","value":1e0}\n'
            '{"account_id":20e9998,"value":1.0000}\n'
            '{"account_id":" ","value":null}\n'
            '{"account_id":[1]}\n'
            '{"account_id":3e9999,"value":1.0}\n'
        ),
    }
    for filename, body in bodies.items():
        path = folder / filename
        path.write_text(body)
        path.chmod(0o600)
    observations, overlaps = module._sample_observations(tmp_path, "bounded")
    actual = observations["bounded/actuals.jsonl"]
    assert actual["null_counts"]["value"] == 1
    assert actual["missing_counts"]["value"] == 1
    assert actual["empty_counts"]["value"] == 2
    assert actual["distinct_counts"]["value"] == 7
    assert actual["distinct_counts"]["account_id"] == 8
    forward = next(row for row in overlaps if row["source_pointer"] == "bounded/actuals.jsonl")
    reverse = next(row for row in overlaps if row["source_pointer"] == "bounded/projected.jsonl")
    assert forward["source_nonnull_rows_probed"] == 7
    assert forward["matched_source_rows"] == 6
    assert forward["target_nonnull_rows_probed"] == 6
    assert forward["target_distinct_values_probed"] == 6
    assert reverse["matched_source_rows"] == 4
    assert all(row["complete_source_and_target"] is False for row in overlaps)
    assert all(row["full_source_population_cardinality"] == "unknown" for row in overlaps)
    assert "1.0000000000000000000000000001" not in json.dumps([observations, overlaps])
    assert "2e9999" not in json.dumps([observations, overlaps])

    snapshot = curate_source("local_files", {"root": str(tmp_path), "path": "bounded",
                                            "include": "*.jsonl", "retain_semantic_samples": False})
    grade = module.grade_samples(snapshot, observations, overlaps)
    assert grade["profiling_passed"]
    assert grade["join_probe_qualification_passed"]
    snapshot["assets"][0]["profile"]["missing_counts"]["value"] += 1
    assert not module.grade_samples(snapshot, observations, overlaps)["profiling_passed"]


@pytest.mark.parametrize("body", (
    '{"account_id":NaN}', '{"account_id":Infinity}', '{"account_id":-Infinity}',
    '{"account_id":0,"account_id":1}', '{"account_id":{"nested":0,"nested":1}}',
))
def test_sample_oracle_rejects_nonfinite_and_duplicate_json(tmp_path, body):
    folder = tmp_path / "bounded"
    folder.mkdir()
    path = folder / "invalid.jsonl"
    path.write_text(body)
    path.chmod(0o600)
    with pytest.raises(ValueError):
        module._sample_observations(tmp_path, "bounded")


def test_policy_presence_does_not_turn_unrelated_identity_excerpt_into_support(tmp_path):
    folder = tmp_path / "policies"
    folder.mkdir()
    for name, content in {
        "FORECAST_POLICY.md": "Vendor Forecast projections remain controls.",
        "IDENTITY_POLICY.md": "account_resolver keeps CRM and provider identities distinct.",
        "REVIEW_POLICY.md": "Review approval requires independent review.",
    }.items():
        (folder / name).write_text(content)
        (folder / name).chmod(0o600)

    def transport(request):
        query = request.url.params["q"]
        matches = []
        if query == "account identity":
            matches = [{"source_pointer": "policies/IDENTITY_POLICY.md",
                        "summary": "Probe the API with a Google identity token"}]
        return streamed_response(200, json={"matches": matches, "total_matches": len(matches)})

    with httpx.Client(base_url="http://127.0.0.1:4173", transport=httpx.MockTransport(transport)) as client:
        result = module._document_policy_probes(client, root=tmp_path, relative="policies")
    assert not result["passed"]
    for policy in result["policies"]:
        assert policy["checks"][0]["passed"]
        assert not policy["checks"][1]["passed"]
        assert not policy["checks"][2]["passed"]
        assert not policy["checks"][3]["passed"]


def test_policy_grade_requires_actual_excerpt_locator_and_revision(tmp_path):
    folder = tmp_path / "policies"
    folder.mkdir()
    passages = {
        "FORECAST_POLICY.md": "Forecast projections stay frozen controls.\n",
        "IDENTITY_POLICY.md": "account_resolver handles account identity; CRM IDs stay distinct.\n",
        "REVIEW_POLICY.md": "Sync and review approval\nrequire independent\nreview.\n"
                                             "SyntheticReviewGate.accept_definition owns this gate.\n",
    }
    for filename, body in passages.items():
        path = folder / filename
        path.write_text(body)
        path.chmod(0o600)
    observed = {"corrupt": False}

    def response(request):
        filename = {"Forecast": "FORECAST_POLICY.md", "account identity": "IDENTITY_POLICY.md",
                    "review approval": "REVIEW_POLICY.md"}[request.url.params["q"]]
        body = passages[filename].rstrip()
        match = {"source_pointer": "policies/" + filename, "excerpt": body,
                 "source_revision": module.hashlib.sha256(passages[filename].encode()).hexdigest(),
                 "source_locator": {"format": "md", "coordinate_basis": "source_lines",
                                    "line_start": 1, "line_end": len(body.splitlines()),
                                    "column_start": 1, "column_end": len(body.splitlines()[-1])}}
        if observed["corrupt"]:
            match["source_revision"] = "wrong-revision"
        return streamed_response(200, json={"matches": [match]})

    with httpx.Client(base_url="http://127.0.0.1:4173", transport=httpx.MockTransport(response)) as client:
        assert module._document_policy_probes(client, root=tmp_path, relative="policies")["passed"]
        observed["corrupt"] = True
        result = module._document_policy_probes(client, root=tmp_path, relative="policies")
        assert not result["passed"]
        assert all(not item["checks"][3]["passed"] for item in result["policies"])


def test_private_inputs_and_report_refuse_symlinks_without_changing_target(tmp_path):
    target = tmp_path / "private.json"
    target.write_text('{"before":true}')
    target.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(target)
    for operation in (lambda: module._private_json(link),
                      lambda: module._write_report(link, {"after": True})):
        try:
            operation()
        except (OSError, ValueError):
            pass
        else:
            raise AssertionError("Symlink operation should be refused")
    assert target.read_text() == '{"before":true}'


def test_private_input_permission_check_and_report_owner_only(tmp_path):
    target = tmp_path / "private.json"
    target.write_text('{"value":1}')
    target.chmod(0o644)
    try:
        module._private_json(target)
    except ValueError:
        pass
    else:
        raise AssertionError("Publicly readable input should be refused")
    module._write_report(target, {"replacement": True})
    assert target.stat().st_mode & 0o777 == 0o600
    assert module._private_json(target) == {"replacement": True}
