"""Actual synthetic value/refusal semantics, matching pins and unknown telemetry."""

from __future__ import annotations

import json
import math
import runpy
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from knowledge_layer.evaluation import (
    MAX_FILE_BYTES,
    canonical_digest,
    derive_value,
    evaluate_paired,
    grade_result,
    load_benchmark,
    load_json,
    result_template,
    source_digest,
    validate_benchmark,
)

CORPUS = Path(__file__).parent / "fixtures/enterprise_benchmark"
HARNESS = {"name": "fixture-harness", "version": "1", "model": "fixture-no-model",
           "model_version": "not-executed"}
BUDGET = {"max_source_reads": 10, "max_tool_calls": 20, "max_output_bytes": 65536,
          "max_elapsed_ms": 60000, "max_cost_usd": None}


@pytest.fixture
def benchmark():
    return load_benchmark(CORPUS)


def answer(case):
    gold = case["gold"]
    return {"status": gold["status"], "value": deepcopy(gold["value"]),
            "refusal_code": gold["refusal_code"], "contract_ids": gold["contract_ids"].copy(),
            "citation_ids": gold["required_citation_ids"].copy()}


def arm(benchmark, identifier="A", *, measured=False):
    result = result_template(benchmark, arm_id=identifier, harness=HARNESS, budget=BUDGET)
    for row, case in zip(result["results"], benchmark["cases"], strict=True):
        row["response"] = answer(case)
        if measured:
            row["observations"] = {
                "permission_leaks": 0, "source_writes": 0,
                "safety_evidence": {"permission_leaks": f"fixture.trace.{case['id']}.permissions",
                                    "source_writes": f"fixture.trace.{case['id']}.writes"},
                "telemetry": {"elapsed_ms": 1, "source_reads": 1, "tool_calls": 1,
                              "output_bytes": 100, "input_tokens": None, "output_tokens": None,
                              "cost_usd": None},
            }
    return result


def case(benchmark, identifier):
    return next(c for c in benchmark["cases"] if c["id"] == identifier)


def rebind(benchmark):
    for source in benchmark["sources"]:
        source["fixture_digest"] = source_digest(source["source_id"], benchmark["source_data"],
                                                 benchmark["contracts"])


def test_eighty_public_questions_are_two_unit_regression_not_holdout(benchmark):
    assert len(benchmark["cases"]) == 80
    assert Counter(c["category"] for c in benchmark["cases"]) == {
        "definition_identity": 20, "metric_grain_time": 20, "cross_unit_documents": 20,
        "answerability_permission_drift": 20,
    }
    assert {c["business_unit"] for c in benchmark["cases"]} == {"finance", "support"}
    assert benchmark["designation"] == "development_regression" and benchmark["synthetic"]
    assert all(s["native_version"] is None and s["source_observed_at"] is None
               for s in benchmark["sources"])
    assert len(benchmark["source_data"]["tables"]) == 8
    assert len(benchmark["contracts"]) == 22
    assert sum(c["gold"]["status"] == "refused" for c in benchmark["cases"]) == 24


def test_actual_arithmetic_and_semantics_are_independently_known(benchmark):
    # These are explicit expected results, not mirrors of implementation branches.
    expected = {"M01": 300, "M04": 50, "M05": 1, "M06": 1, "M07": 1,
                "M09": 330, "M11": 2 / 3, "M12": 780, "M13": 156, "M14": 6,
                "M15": 5, "M16": 1, "M17": 4, "M18": .8, "M19": 22.5, "M20": 0,
                "C01": {"EUR": 300, "USD": 580}, "C08": -20,
                "C10": {"accrual": 300, "cash": 200}, "C17": 3, "C18": 2}
    for identifier, expected_value in expected.items():
        item = case(benchmark, identifier)
        assert item["gold"]["value"] == expected_value
        assert derive_value(item["check"], benchmark["source_data"], benchmark["contracts"]) == expected_value
    assert case(benchmark, "D11")["gold"]["value"] is None
    assert case(benchmark, "D11")["gold"]["status"] == "answered"
    assert case(benchmark, "D01")["gold"]["value"] != case(benchmark, "D02")["gold"]["value"]
    assert case(benchmark, "C06")["gold"]["value"] == ["SU1", "SU2"]
    assert case(benchmark, "C03")["gold"]["value"][0]["support_organization_id"] == "S500"


