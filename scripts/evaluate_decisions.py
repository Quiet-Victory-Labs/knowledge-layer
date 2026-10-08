"""Evaluate rules or a Jev-compatible provider on locally labeled curation packets.

Input JSONL: {"packet": <one bounded curation packet>, "expected_route":
"curate" | "needs_steward" | "defer"}. Reports aggregate metrics only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from knowledge_layer.decision_evaluation import evaluate_decisions
from knowledge_layer.decisions import RuleDecisionProvider, SystemOneDecisionProvider
from knowledge_layer.operator_secrets import operator_secret


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="Local labeled JSONL file; keep customer packets out of Git")
    parser.add_argument("--provider", choices=("rules", "system-one"), default="rules")
    parser.add_argument("--gate-floor", type=float, default=0.98)
    parser.add_argument("--output", type=Path, default=Path(".local/decision-evaluation.json"))
    args = parser.parse_args()
    raw = args.dataset.read_bytes()
    if len(raw) > 20_000_000:
        parser.error("The labeled dataset must be under 20 MB")
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not 1 <= len(rows) <= 10_000:
        parser.error("The labeled dataset must have 1-10,000 records")
    if args.provider == "rules":
        provider = RuleDecisionProvider()
        provider_name = "rules"
    else:
        url = os.getenv("KNOWLEDGE_DECISION_URL", "")
        if urlparse(url).scheme == "https" and os.getenv("KNOWLEDGE_DECISION_ALLOW_REMOTE") != "1":
            parser.error("Remote decision providers require KNOWLEDGE_DECISION_ALLOW_REMOTE=1")
        model = os.getenv("KNOWLEDGE_DECISION_MODEL", "")
        provider = SystemOneDecisionProvider(
            url=url, model=model, token=operator_secret("KNOWLEDGE_DECISION_API_KEY") or "",
            allow_remote=os.getenv("KNOWLEDGE_DECISION_ALLOW_REMOTE") == "1",
        )
        provider_name = model
    metrics = evaluate_decisions(rows, provider, gate_floor=args.gate_floor)
    report = {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "provider": args.provider, "model": provider_name,
        "metrics": metrics,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(args.output), "count": metrics["count"],
        "false_gate_skips": metrics["false_gate_skips"],
        "gate_candidates": metrics["gate_candidates"],
        "accuracy": metrics["accuracy"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
