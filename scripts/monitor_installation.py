"""Bounded deployment health/freshness samples, with no source writes or models.

Run once for an operator check or up to seven days for an endurance record. Exit
one if any sample fails. Alert routing and customer fault scenarios are separate
operator integrations; seven days of empty health checks do not qualify a pilot.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from knowledge_layer.operator_secrets import read_owner_only_file


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin(value: str, allow_loopback: bool = False) -> str:
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")
            or (parsed.scheme != "https" and not (allow_loopback and parsed.scheme == "http"
                                                  and parsed.hostname in {"127.0.0.1", "localhost", "::1"}))):
        raise ValueError("https_origin_required")
    return value.rstrip("/")


def request(base: str, path: str, token: str | None) -> tuple[int, dict]:
    headers = {"Authorization": "Bearer " + token} if token else {}
    try:
        with build_opener(NoRedirect()).open(Request(base + path, headers=headers), timeout=10) as response:
            raw = response.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError("response_budget_exceeded")
            return response.status, json.loads(raw)
    except HTTPError as exc:
        # Never log or follow redirect destinations or provider diagnostics.
        return exc.code, {}


def sample(base: str, token_file: Path, expected_sources: list[str], max_age: int) -> dict:
    started = monotonic()
    status, _ = request(base, "/api/ready", None)
    if status != 200:
        raise ValueError("readiness_failed")
    status, _ = request(base, "/agent/api/sources", None)
    if status not in {401, 403}:
        raise ValueError("unauthenticated_source_access_not_denied")
    token = read_owner_only_file(str(token_file)).decode().strip()
    if not token or len(token) > 16384:
        raise ValueError("agent_credential_unavailable")
    status, body = request(base, "/agent/api/sources?limit=100", token)
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("connections"), list):
        raise ValueError("authenticated_source_read_failed")
    visible = {item.get("id"): item for item in body["connections"] if isinstance(item, dict)}
    for identifier in expected_sources:
        source = visible.get(identifier)
        if source is None:
            raise ValueError("expected_source_not_visible_on_bounded_page")
        if source.get("status") in {"error", "configured"}:
            raise ValueError("source_not_ready")
        clock = source.get("last_complete_refresh_at")
        if not isinstance(clock, str):
            raise ValueError("complete_inventory_clock_unknown")  # noqa: TRY004 - bounded refusal code
        completed = datetime.fromisoformat(clock)
        if completed.tzinfo is None:
            raise ValueError("complete_inventory_clock_unknown")
        age = (datetime.now(UTC) - completed).total_seconds()
        if age < -60 or age > max_age:
            raise ValueError("complete_inventory_outside_freshness_window")
    return {"at": datetime.now(UTC).isoformat(), "result": "passed",
            "elapsed_ms": (monotonic() - started) * 1000,
            "expected_sources_checked": len(expected_sources),
            "claim_scope": "Readiness, unauthenticated denial and permitted complete-inventory clocks only"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--agent-token-file", type=Path, required=True)
    parser.add_argument("--expected-sources-file", type=Path, required=True, help="Owner-only JSON list, 1-25 source IDs")
    parser.add_argument("--freshness-max-age-seconds", type=int, default=86400)
    parser.add_argument("--duration-hours", type=int, default=0, help="0 runs once; maximum 168")
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--allow-loopback", action="store_true")
    parser.add_argument("--output", type=Path, required=True, help="New owner-only JSONL record")
    args = parser.parse_args(argv)
    if (not 0 <= args.duration_hours <= 168 or not 60 <= args.interval_seconds <= 3600
            or not 60 <= args.freshness_max_age_seconds <= 2678400):
        parser.error("Bounded duration, interval and freshness window required")
    try:
        base = origin(args.origin, args.allow_loopback)
        expected = json.loads(read_owner_only_file(str(args.expected_sources_file)))
        if (not isinstance(expected, list) or not 1 <= len(expected) <= 25
                or any(not isinstance(item, str) or not re.fullmatch(r"conn_[A-Za-z0-9_-]{1,155}", item) for item in expected)
                or len(expected) != len(set(expected))):
            raise ValueError("expected_sources_invalid")
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except (OSError, ValueError, UnicodeError):
        parser.exit(2, "Deployment monitor refused; check protected inputs and new output path\n")
    failures, samples, began = 0, 0, monotonic()
    deadline = began + args.duration_hours * 3600
    with os.fdopen(fd, "w") as stream:
        while True:
            try:
                record = sample(base, args.agent_token_file, expected, args.freshness_max_age_seconds)
            except (OSError, ValueError, UnicodeError) as exc:
                failures += 1
                # Only locally generated codes are admitted; network messages
                # can contain source paths, token values or private URLs.
                reason = str(exc) if type(exc) is ValueError and re.fullmatch(r"[a-z_]{1,80}", str(exc)) else "probe_failed"
                record = {"at": datetime.now(UTC).isoformat(), "result": "failed", "reason": reason}
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
            stream.flush()
            samples += 1
            if not args.duration_hours or monotonic() >= deadline:
                break
            sleep(min(args.interval_seconds, max(0, deadline - monotonic())))
    print(json.dumps({"schema_version": "knowledge-deployment-monitor-v1", "samples": samples,
                      "failed_samples": failures, "elapsed_seconds": monotonic() - began,
                      "customer_endurance_qualified": False}))
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
