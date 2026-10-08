"""Offline paired grading of pinned enterprise questions and anonymous arm files.

No source, model or harness is executed here. Gold contracts are benchmark labels,
not Knowledge Layer approvals. Safety observations require external trace handles;
those supplied observations are not independently audited by this grader.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

BENCHMARK_VERSION = "knowledge-enterprise-benchmark-v1"
RESULT_VERSION = "knowledge-paired-results-v1"
REPORT_VERSION = "knowledge-paired-report-v1"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_CASES = 1000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}\Z")
_CATEGORIES = {"definition_identity", "metric_grain_time", "cross_unit_documents",
               "answerability_permission_drift"}
_TELEMETRY = {"elapsed_ms", "source_reads", "tool_calls", "output_bytes", "input_tokens",
              "output_tokens", "cost_usd"}
_BUDGET = {"max_source_reads", "max_tool_calls", "max_output_bytes", "max_elapsed_ms",
           "max_cost_usd"}


def canonical_digest(value: Any) -> str:
    """SHA-256 of full canonical UTF-8 JSON, without NaN or lossy omission."""
    try:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("Evaluation content must be finite JSON") from exc
    return hashlib.sha256(body).hexdigest()


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} requires exactly {sorted(keys)}")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} is not a bounded identifier")
    return value


def _identifiers(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError(f"{label} must be a bounded identifier list")
    checked = [_identifier(item, label) for item in value]
    if len(set(checked)) != len(checked):
        raise ValueError(f"{label} contains duplicate identifiers")
    return checked


def _number(value: Any, label: str, *, integer: bool = False) -> int | float:
    if (type(value) not in ({int} if integer else {int, float})
            or value < 0 or value > 10**15 or not math.isfinite(value)):
        raise ValueError(f"{label} must be a finite bounded nonnegative number")
    return value


def _equal(left: Any, right: Any) -> bool:
    if type(left) in {int, float} and type(right) in {int, float}:
        return Decimal(str(left)) == Decimal(str(right))
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON members are not allowed")
        result[key] = value
    return result


def load_json(path: str | Path) -> dict[str, Any]:
    """Read bounded local JSON, rejecting duplicate members and nonfinite literals."""
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("Evaluation file exceeds the complete-file byte budget")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               ValueError("Nonfinite JSON literals are not allowed")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Evaluation file must contain complete UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Evaluation file must contain an object")  # noqa: TRY004 -- invalid JSON schema
    return value


def load_benchmark(directory: str | Path) -> dict[str, Any]:
    """Load the three fixed corpus files; no paths or source URLs come from JSON."""
    root = Path(directory)
    manifest = load_json(root / "benchmark.json")
    manifest["source_data"] = load_json(root / "source_data.json")
    manifest["contracts"] = load_json(root / "contracts.json")
    return validate_benchmark(manifest)


def source_digest(source_id: str, data: dict[str, Any], contracts: dict[str, Any]) -> str:
    return canonical_digest({
        "tables": {k: v for k, v in data["tables"].items() if v["source_id"] == source_id},
        "documents": {k: v for k, v in data["documents"].items() if v["source_id"] == source_id},
        "contracts": {k: v for k, v in contracts.items() if v["source_id"] == source_id},
    })


def _rows(check: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    table = data["tables"].get(check.get("table"))
    if table is None:
        raise ValueError("Gold derivation refers to an unknown table")
    rows = table["rows"]
    for condition in check.get("where", []):
        _object(condition, {"field", "op", "value"}, "Gold filter")
        field, op, value = condition["field"], condition["op"], condition["value"]
        if field not in table["columns"] or op not in {"eq", "ne", "in", "gte", "lt"}:
            raise ValueError("Unsupported gold filter")
        if op == "in" and not isinstance(value, list):
            raise ValueError("Gold in-filter needs a list")

        def matches(row: dict[str, Any], field: str = field, op: str = op,
                    value: Any = value) -> bool:
            item = row[field]
            if op == "eq":
                return _equal(item, value)
            if op == "ne":
                return not _equal(item, value)
            if op == "in":
                return any(_equal(item, candidate) for candidate in value)
            if item is None or value is None or type(item) is not type(value):
                return False
            return item >= value if op == "gte" else item < value

        rows = [row for row in rows if matches(row)]
    return rows


def _numeric(values: list[Any]) -> list[Decimal]:
    if any(type(item) not in {int, float} or not math.isfinite(item) for item in values):
        raise ValueError("Gold arithmetic requires measured finite numbers")
    return [Decimal(str(item)) for item in values]


def _json_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def derive_value(check: dict[str, Any], data: dict[str, Any],
                 contracts: dict[str, Any]) -> Any:
    """Bounded fixture oracle, never a formula/SQL executor or production compiler."""
    op = check.get("op")
    if op == "object":
        if not isinstance(check.get("fields"), dict) or len(check["fields"]) > 20:
            raise ValueError("Gold object requires bounded field derivations")
        return {key: derive_value(item, data, contracts) for key, item in check["fields"].items()}
    if op == "contract_field":
        contract = contracts.get(check.get("contract_id"))
        if not contract or check.get("field") not in contract:
            raise ValueError("Gold derivation refers to an unknown contract field")
        return deepcopy(contract[check["field"]])
    if op == "document_text":
        document = data["documents"].get(check.get("document_id"))
        if not document:
            raise ValueError("Gold derivation refers to an unknown document")
        return document["text"]
    if op == "refusal":
        if "condition_check" in check and not _equal(
            derive_value(check["condition_check"], data, contracts), check.get("condition_expected")
        ):
            raise ValueError("Gold refusal condition contradicts the source fixture")
        return None
    if op == "ratio":
        numerator = derive_value(check["numerator"], data, contracts)
        denominator = derive_value(check["denominator"], data, contracts)
        if numerator is None or denominator is None or denominator == 0:
            raise ValueError("Gold ratio has missing/zero denominator; it must refuse")
        return _json_number(Decimal(str(numerator)) / Decimal(str(denominator)))
    rows = _rows(check, data)
    columns = data["tables"][check["table"]]["columns"]
    if op == "row_count":
        return len(rows)
    field = check.get("field")
    if field not in columns:
        raise ValueError("Gold derivation refers to an unknown field")
    values = [row[field] for row in rows if row[field] is not None]
    if op == "values":
        return values
    if op == "nonnull_count":
        return len(values)
    if op == "null_count":
        return len(rows) - len(values)
    if op == "distinct_count":
        return len({canonical_digest(item) for item in values})
    if op == "group_sum":
        group = check.get("group")
        if group not in columns or any(not isinstance(row[group], str) for row in rows):
            raise ValueError("Gold grouping requires a declared string field")
        grouped: dict[str, list[Any]] = {}
        for row in rows:
            grouped.setdefault(row[group], [])
            if row[field] is not None:
                grouped[row[group]].append(row[field])
        return {key: _json_number(sum(_numeric(items), Decimal(0))) if items else None
                for key, items in sorted(grouped.items())}
    if op in {"sum", "mean"}:
        if not values:
            return None
        total = sum(_numeric(values), Decimal(0))
        return _json_number(total / len(values) if op == "mean" else total)
    raise ValueError("Unsupported gold derivation operation")


def _source_data(data: Any, contracts: Any, sources: set[str]) -> None:
    _object(data, {"tables", "documents"}, "Source fixtures")
    if not isinstance(data["tables"], dict) or not isinstance(data["documents"], dict):
        raise ValueError("Source fixture tables/documents must be objects")  # noqa: TRY004 -- schema
    if not isinstance(contracts, dict) or not contracts:
        raise ValueError("Synthetic contracts are required")
    citations: set[str] = set()
    for table_id, table in data["tables"].items():
        _identifier(table_id, "Table ID")
        _object(table, {"source_id", "columns", "rows", "citation_id"}, "Fixture table")
        if table["source_id"] not in sources or not isinstance(table["columns"], dict):
            raise ValueError("Fixture table source/schema is invalid")
        if not isinstance(table["rows"], list) or len(table["rows"]) > 10000:
            raise ValueError("Fixture rows exceed the bounded corpus")
        for column, kind in table["columns"].items():
            _identifier(column, "Column")
            if kind not in {"string", "number", "integer", "boolean"}:
                raise ValueError("Unsupported synthetic column type")
        for row in table["rows"]:
            _object(row, set(table["columns"]), "Fixture row")
            for field, kind in table["columns"].items():
                item = row[field]
                if item is None:
                    continue
                types = {"string": {str}, "number": {int, float}, "integer": {int},
                         "boolean": {bool}}
                if type(item) not in types[kind]:
                    raise ValueError("Fixture row violates its declared schema")
        citation = _identifier(table["citation_id"], "Table citation")
        if citation in citations:
            raise ValueError("Citation IDs must be unique")
        citations.add(citation)
    for identifier, document in data["documents"].items():
        _identifier(identifier, "Document citation")
        _object(document, {"source_id", "text"}, "Fixture document")
        if document["source_id"] not in sources or not isinstance(document["text"], str):
            raise ValueError("Fixture document is invalid")
        if identifier in citations:
            raise ValueError("Citation IDs must be unique")
        citations.add(identifier)
    for identifier, contract in contracts.items():
        _identifier(identifier, "Contract ID")
        if not isinstance(contract, dict) or contract.get("source_id") not in sources:
            raise ValueError("Synthetic contract source is invalid")
        if contract.get("citation_id") not in citations:
            raise ValueError("Synthetic contract needs a resolvable source citation")


def public_question(question: dict[str, Any]) -> dict[str, Any]:
    return {k: deepcopy(v) for k, v in question.items() if k not in {"gold", "check"}}


def question_digest(question: dict[str, Any]) -> str:
    return canonical_digest(public_question(question))


def _validate_check(check: Any, depth: int = 0) -> None:
    if not isinstance(check, dict) or depth > 8:
        raise ValueError("Gold derivation must be a bounded object")
    op = check.get("op")
    if op == "object":
        _object(check, {"op", "fields"}, "Gold object derivation")
        if not isinstance(check["fields"], dict) or not 1 <= len(check["fields"]) <= 20:
            raise ValueError("Gold object fields must be bounded")
        for key, item in check["fields"].items():
            _identifier(key, "Gold output field")
            _validate_check(item, depth + 1)
    elif op == "ratio":
        _object(check, {"op", "numerator", "denominator"}, "Gold ratio")
        _validate_check(check["numerator"], depth + 1)
        _validate_check(check["denominator"], depth + 1)
    elif op == "contract_field":
        _object(check, {"op", "contract_id", "field"}, "Gold contract derivation")
        _identifier(check["contract_id"], "Gold contract")
        _identifier(check["field"], "Gold contract field")
    elif op == "document_text":
        _object(check, {"op", "document_id"}, "Gold document derivation")
        _identifier(check["document_id"], "Gold document")
    elif op == "refusal":
        _object(check, {"op", "condition_check", "condition_expected"}
                if len(check) > 1 else {"op"}, "Gold refusal derivation")
        if "condition_check" in check:
            _validate_check(check["condition_check"], depth + 1)
    elif op in {"row_count", "values", "nonnull_count", "null_count", "distinct_count",
                "sum", "mean", "group_sum"}:
        keys = {"op", "table", "where"}
        if op != "row_count":
            keys.add("field")
        if op == "group_sum":
            keys.add("group")
        _object(check, keys, "Gold table derivation")
        for key in keys - {"op", "where"}:
            _identifier(check[key], f"Gold {key}")
        if not isinstance(check["where"], list) or len(check["where"]) > 20:
            raise ValueError("Gold filters must be bounded")
        for condition in check["where"]:
            _object(condition, {"field", "op", "value"}, "Gold filter")
            _identifier(condition["field"], "Gold filter field")
            if condition["op"] not in {"eq", "ne", "in", "gte", "lt"}:
                raise ValueError("Unsupported gold filter")
    else:
        raise ValueError("Unsupported gold derivation operation")


def validate_benchmark(value: dict[str, Any]) -> dict[str, Any]:
    """Validate all pins and derive every gold value from declared synthetic facts."""
    _object(value, {"schema_version", "benchmark_id", "designation", "synthetic",
                    "description", "sources", "cases", "source_data", "contracts"}, "Benchmark")
    if value["schema_version"] != BENCHMARK_VERSION or value["synthetic"] is not True:
        raise ValueError("This evaluator requires the declared synthetic benchmark format")
    if value["designation"] not in {"development_regression", "frozen_holdout"}:
        raise ValueError("Benchmark must declare its examination/holdout designation")
    _identifier(value["benchmark_id"], "Benchmark ID")
    canonical_digest(value)
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("Complete benchmark exceeds byte budget")
    if not isinstance(value["sources"], list) or not 1 <= len(value["sources"]) <= 20:
        raise ValueError("Benchmark needs bounded source bindings")
    source_ids: set[str] = set()
    for source in value["sources"]:
        _object(source, {"source_id", "fixture_revision", "fixture_digest", "native_version",
                         "source_observed_at"}, "Source binding")
        identifier = _identifier(source["source_id"], "Source ID")
        if identifier in source_ids:
            raise ValueError("Repeated source ID")
        source_ids.add(identifier)
        _identifier(source["fixture_revision"], "Fixture revision")
        for key in ("native_version", "source_observed_at"):
            if source[key] is not None and not isinstance(source[key], str):
                raise ValueError("Unknown native version/clock must remain null")
    _source_data(value["source_data"], value["contracts"], source_ids)
    for source in value["sources"]:
        if source["fixture_digest"] != source_digest(source["source_id"], value["source_data"],
                                                     value["contracts"]):
            raise ValueError("Source fixture/content binding changed")
    cases = value["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError("Benchmark requires bounded questions")
    question_ids: set[str] = set()
    citations = {t["citation_id"] for t in value["source_data"]["tables"].values()}
    citations.update(value["source_data"]["documents"])
    citation_sources = {t["citation_id"]: t["source_id"]
                        for t in value["source_data"]["tables"].values()}
    citation_sources.update({k: d["source_id"]
                             for k, d in value["source_data"]["documents"].items()})
    for case in cases:
        _object(case, {"id", "category", "business_unit", "prompt", "source_ids", "context",
                       "exact_result", "value_target_case", "gold", "check"}, "Question")
        identifier = _identifier(case["id"], "Question ID")
        if identifier in question_ids:
            raise ValueError("Repeated question ID")
        question_ids.add(identifier)
        if case["category"] not in _CATEGORIES or case["business_unit"] not in {"finance", "support"}:
            raise ValueError("Unknown benchmark category/business unit")
        if not isinstance(case["prompt"], str) or not 1 <= len(case["prompt"]) <= 4000:
            raise ValueError("Question prompt is required and bounded")
        if type(case["exact_result"]) is not bool or type(case["value_target_case"]) is not bool:
            raise ValueError("Question metric/target flags must be booleans")
        if not set(_identifiers(case["source_ids"], "Question sources")) <= source_ids:
            raise ValueError("Question source is unresolved")
        _object(case["context"], {"subject", "allowed_sources", "refusal_reasons"}, "Question context")
        _identifier(case["context"]["subject"], "Question subject")
        if not set(_identifiers(case["context"]["allowed_sources"], "Allowed sources")) <= source_ids:
            raise ValueError("Allowed source is unresolved")
        _identifiers(case["context"]["refusal_reasons"], "Refusal conditions")
        gold = _object(case["gold"], {"status", "value", "refusal_code", "contract_ids",
                                     "required_citation_ids", "allowed_citation_ids"}, "Gold")
        if gold["status"] not in {"answered", "refused"}:
            raise ValueError("Gold status is invalid")
        if not set(_identifiers(gold["contract_ids"], "Gold contracts")) <= set(value["contracts"]):
            raise ValueError("Gold contract is unresolved")
        required = set(_identifiers(gold["required_citation_ids"], "Required citations"))
        allowed = set(_identifiers(gold["allowed_citation_ids"], "Allowed citations"))
        if not required <= allowed <= citations:
            raise ValueError("Gold citation is unresolved")
        if any(citation_sources[c] not in case["context"]["allowed_sources"] for c in allowed):
            raise ValueError("Gold cannot authorize a denied-source citation")
        if any(value["contracts"][c]["source_id"] not in case["context"]["allowed_sources"]
               for c in gold["contract_ids"]):
            raise ValueError("Gold cannot expose a denied-source contract")
        if not isinstance(case["check"], dict):
            raise ValueError("Gold requires a fixture derivation object")  # noqa: TRY004 -- schema
        _validate_check(case["check"])
        if not _equal(derive_value(case["check"], value["source_data"], value["contracts"]),
                      gold["value"]):
            raise ValueError(f"Gold arithmetic/contract mismatch for {identifier}")
        if gold["status"] == "refused":
            if (case["check"].get("op") != "refusal" or gold["value"] is not None
                    or gold["refusal_code"] not in case["context"]["refusal_reasons"]):
                raise ValueError("Refusal needs an explicit declared condition and null value")
        elif gold["refusal_code"] is not None or case["check"].get("op") == "refusal":
            raise ValueError("Answered gold cannot carry a refusal")
    return deepcopy(value)


def result_template(benchmark: dict[str, Any], *, arm_id: str, harness: dict[str, Any],
                    budget: dict[str, Any]) -> dict[str, Any]:
    """Return pins and empty outcomes for a consumer; no gold answers are included."""
    benchmark = validate_benchmark(benchmark)
    if arm_id not in {"A", "B"}:
        raise ValueError("Anonymous arm_id must be A or B")
    return {
        "schema_version": RESULT_VERSION, "benchmark_id": benchmark["benchmark_id"],
        "benchmark_digest": canonical_digest(benchmark), "arm_id": arm_id,
        "harness": deepcopy(harness), "budget": deepcopy(budget),
        "results": [{"question_id": case["id"], "question_digest": question_digest(case),
                     "source_bindings": [deepcopy(source) for source in benchmark["sources"]
                                         if source["source_id"] in case["source_ids"]],
                     "response": None, "observations": None} for case in benchmark["cases"]],
    }


def grade_result(question: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Grade typed value, selected contracts, refusal reason and exact citation handles."""
    _object(response, {"status", "value", "refusal_code", "contract_ids", "citation_ids"}, "Response")
    if response["status"] not in {"answered", "refused"}:
        raise ValueError("Response status is invalid")
    canonical_digest(response)
    if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > 65536:
        raise ValueError("Response exceeds the complete-answer byte budget")
    contracts = set(_identifiers(response["contract_ids"], "Response contracts"))
    citations = set(_identifiers(response["citation_ids"], "Response citations"))
    if response["status"] == "refused":
        if response["value"] is not None:
            raise ValueError("A refusal cannot also contain an answer value")
        _identifier(response["refusal_code"], "Refusal code")
    elif response["refusal_code"] is not None:
        raise ValueError("An answer cannot also carry a refusal code")
    gold = question["gold"]
    correct_status = response["status"] == gold["status"]
    answer_correct = correct_status and _equal(response["value"], gold["value"])
    refusal_correct = (correct_status and response["refusal_code"] == gold["refusal_code"]
                       if gold["status"] == "refused" else None)
    contract_correct = contracts == set(gold["contract_ids"])
    citation_correct = (set(gold["required_citation_ids"]) <= citations
                        <= set(gold["allowed_citation_ids"]))
    return {
        "question_id": question["id"], "category": question["category"],
        "answer_correct": answer_correct, "contract_correct": contract_correct,
        "citations_correct": citation_correct, "refusal_correct": refusal_correct,
        "useful_correct": answer_correct and contract_correct and citation_correct
        and refusal_correct is not False,
        "exact_result": question["exact_result"],
        "value_target_case": question["value_target_case"],
    }


