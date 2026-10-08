"""Translate an operator-scoped artifact to adapter v1.2 JSON; never run dbt."""

import argparse
import json
import sys

from knowledge_layer.adapters.dbt import MAX_BYTES, import_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--unique-id", action="append", required=True)
    args = parser.parse_args()
    with open(args.manifest, "rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    try:
        result = import_manifest(raw, namespace=args.namespace, unique_ids=args.unique_id)
    except ValueError:
        parser.exit(2, "dbt import refused; check schema, exact scope and artifact size\n")
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
