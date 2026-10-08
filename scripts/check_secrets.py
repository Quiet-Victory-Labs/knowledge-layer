"""Fail CI on new secret candidates without printing candidate values."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BASELINE_BYTES = 5 * 1024 * 1024
FAILURE_MESSAGE = "Secret candidate scan failed; review locally with detect-secrets-hook"


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Invalid baseline")
        result[key] = value
    return result


def _read_baseline(path: Path) -> bytes:
    with path.open("rb") as handle:
        raw = handle.read(MAX_BASELINE_BYTES + 1)
    if len(raw) > MAX_BASELINE_BYTES:
        raise ValueError("Invalid baseline")
    return raw


def _candidate_identities(raw: bytes) -> set[tuple[str, str, str]]:
    baseline = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys)
    if not isinstance(baseline, dict) or baseline.get("version") != "1.5.0":
        raise ValueError("Invalid baseline")
    results = baseline.get("results")
    if not isinstance(results, dict):
        raise TypeError("Invalid baseline")
    identities = set()
    for filename, candidates in results.items():
        if not filename or not isinstance(candidates, list):
            raise ValueError("Invalid baseline")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise TypeError("Invalid baseline")
            hashed_secret, kind = candidate.get("hashed_secret"), candidate.get("type")
            if (candidate.get("filename") != filename
                    or not isinstance(hashed_secret, str) or not re.fullmatch(r"[a-f0-9]{40}", hashed_secret)
                    or not isinstance(kind, str) or not kind):
                raise ValueError("Invalid baseline")
            identities.add((filename, hashed_secret, kind))
    return identities


def main() -> int:
    try:
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=ROOT, stderr=subprocess.DEVNULL, timeout=10,
        ).decode("utf-8").split("\0")
        files = [path for path in tracked if path and path != ".secrets.baseline"]
        if not files:
            raise ValueError("No tracked files")
        reviewed = _read_baseline(ROOT / ".secrets.baseline")
        original_identities = _candidate_identities(reviewed)
        with tempfile.TemporaryDirectory(prefix="knowledge-layer-secret-scan-") as directory:
            baseline = Path(directory) / ".secrets.baseline"
            descriptor = os.open(baseline, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(reviewed)
            command = [
                "uvx", "--from", "detect-secrets==1.5.0", "detect-secrets-hook",
                "--no-verify", "--baseline", str(baseline), "--", *files,
            ]
            # Retain zero bytes of detector diagnostics: they may contain secrets.
            result = subprocess.run(
                command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=90,
            )
            if (result.returncode not in {0, 3} or result.returncode == 3
                    and not _candidate_identities(_read_baseline(baseline)) <= original_identities):
                raise ValueError("New or invalid candidates")
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, subprocess.SubprocessError):
        print(FAILURE_MESSAGE)
        return 1
    print(f"Secret candidate scan passed for {len(files)} tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