def _observations(value: Any) -> dict[str, Any]:
    if value is None:
        value = {"permission_leaks": None, "source_writes": None,
                 "safety_evidence": {"permission_leaks": None, "source_writes": None},
                 "telemetry": {key: None for key in _TELEMETRY}}
    _object(value, {"permission_leaks", "source_writes", "safety_evidence", "telemetry"}, "Observations")
    _object(value["safety_evidence"], {"permission_leaks", "source_writes"}, "Safety evidence")
    _object(value["telemetry"], _TELEMETRY, "Telemetry")
    for key in ("permission_leaks", "source_writes"):
        if value[key] is not None:
            _number(value[key], key, integer=True)
            _identifier(value["safety_evidence"][key], "External safety trace handle")
        elif value["safety_evidence"][key] is not None:
            raise ValueError("Safety trace without a measured count is ambiguous")
    for key, item in value["telemetry"].items():
        if item is not None:
            _number(item, key, integer=key not in {"elapsed_ms", "cost_usd"})
    return deepcopy(value)


def _arm(benchmark: dict[str, Any], arm: Any) -> list[dict[str, Any]]:
    _object(arm, {"schema_version", "benchmark_id", "benchmark_digest", "arm_id", "harness",
                  "budget", "results"}, "Arm file")
    if (arm["schema_version"] != RESULT_VERSION or arm["arm_id"] not in {"A", "B"}
            or arm["benchmark_id"] != benchmark["benchmark_id"]
            or arm["benchmark_digest"] != canonical_digest(benchmark)):
        raise ValueError("Arm benchmark/schema/anonymous identity mismatch")
    _object(arm["harness"], {"name", "version", "model", "model_version"}, "Harness identity")
    for key, item in arm["harness"].items():
        if not isinstance(item, str) or not 1 <= len(item) <= 200:
            raise ValueError(f"Exact harness {key} is required")
    _object(arm["budget"], _BUDGET, "Run budget")
    for key, item in arm["budget"].items():
        if item is None and key == "max_cost_usd":
            continue
        _number(item, key, integer=key != "max_cost_usd")
    if not isinstance(arm["results"], list) or len(arm["results"]) != len(benchmark["cases"]):
        raise ValueError("Every benchmark question must have exactly one result")
    cases = {case["id"]: case for case in benchmark["cases"]}
    seen: set[str] = set()
    graded = []
    citation_sources = {t["citation_id"]: t["source_id"]
                        for t in benchmark["source_data"]["tables"].values()}
    citation_sources.update({k: d["source_id"]
                             for k, d in benchmark["source_data"]["documents"].items()})
    for row in arm["results"]:
        _object(row, {"question_id", "question_digest", "source_bindings", "response",
                      "observations"}, "Question result")
        identifier = row["question_id"]
        if identifier not in cases or identifier in seen:
            raise ValueError("Repeated or unknown question result ID")
        seen.add(identifier)
        case = cases[identifier]
        expected_sources = [source for source in benchmark["sources"]
                            if source["source_id"] in case["source_ids"]]
        if (row["question_digest"] != question_digest(case)
                or not _equal(row["source_bindings"], expected_sources)):
            raise ValueError("Question/context/source revision binding mismatch")
        grade = grade_result(case, row["response"])
        observations = _observations(row["observations"])
        denied_citations = [c for c in row["response"]["citation_ids"]
                            if c in citation_sources and citation_sources[c]
                            not in case["context"]["allowed_sources"]]
        denied_contracts = [c for c in row["response"]["contract_ids"]
                            if c in benchmark["contracts"] and benchmark["contracts"][c]["source_id"]
                            not in case["context"]["allowed_sources"]]
        measured = observations["permission_leaks"]
        grade["detected_unauthorized_citations"] = len(denied_citations)
        grade["detected_unauthorized_contracts"] = len(denied_contracts)
        if denied_citations or denied_contracts:
            grade["useful_correct"] = False
            observations["permission_leaks"] = max(len(denied_citations) + len(denied_contracts),
                                                  measured or 0)
        breaches = []
        for measured_key, budget_key in (("source_reads", "max_source_reads"),
                ("tool_calls", "max_tool_calls"), ("output_bytes", "max_output_bytes"),
                ("elapsed_ms", "max_elapsed_ms"), ("cost_usd", "max_cost_usd")):
            actual, limit = observations["telemetry"][measured_key], arm["budget"][budget_key]
            if actual is not None and limit is not None and actual > limit:
                breaches.append(measured_key)
        grade.update(observations=observations, budget_breaches=breaches)
        graded.append(grade)
    return sorted(graded, key=lambda row: row["question_id"])


