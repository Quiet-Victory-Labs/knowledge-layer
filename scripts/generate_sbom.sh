#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${1:-$repo_dir/.local/sbom}"
mkdir -p "$output_dir"

cd "$repo_dir"
uv export --frozen --no-dev --format cyclonedx1.5 \
  --output-file "$output_dir/python.cdx.json" > /dev/null
uv export --frozen --no-dev --all-extras --no-extra dev --format cyclonedx1.5 \
  --output-file "$output_dir/python-all-runtime-extras.cdx.json" > /dev/null
# Every shipped connector/validator is a separate install scope. The dev extra
# is deliberately excluded; an extras union is not the deployed base runtime.
while IFS= read -r extra; do
  uv export --frozen --no-dev --extra "$extra" --format cyclonedx1.5 \
    --output-file "$output_dir/python-$extra.cdx.json" > /dev/null
done < <(python3 - <<'PY'
import tomllib
from pathlib import Path
project = tomllib.loads(Path('pyproject.toml').read_text())['project']
for extra in sorted(project.get('optional-dependencies', {})):
    if extra != 'dev':
        print(extra)
PY
)
# The web source build needs the complete lock, including build/test tooling.
npm --prefix web sbom --package-lock-only --sbom-format=cyclonedx \
  > "$output_dir/web.cdx.json"

python3 - "$output_dir" <<'PY'
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
for path in sorted(output.glob('*.cdx.json')):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("bomFormat") != "CycloneDX" or not data.get("components"):
        raise SystemExit(f"Invalid {path.name} SBOM")
    print(f"{path.name}: {len(data['components'])} components")
coverage = {'schema_version': 'sbom-scopes-v1', 'python_base': 'python.cdx.json',
    'python_runtime_extras_union': 'python-all-runtime-extras.cdx.json',
    'web_lock': 'web.cdx.json',
    'dev_dependencies_included': True,
    'python_dev_dependencies_included': False,
    'web_dev_dependencies_included': True,
    'files': sorted(path.name for path in output.glob('*.cdx.json')),
    'installed_platform_coverage': 'not_inferred', 'license_approval': False}
(output / 'scopes.json').write_text(json.dumps(coverage, indent=2) + '\n')
PY
