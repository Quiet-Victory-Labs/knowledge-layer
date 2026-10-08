"""Optional model worker for a configured Chat Completions compatible endpoint.

Set MODEL_CHAT_COMPLETIONS_URL, MODEL_NAME, and MODEL_API_KEY (or
MODEL_API_KEY_FILE). The platform's bearer token is supplied through
KNOWLEDGE_AGENT_TOKEN (or KNOWLEDGE_AGENT_TOKEN_FILE). No model is bundled.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from urllib.parse import urlparse

from knowledge_layer.decisions import SystemOneDecisionProvider
from knowledge_layer.model_curator import run_once
from knowledge_layer.operator_secrets import operator_secret


def _secret(name: str) -> str:
    return (operator_secret(name) or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="Poll for new curation packets")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds")
    parser.add_argument("--decision-mode", choices=("shadow", "gate"), default="shadow",
                        help="Observe decisions or let calibrated high-probability decisions route work")
    args = parser.parse_args()
    if not 5 <= args.interval <= 3600:
        parser.error("--interval must be 5-3600 seconds")
    config = {
        "knowledge_url": os.getenv("KNOWLEDGE_GATEWAY_URL", "http://127.0.0.1:8001"),
        "knowledge_token": _secret("KNOWLEDGE_AGENT_TOKEN"),
        "model_url": os.getenv("MODEL_CHAT_COMPLETIONS_URL", ""),
        "model_name": os.getenv("MODEL_NAME", ""),
        "model_token": _secret("MODEL_API_KEY"),
        "allow_remote_model": os.getenv("KNOWLEDGE_MODEL_ALLOW_REMOTE") == "1",
    }
    decision_url = os.getenv("KNOWLEDGE_DECISION_URL", "").strip()
    if decision_url:
        if urlparse(decision_url).scheme == "https" and os.getenv("KNOWLEDGE_DECISION_ALLOW_REMOTE") != "1":
            parser.error("Remote decision providers require KNOWLEDGE_DECISION_ALLOW_REMOTE=1")
        config["decision_provider"] = SystemOneDecisionProvider(
            url=decision_url,
            model=os.getenv("KNOWLEDGE_DECISION_MODEL", ""),
            token=_secret("KNOWLEDGE_DECISION_API_KEY"),
            allow_remote=os.getenv("KNOWLEDGE_DECISION_ALLOW_REMOTE") == "1",
        )
        config["decision_mode"] = args.decision_mode
        config["decision_calibration_id"] = os.getenv("KNOWLEDGE_DECISION_CALIBRATION_ID")
        config["decision_min_probability"] = float(os.getenv("KNOWLEDGE_DECISION_MIN_PROBABILITY", "0.98"))
    elif args.decision_mode == "gate":
        parser.error("--decision-mode gate requires KNOWLEDGE_DECISION_URL")
    seen: set[str] = set()
    while True:
        report = run_once(**config, skip_work_ids=seen)
        fresh = [item for item in report["outcomes"] if item["work_id"] not in seen]
        seen.update(item["work_id"] for item in report["outcomes"]
                    if item["status"] in {"pending_review", "abstained", "needs_steward", "defer"})
        print(json.dumps({"work_seen": report["work_seen"], "new_outcomes": fresh, "pending_review": report["pending_review"]}))
        if not args.watch:
            return 0 if all(item["status"] in {"pending_review", "abstained", "needs_steward", "defer"}
                            for item in report["outcomes"]) else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
