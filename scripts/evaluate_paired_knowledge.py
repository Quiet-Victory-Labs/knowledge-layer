#!/usr/bin/env python3
"""Grade existing anonymous result files; never execute a source or model."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from knowledge_layer.evaluation import evaluate_paired, load_benchmark, load_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path,
                        default=Path(__file__).resolve().parents[1]
                        / "tests/fixtures/enterprise_benchmark")
    parser.add_argument("--arm-a", type=Path, required=True)
    parser.add_argument("--arm-b", type=Path, required=True)
    parser.add_argument("--gain-target", type=float, default=0.10,
                        help="Absolute paired accuracy difference, default 0.10")
    parser.add_argument("--output", type=Path,
                        help="New report file; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        report = evaluate_paired(load_benchmark(args.benchmark), load_json(args.arm_a),
                                 load_json(args.arm_b), gain_target=args.gain_target)
        body = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(body)
        else:
            sys.stdout.write(body)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Paired evaluation refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