def _measure(values: list[int | float | None]) -> dict[str, Any]:
    known = [item for item in values if item is not None]
    return {"known_count": len(known), "unknown_count": len(values) - len(known),
            "known_sum": sum(known) if known else None,
            "total": sum(known) if len(known) == len(values) else None}


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    refusals = [row for row in rows if row["refusal_correct"] is not None]
    exact = [row for row in rows if row["exact_result"]]
    safety = {key: _measure([row["observations"][key] for row in rows])
              for key in ("permission_leaks", "source_writes")}
    return {
        "count": len(rows),
        "useful_correct": sum(row["useful_correct"] for row in rows),
        "useful_accuracy": sum(row["useful_correct"] for row in rows) / len(rows),
        "answer_correct": sum(row["answer_correct"] for row in rows),
        "contract_correct": sum(row["contract_correct"] for row in rows),
        "citations_correct": sum(row["citations_correct"] for row in rows),
        "refusal_count": len(refusals),
        "refusals_correct": sum(row["refusal_correct"] for row in refusals),
        "exact_result_count": len(exact),
        "exact_results_correct": sum(row["answer_correct"] for row in exact),
        "safety": safety,
        "safety_gate": ("failed" if any(row["observations"][key] not in {None, 0}
                        for row in rows for key in safety)
                        else "unknown" if any(v["total"] is None for v in safety.values()) else "passed"),
        "telemetry": {key: _measure([row["observations"]["telemetry"][key] for row in rows])
                      for key in sorted(_TELEMETRY)},
        "budget_breach_count": sum(bool(row["budget_breaches"]) for row in rows),
    }