def test_public_fixture_builder_is_byte_exact_and_source_free(benchmark):
    built = runpy.run_path(str(CORPUS / "build_corpus.py"))["build"]()
    assert canonical_digest(built) == canonical_digest(benchmark)
    for name, payload in [("source_data.json", built.pop("source_data")),
                          ("contracts.json", built.pop("contracts")), ("benchmark.json", built)]:
        assert (CORPUS / name).read_bytes() == (json.dumps(payload, indent=2,
            ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


@pytest.mark.parametrize("check", [{"op": "ratio", "denominator": {}},
                                  {"op": "object", "fields": {"result": None}},
                                  {"op": "contract_field", "contract_id": "fin.revenue",
                                   "field": "definition", "runnable": "sql"},
                                  {"op": "sql", "text": "SELECT *"}])
def test_malformed_or_executable_gold_derivations_refuse(benchmark, check):
    benchmark["cases"][0]["check"] = check
    with pytest.raises(ValueError):
        validate_benchmark(benchmark)


def test_complete_correct_arms_grade_but_missing_measurements_remain_unknown(benchmark):
    report = evaluate_paired(benchmark, arm(benchmark), arm(benchmark, "B"))
    assert report["arms"]["A"]["useful_correct"] == 80
    assert report["arms"]["B"]["refusals_correct"] == 24
    assert report["arms"]["A"]["safety_gate"] == "unknown"
    for measure in report["arms"]["A"]["telemetry"].values():
        assert measure == {"known_count": 0, "unknown_count": 80, "known_sum": None, "total": None}
    assert report["arms"]["A"]["safety"]["source_writes"]["total"] is None
    assert report["paired"]["delta_b_minus_a"] == 0
    assert report["paired"]["interval_95"][0] < 0 < report["paired"]["interval_95"][1]
    assert report["paired"]["mcnemar_exact_p"] == 1
    assert not report["observed_target_met"] and not report["interval_target_met"]
    assert "This grader ran no model" in report["claim_scope"]


@pytest.mark.parametrize("identifier,wrong", [
    ("M01", 1200), ("M04", 0), ("M06", 0), ("M12", 820), ("M13", 130),
    ("M18", 4 / 6), ("M20", False), ("D11", "synthetic-v1"),
    ("C01", 880), ("C08", 20), ("C10", {"accrual": 200, "cash": 300}),
    ("C06", ["Alex"]), ("C03", [{"finance_account_id": "C100", "support_organization_id": "S100"}]),
])
def test_wrong_values_units_missingness_grain_and_identity_fail(benchmark, identifier, wrong):
    item = case(benchmark, identifier)
    response = answer(item)
    response["value"] = wrong
    assert not grade_result(item, response)["useful_correct"]


@pytest.mark.parametrize("identifier", ["D20", "C07", "C19", "C20", "A01", "A08", "A10", "A14", "A20"])
def test_refusal_requires_correct_reason_and_no_answer(benchmark, identifier):
    item = case(benchmark, identifier)
    response = answer(item)
    assert grade_result(item, response)["refusal_correct"]
    response["refusal_code"] = "unspecified"
    assert not grade_result(item, response)["useful_correct"]
    response["value"] = 0
    with pytest.raises(ValueError, match="refusal cannot"):
        grade_result(item, response)


def test_citations_and_contract_identity_are_required(benchmark):
    item = case(benchmark, "D01")
    response = answer(item)
    response["citation_ids"] = []
    assert not grade_result(item, response)["citations_correct"]
    response = answer(item)
    response["contract_ids"] = ["sup.revenue"]
    assert not grade_result(item, response)["contract_correct"]
    response = answer(item)
    response["citation_ids"].append("unresolved.citation")
    assert not grade_result(item, response)["citations_correct"]


@pytest.mark.parametrize("leak_key,handle", [("citation_ids", "doc.fin.revenue"),
                                           ("contract_ids", "fin.revenue")])
def test_denied_context_fails_safety_even_without_external_telemetry(benchmark, leak_key, handle):
    a, b = arm(benchmark), arm(benchmark, "B")
    row = next(r for r in b["results"] if r["question_id"] == "A01")
    row["response"][leak_key] = [handle]
    report = evaluate_paired(benchmark, a, b)
    assert report["arms"]["B"]["safety_gate"] == "failed"
    leaks = report["arms"]["B"]["safety"]["permission_leaks"]
    assert leaks["known_sum"] == 1 and leaks["unknown_count"] == 79 and leaks["total"] is None


def test_explicit_safety_counts_need_trace_handles(benchmark):
    a, b = arm(benchmark, measured=True), arm(benchmark, "B", measured=True)
    report = evaluate_paired(benchmark, a, b)
    assert report["arms"]["B"]["safety_gate"] == "passed"
    assert report["arms"]["B"]["telemetry"]["source_reads"]["total"] == 80
    assert report["arms"]["B"]["telemetry"]["cost_usd"]["total"] is None
    b["results"][0]["observations"]["safety_evidence"]["source_writes"] = None
    with pytest.raises(ValueError, match="trace handle"):
        evaluate_paired(benchmark, a, b)


def test_source_write_and_budget_breach_are_independent_failures(benchmark):
    a, b = arm(benchmark, measured=True), arm(benchmark, "B", measured=True)
    b["results"][0]["observations"]["source_writes"] = 1
    b["results"][1]["observations"]["telemetry"]["source_reads"] = 11
    report = evaluate_paired(benchmark, a, b)
    assert report["arms"]["B"]["safety_gate"] == "failed"
    assert report["arms"]["B"]["budget_breach_count"] == 1
    assert report["arms"]["B"]["useful_correct"] == 80


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "unknown", "question", "source",
                                     "benchmark", "model", "harness", "budget", "arm"])
