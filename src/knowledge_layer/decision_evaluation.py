"""Aggregate, source-text-free measurements for a labeled decision workload."""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Iterable
from typing import Any

from knowledge_layer.decisions import ROUTES, DecisionProvider


def evaluate_decisions(
    records: Iterable[dict[str, Any]], provider: DecisionProvider, *, gate_floor: float = 0.98,
) -> dict[str, Any]:
    if not 0.95 <= gate_floor <= 1:
        raise ValueError("gate_floor must be 0.95-1")
    confusion = {expected: {actual: 0 for actual in sorted(ROUTES)} for expected in sorted(ROUTES)}
    latencies: list[float] = []
    brier: list[float] = []
    calibration: list[tuple[float, bool]] = []
    count = skipped = false_skips = gate_candidates = false_gate_skips = input_tokens = 0
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("packet"), dict):
            raise TypeError("Every evaluation record needs a packet object")
        expected = record.get("expected_route")
        if expected not in ROUTES:
            raise ValueError("Every evaluation record needs a valid expected_route")
        start = time.perf_counter()
        decision = provider.assess(record["packet"])
        latencies.append(round((time.perf_counter() - start) * 1000, 2))
        if decision.route not in ROUTES:
            raise ValueError("Provider returned an invalid route")
        count += 1
        confusion[expected][decision.route] += 1
        if decision.route != "curate":
            skipped += 1
            false_skips += expected == "curate"
        if decision.input_tokens is not None:
            input_tokens += decision.input_tokens
        if decision.probabilities is not None:
            probabilities = decision.probabilities
            if set(probabilities) != ROUTES or any(
                not math.isfinite(value) or value < 0 or value > 1
                for value in probabilities.values()
            ):
                raise ValueError("Provider returned invalid probabilities")
            brier.append(sum((probabilities[route] - (route == expected)) ** 2 for route in ROUTES))
            calibration.append((probabilities[decision.route], decision.route == expected))
        if decision.route != "curate" and decision.probability is not None and decision.probability >= gate_floor:
            gate_candidates += 1
            false_gate_skips += expected == "curate"
    if not count:
        raise ValueError("Evaluation needs at least one labeled record")
    ordered = sorted(latencies)
    bins = []
    for lower, upper in ((0.0, 0.5), (0.5, 0.8), (0.8, 0.95), (0.95, 1.001)):
        members = [(p, correct) for p, correct in calibration if lower <= p < upper]
        if members:
            bins.append({
                "range": [lower, upper], "count": len(members),
                "mean_probability": round(statistics.mean(p for p, _ in members), 4),
                "accuracy": round(statistics.mean(int(correct) for _, correct in members), 4),
            })
    expected_calibration_error = sum(
        row["count"] / len(calibration) * abs(row["mean_probability"] - row["accuracy"])
        for row in bins
    ) if calibration else None
    return {
        "count": count, "confusion": confusion,
        "correct": sum(confusion[route][route] for route in ROUTES),
        "accuracy": round(sum(confusion[route][route] for route in ROUTES) / count, 4),
        "routed_without_generation": skipped,
        "false_skips_of_needed_generation": false_skips,
        "gate_floor": gate_floor, "gate_candidates": gate_candidates,
        "false_gate_skips": false_gate_skips,
        "brier_mean": round(statistics.mean(brier), 4) if brier else None,
        "expected_calibration_error": round(expected_calibration_error, 4) if expected_calibration_error is not None else None,
        "calibration_bins": bins,
        "input_tokens": input_tokens,
        "latency_ms_p50": ordered[len(ordered) // 2],
        "latency_ms_p95": ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)],
        "note": "Labels are operator supplied; this report does not certify a model or grant approval authority.",
    }