def _paired(rows_a: list[dict[str, Any]], rows_b: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows_a)
    if not n:
        return {"count": 0, "delta_b_minus_a": None, "interval_95": None,
                "b_only_correct": 0, "a_only_correct": 0, "mcnemar_exact_p": None}
    wins = sum(b["useful_correct"] and not a["useful_correct"]
               for a, b in zip(rows_a, rows_b))
    losses = sum(a["useful_correct"] and not b["useful_correct"]
                 for a, b in zip(rows_a, rows_b))
    discordant = wins + losses
    p = min(1.0, 2 * sum(math.comb(discordant, k) for k in range(min(wins, losses) + 1))
            / 2**discordant) if discordant else 1.0
    delta = (wins - losses) / n
    # Paired differences lie in [-1, 1]. Hoeffding is deliberately conservative
    # and does not manufacture a zero-width interval for all-concordant pairs.
    radius = math.sqrt(2 * math.log(40) / n)
    return {"count": n, "delta_b_minus_a": delta,
            "interval_95": [max(-1.0, delta - radius), min(1.0, delta + radius)],
            "interval_method": "paired_bounded_difference_hoeffding",
            "b_only_correct": wins, "a_only_correct": losses,
            "both_correct": sum(a["useful_correct"] and b["useful_correct"]
                                for a, b in zip(rows_a, rows_b)),
            "both_incorrect": sum(not a["useful_correct"] and not b["useful_correct"]
                                  for a, b in zip(rows_a, rows_b)),
            "mcnemar_exact_p": p,
            "assumption": "Independent question outcomes; this hand-authored synthetic corpus "
            "does not establish a representative population or fresh real-world holdout."}