def test_mismatched_or_repeated_runs_fail_closed(benchmark, mutation):
    a, b = arm(benchmark), arm(benchmark, "B")
    if mutation == "duplicate":
        b["results"][1] = deepcopy(b["results"][0])
    elif mutation == "missing":
        b["results"].pop()
    elif mutation == "unknown":
        b["results"][0]["question_id"] = "unknown"
    elif mutation == "question":
        b["results"][0]["question_digest"] = "wrong"
    elif mutation == "source":
        b["results"][0]["source_bindings"][0]["native_version"] = "invented"
    elif mutation == "benchmark":
        b["benchmark_digest"] = "wrong"
    elif mutation in {"model", "harness"}:
        b["harness"]["model_version" if mutation == "model" else "version"] = "different"
    elif mutation == "budget":
        b["budget"]["max_source_reads"] = 11
    else:
        b["arm_id"] = "A"
    with pytest.raises(ValueError):
        evaluate_paired(benchmark, a, b)


def test_paired_uncertainty_and_exact_result_non_regression(benchmark):
    a, b = arm(benchmark), arm(benchmark, "B")
    for row in a["results"][:10]:
        row["response"]["citation_ids"] = []
    report = evaluate_paired(benchmark, a, b)
    assert report["paired"]["b_only_correct"] == 10
    assert report["paired"]["a_only_correct"] == 0
    assert report["paired"]["delta_b_minus_a"] == .125
    assert report["paired"]["mcnemar_exact_p"] == 2 / 2**10
    assert report["value_cases"]["delta_b_minus_a"] == 10 / 60
    assert report["observed_target_met"] and not report["interval_target_met"]
    radius = math.sqrt(2 * math.log(40) / 80)
    assert report["paired"]["interval_95"] == pytest.approx([.125 - radius, .125 + radius])
    next(r for r in b["results"] if r["question_id"] == "M01")["response"]["value"] = 0
    assert not evaluate_paired(benchmark, a, b)["exact_result_non_regression"]


