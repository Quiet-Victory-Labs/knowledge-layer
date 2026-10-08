"""Join every generated SBOM scope to publisher/installed metadata; never approve licenses."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
from collections import Counter
from pathlib import Path


def canonical(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def has_license_metadata(record: dict) -> bool:
    return bool(record["declared_licenses"] or any(
        record.get(source, {}).get(key)
        for source in ("installed_metadata", "artifact_metadata")
        for key in ("license_expression", "legacy_license", "license_classifiers")))


def collect(directory: Path, artifact_metadata: Path | None = None) -> dict:
    installed = {(canonical(dist.metadata["Name"]), dist.version): dist
                 for dist in importlib.metadata.distributions() if dist.metadata["Name"]}
    components = {}
    scopes = sorted(directory.glob("*.cdx.json"))
    if not scopes:
        raise ValueError("No SBOM scopes")
    for scope in scopes:
        data = json.loads(scope.read_text())
        if data.get("bomFormat") != "CycloneDX":
            raise ValueError("Invalid SBOM")
        for component in data.get("components", []):
            name, version = component["name"], component.get("version")
            purl = component.get("purl", "")
            component_type = component.get("type", "library" if purl else "unknown")
            key = purl or f"{component_type}:{name}@{version}"
            record = components.setdefault(key, {"name": name, "version": version, "purl": purl,
                "scopes": [], "component_types": [], "declared_licenses": [],
                "metadata_source": "sbom", "review_status": "independent_review_required"})
            if (canonical(record["name"]), record["version"]) != (canonical(name), version):
                raise ValueError("Conflicting component identity in SBOM scopes")
            if scope.name not in record["scopes"]:
                record["scopes"].append(scope.name)
            if component_type not in record["component_types"]:
                record["component_types"].append(component_type)
            for declaration in component.get("licenses", []):
                if declaration not in record["declared_licenses"]:
                    record["declared_licenses"].append(declaration)
            if (not record["declared_licenses"] and purl.startswith("pkg:pypi/")
                    and (dist := installed.get((canonical(name), version))) is not None):
                expression = dist.metadata.get("License-Expression")
                legacy = dist.metadata.get("License")
                classifiers = [item for item in dist.metadata.get_all("Classifier", [])
                               if item.startswith("License ::")]
                record["installed_metadata"] = {"license_expression": expression,
                    "legacy_license": legacy, "license_classifiers": classifiers,
                    "license_files": dist.metadata.get_all("License-File", [])}
                record["metadata_source"] = "matching_installed_distribution"
    if artifact_metadata is not None:
        evidence = json.loads(artifact_metadata.read_text())
        if evidence.get("schema_version") != "locked-license-metadata-v1":
            raise ValueError("Invalid artifact metadata")
        for entry in evidence["entries"]:
            record = components.get(entry["purl"])
            if record is None or (canonical(record["name"]), record["version"]) != (
                    canonical(entry["name"]), entry["version"]):
                raise ValueError("Artifact metadata is outside the selected SBOM scopes")
            record["artifact_metadata"] = entry
    records = sorted(components.values(), key=lambda item: (item["name"], item["version"] or ""))
    for record in records:
        record["component_types"].sort()
        record["component_type"] = record["component_types"][0] if len(record["component_types"]) == 1 else "ambiguous"
    types = Counter(record["component_type"] for record in records)
    missing_types = Counter(record["component_type"] for record in records if not has_license_metadata(record))
    return {"schema_version": "dependency-license-inventory-v1", "components": records,
            "component_count": len(records), "component_type_counts": dict(sorted(types.items())),
            "non_file_component_count": len(records) - types["file"],
            "missing_license_metadata": sum(missing_types.values()),
            "missing_license_metadata_by_type": dict(sorted(missing_types.items())),
            "non_file_missing_license_metadata": sum(missing_types.values()) - missing_types["file"],
            "scopes": [path.name for path in scopes], "legal_approved": False,
            "notices_complete": False, "source_data_rights": "independent_review_required",
            "limitations": ["Installed metadata covers only matching versions on this runner.",
                "SBOM declarations and classifiers are evidence, not license compatibility decisions.",
                "File entries are retained inventory evidence, not separate package dependencies.",
                "Optional artifact metadata covers one locked wheel, not every platform variant or required license text.",
                "License texts, platform variants, binary/source obligations and notices require review."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sbom-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-metadata", type=Path,
                        help="Join explicit exact-version evidence from fetch_license_metadata.py; no network here")
    args = parser.parse_args()
    try:
        result = collect(args.sbom_dir, args.artifact_metadata)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({key: result[key] for key in
                          ("component_count", "non_file_component_count", "missing_license_metadata",
                           "non_file_missing_license_metadata", "legal_approved", "notices_complete")}))
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print("Dependency license inventory failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