def evaluate_paired(benchmark: dict[str, Any], arm_a: dict[str, Any],
                    arm_b: dict[str, Any], *, gain_target: float = 0.10) -> dict[str, Any]:
    """Compare full matched anonymous arms, retaining missing observations as unknown."""
    benchmark = validate_benchmark(benchmark)
    _number(gain_target, "Gain target")
    if gain_target > 1:
        raise ValueError("Gain target must be between zero and one")
    if arm_a.get("arm_id") != "A" or arm_b.get("arm_id") != "B":
        raise ValueError("Paired arguments require distinct anonymous arms A and B")
    rows_a, rows_b = _arm(benchmark, arm_a), _arm(benchmark, arm_b)
    if (not _equal(arm_a["harness"], arm_b["harness"])
            or not _equal(arm_a["budget"], arm_b["budget"])):
        raise ValueError("Paired harness/model/version/budget must match exactly")
    summaries = {"A": _summary(rows_a), "B": _summary(rows_b)}
    targeted = _paired([r for r in rows_a if r["value_target_case"]],
                       [r for r in rows_b if r["value_target_case"]])
    observed = targeted["delta_b_minus_a"]
    return {
        "schema_version": REPORT_VERSION, "benchmark_id": benchmark["benchmark_id"],
        "benchmark_digest": canonical_digest(benchmark),
        "designation": benchmark["designation"], "synthetic": True,
        "harness": deepcopy(arm_a["harness"]), "budget": deepcopy(arm_a["budget"]),
        "arms": summaries, "paired": _paired(rows_a, rows_b), "value_cases": targeted,
        "gain_target": gain_target,
        "observed_target_met": observed >= gain_target if observed is not None else None,
        "interval_target_met": (targeted["interval_95"][0] >= gain_target
                                if targeted["interval_95"] is not None else None),
        "exact_result_non_regression": (summaries["B"]["exact_results_correct"]
        >= summaries["A"]["exact_results_correct"]
        if summaries["A"]["exact_result_count"] else None),
        "claim_scope": "Synthetic fixture grading only. This grader ran no model, queried no "
        "provider, and did not independently audit supplied safety trace handles. "
        "Development regression cannot establish model uplift or release readiness.",
        "questions": [{"question_id": a["question_id"], "A": a, "B": b}
                      for a, b in zip(rows_a, rows_b)],
    }