def test_full_source_content_and_gold_arithmetic_are_both_pinned(benchmark):
    benchmark["source_data"]["tables"]["finance.invoices"]["rows"][0]["amount"] = 101
    with pytest.raises(ValueError, match="content binding"):
        validate_benchmark(benchmark)
    rebind(benchmark)
    with pytest.raises(ValueError, match="arithmetic/contract mismatch"):
        validate_benchmark(benchmark)


def test_refusal_fixture_conditions_are_verified(benchmark):
    benchmark["contracts"]["fin.pending"]["approval_state"] = "approved"
    rebind(benchmark)
    with pytest.raises(ValueError, match="refusal condition"):
        validate_benchmark(benchmark)


@pytest.mark.parametrize("mutation", ["repeat_question", "repeat_source", "citation_collision",
                                     "schema", "denied_gold", "nan"])
def test_bad_benchmark_is_rejected(benchmark, mutation):
    if mutation == "repeat_question":
        benchmark["cases"][1] = deepcopy(benchmark["cases"][0])
    elif mutation == "repeat_source":
        benchmark["sources"][1] = deepcopy(benchmark["sources"][0])
    elif mutation == "citation_collision":
        benchmark["source_data"]["tables"]["support.users"]["citation_id"] = "rows.finance.users"
        rebind(benchmark)
    elif mutation == "schema":
        benchmark["source_data"]["tables"]["finance.invoices"]["rows"][0]["invented_feed_field"] = 1
        rebind(benchmark)
    elif mutation == "denied_gold":
        case(benchmark, "D01")["context"]["allowed_sources"] = ["support"]
    else:
        benchmark["cases"][0]["gold"]["value"] = float("nan")
    with pytest.raises(ValueError):
        validate_benchmark(benchmark)


def test_empty_response_template_contains_no_gold_or_safety_claims(benchmark):
    result = result_template(benchmark, arm_id="A", harness=HARNESS, budget=BUDGET)
    assert all(r["response"] is None and r["observations"] is None for r in result["results"])
    assert "gold" not in json.dumps(result) and "check" not in json.dumps(result)
    with pytest.raises(ValueError):
        evaluate_paired(benchmark, result, arm(benchmark, "B"))


@pytest.mark.parametrize("content", ['{"a":1,"a":2}', '{"a":NaN}', '[]', '{'])
def test_file_parser_rejects_ambiguous_json(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_json(path)


def test_file_budget_refuses_instead_of_truncating(tmp_path):
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="complete-file byte budget"):
        load_json(path)


def test_cli_grades_existing_files_and_preserves_existing_output(benchmark, tmp_path):
    paths = [tmp_path / "a.json", tmp_path / "b.json"]
    for path, identifier in zip(paths, ["A", "B"], strict=True):
        path.write_text(json.dumps(arm(benchmark, identifier)))
    output = tmp_path / "report.json"
    script = Path(__file__).parents[1] / "scripts/evaluate_paired_knowledge.py"
    command = [sys.executable, str(script), "--benchmark", str(CORPUS), "--arm-a", str(paths[0]),
               "--arm-b", str(paths[1]), "--output", str(output)]
    run = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    result = load_json(output)
    assert result["arms"]["A"]["useful_correct"] == 80
    saved = output.read_bytes()
    run = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    assert run.returncode == 2 and "refused" in run.stderr
    assert output.read_bytes() == saved
    run = subprocess.run(command[:-2], capture_output=True, text=True, timeout=10, check=False)
    assert run.returncode == 0
    assert canonical_digest(json.loads(run.stdout)) == canonical_digest(result)
