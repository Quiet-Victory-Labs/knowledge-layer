# Dependency license review

This is a reproducible inventory, not legal approval for distribution. Run `scripts/generate_sbom.sh` from a clean checkout and review every generated CycloneDX scope under ignored `.local/sbom/` at the release commit. Historical counts below describe their dated inputs; the current candidate's archive/text collection is recorded below. New release changes need their own exact-commit inventory and review.

On 2026-10-02, the locked base dependency export contained 55 Python components and the web lockfile contained 71 components. All 71 web components had license fields in the npm-generated SBOM. The uv-generated Python SBOM had no license fields for its 55 components. Installed package metadata identified `psycopg` and `psycopg-binary` as LGPL-3.0-only; these are runtime dependencies, and their distribution obligations require independent review. Three Python components in the export were not installed on the Mac evaluation host, so local metadata inspection cannot settle their licenses or platform applicability.

Before a public source or binary release, QVL must review every direct and transitive dependency for the shipped platform and optional connector extras, retain required third-party notices, confirm the Apache Ossie schema provenance in [NOTICE](../NOTICE), and document any source-data rights. Do not infer compatibility from the project's Apache-2.0 license or a vulnerability-free dependency scan.

The recurrence increment makes the existing locked `tzdata` package an explicit core dependency for hosts without system IANA data. Its [publisher metadata](https://pypi.org/project/tzdata/) declares Apache-2.0; the release inventory still requires review on each shipped platform.

The optional `semantic-validation` extra locks pySHACL 0.40.1 and adds html5rdf
1.2.1, owlrl 7.6.2, PrettyTable 3.18.0 and wcwidth 0.9.1. Installed publisher
metadata identifies Apache-2.0, MIT, W3C-20150513, BSD-3-Clause and MIT respectively.
These packages are excluded from the base install. owlrl is a transitive validator
dependency; Knowledge Layer explicitly disables inference. This local metadata
inventory does not replace the release's complete dependency/notice review.

The release engineering scripts now generate `python.cdx.json` for the base,
`python-EXTRA.cdx.json` for every non-development extra,
`python-all-runtime-extras.cdx.json` for their union, and `web.cdx.json` from the
locked web dependency tree. The manual release workflow additionally generates
`container.cdx.json` with pinned Syft, covering operating-system and installed
runtime components. `scopes.json` states scope boundaries. A union inventory is
not the installed base runtime and does not prove every platform wheel was inspected.

Run the following after generating the SBOMs:

```bash
uv run --no-sync python scripts/fetch_license_metadata.py --sbom-dir .local/release/sbom \
  --output .local/release/locked-license-metadata.json
uv run --no-sync python scripts/dependency_licenses.py --sbom-dir .local/release/sbom \
  --artifact-metadata .local/release/locked-license-metadata.json \
  --output .local/release/dependency-licenses.json
```

It preserves each component's scope membership, SBOM declarations and matching
installed-package metadata, records missing license evidence, and always declares
`legal_approved=false` and `notices_complete=false`. Required license texts,
copyleft obligations, binary/source notices, optional platform variants and source
data rights remain independent review tasks. The root NOTICE preserves the
vendored Ossie notice; it is not a comprehensive third-party notice bundle.

Declarations from every SBOM scope are merged, even when a component first appears
without license metadata. File entries remain in the inventory but are counted
separately from packages, applications and the operating system; thousands of
container file entries are not thousands of unlicensed dependencies.

For optional or foreign-platform Python packages absent on the runner, the fetcher
uses the [PyPI JSON API](https://docs.pypi.org/api/json/) to select one wheel whose
archive SHA-256 matches `uv.lock`. It downloads only that wheel's core-metadata
sidecar and verifies the digest advertised by the index, plus its exact name and
version. The receipt records both hashes and the index response hash. It installs
and executes no package code, accepts only the explicit PyPI/file-host origins,
refuses redirects/compression, and bounds package count, response size and fetch
time. Unavailable evidence stays a gap. The sidecar hash comes from the index;
the wheel archive itself is not downloaded or independently inspected.

This metadata covers one locked wheel per missing package. It does not collect
license texts, qualify all platform variants, settle copyleft obligations or
complete third-party notices. Both reports continue to declare
`legal_approved=false` and `notices_complete=false`.

## Actual locked archive notices, 2026-10-05

The pending production candidate's `web.cdx.json` contains **151 packages** and
`python-all-runtime-extras.cdx.json` contains **122 packages**. After platform
selection, the notice collection covers **269 dependencies** and **270 locked
archives**, including the additional Tokenizers source distribution. It emitted
**206 distinct license/notice texts** totaling **879,591 bytes** and retained
**64 publisher proof files** totaling **321,344 bytes**. The independent offline
checker passes with **zero missing selected notice texts or source pointers**.
Replacing the web UI package removed the `react-remove-scroll-bar@2.3.8`
dependency and its unresolved
notice request from the locked tree. The refreshed catalogue binds 19 npm
versions to immutable publisher license text through exact-version registry
`gitHead` evidence. All 19 source-license hashes match the corresponding
locked-archive license bytes. Two further entries bind `@popperjs/core@2.11.8`
and `py-rust-stemmers@0.1.8` to immutable annotated release tags, versioned
manifests and license text. The fresh publisher receipt validates 55 catalogue
entries, and the full archive bundle records **zero source-pointer gaps**.
Publisher pointers do not establish reproducible builds, corresponding-source
or legal approval.

The candidate's retained manifest is the ignored
`.local/ga-next/notices-mui-complete/third-party-notices.json` (SHA-256
`323b17cfb096df766139804684c495be47ea39c29d75e24949ec718f32b3451d`).
Its retained archive receipt is `.local/ga-next/archive-receipt-mui.json` (SHA-256
`9f657ff2f417ea35e2ced5afac30d35ced5467e6f2be4bb04de63e36f75bcaae`).
It binds the checked-in [publisher catalogue](publisher-notices.json) at SHA-256
`5135618ba8f93328f71d20604d948a414527c499a3109dfd51e4fb086feb4a5e`.
The publisher-only receipt is retained at
`.local/ga-next/publisher-notices-mui-complete` (SHA-256
`5fc3828ba7b1309b55296541aacb8bf1d91f2d448a7b0f4eb3b7d0528dd9e1d2`).
These are local candidate receipts, not published artifacts or exact-commit GA
approval. The prior `50803bb` collection covered 414 selected dependencies,
415 archives, 265 texts, eight missing texts and 20 source-pointer gaps; those
counts describe the earlier dependency tree.

Python applicability is explicitly Linux amd64, ordinary CPython 3.14.8. Frozen
requirement markers exclude `colorama`, `httpx2-jsfetch`, `pywin32` and
`win32-setctime`; the manifest records the requirement-file hash and exact marker
environment. A missing compatible wheel never makes a package inapplicable.
Provided archives for marker-excluded packages are inspected but are not required
for selected notice coverage. The complete web lock inventory retains its platform
variants; it is not a statement that every npm binary runs in the Linux image.

[`fetch_release_archives.py`](../scripts/fetch_release_archives.py) fetches only
exact locked wheel/npm archives and explicitly selected locked source
distributions from allowed publisher origins. It refuses redirects and
credential-bearing URLs, verifies the locked digest, and bounds archive bytes,
total bytes, package count and time. It snapshots selection inputs and refuses
input drift before recording the receipt. It installs and executes no package code.
[`collect_release_notices.py`](../scripts/collect_release_notices.py) then operates
offline on those local archives. It copies each bounded archive into a private
anonymous snapshot, then hashes and parses the same bytes. It checks actual
package name/version against the lock owner, refuses traversal, links, duplicate
members and oversized structures, and copies license/NOTICE bytes without
extracting package trees or executing package code. Archive, expanded-content,
individual-text, total-output, inventory and elapsed-time budgets apply.

[`fetch_publisher_notices.py`](../scripts/fetch_publisher_notices.py) separately
fetches the catalogue's immutable publisher texts and their binding proof bytes
under finite download/time limits. The bundle distinguishes texts distributed in
locked archives from supplemental publisher texts. It preserves registry
digest/source associations and exact version-manifest evidence for independent
offline revalidation. Registry attestation consistency checks do **not** verify
cryptographic signatures: `attestation_signatures_verified=false`. Neither an
upstream source pointer nor a publisher notice is a verified corresponding-source
offer or legal approval.

The manifest binds archive texts to their archive SHA-256 and locked digest/URL,
publisher texts to the frozen catalogue and copied proof bytes, and selected
SBOMs, both lockfiles and requirements by hash. Texts are content-addressed.
`--check` independently rebinds the scope, platform, SBOMs, locks, requirements and
catalogue; checks copied text/proof bytes and the exact output inventory; and
rejects dropped dependencies, changed exclusions or missing selected notice
evidence. It needs neither the input archive directory nor the publisher fetch
directory. Collection retains a partial bundle for review; successful collection
does not mean the coverage check passed.

After generating the release SBOMs, run the same commands as the release workflow
from a fresh checkout. Use new output directories. Create the tarball only after
the independent check succeeds. The current candidate passes selected coverage;
later lock or catalogue changes require another collection and check.

```sh
uv export --frozen --no-dev --all-extras --no-extra dev --no-emit-package knowledge-layer \
  --no-hashes --format requirements-txt \
  --output-file .local/release/notice-requirements.txt > /dev/null
uv run --no-sync python scripts/fetch_release_archives.py \
  --sbom-dir .local/release/sbom \
  --scope python-all-runtime-extras.cdx.json --scope web.cdx.json \
  --platform linux/amd64 --requirements .local/release/notice-requirements.txt \
  --source-package tokenizers --output-dir .local/release-tools/notice-archives
uv run --no-sync python scripts/fetch_publisher_notices.py \
  --catalog docs/publisher-notices.json --uv-lock uv.lock \
  --npm-lock web/package-lock.json --output-dir .local/release-tools/publisher-notices
uv run --no-sync python scripts/collect_release_notices.py \
  --sbom-dir .local/release/sbom \
  --scope python-all-runtime-extras.cdx.json --scope web.cdx.json \
  --uv-lock uv.lock --npm-lock web/package-lock.json \
  --platform linux/amd64 --requirements .local/release/notice-requirements.txt \
  --wheel-dir .local/release-tools/notice-archives/wheels \
  --sdist-dir .local/release-tools/notice-archives/sdists \
  --npm-dir .local/release-tools/notice-archives/npm \
  --publisher-catalog docs/publisher-notices.json \
  --publisher-dir .local/release-tools/publisher-notices --output-dir .local/release/notices
uv run --no-sync python scripts/collect_release_notices.py --check --require-source-pointers \
  --sbom-dir .local/release/sbom \
  --scope python-all-runtime-extras.cdx.json --scope web.cdx.json \
  --uv-lock uv.lock --npm-lock web/package-lock.json \
  --platform linux/amd64 --requirements .local/release/notice-requirements.txt \
  --publisher-catalog docs/publisher-notices.json --output-dir .local/release/notices
tar -czf .local/release/third-party-notices.tar.gz -C .local/release/notices .
```

The comparison Graph Lab and its G6, Reactodia, Graph Explorer and Bergos
dependencies were removed from the pending production web lock; its prior
implementation remains in Git history at `50803bb`. The main Knowledge Map now
uses Cytoscape with fCoSE. The web UI package change also removes the scrollbar
package from the shipping lock; it does not supply its missing historical text.
Independently review the 21 current source-pointer gaps. Other
platform variants, image/OS notices,
copyleft and corresponding-source obligations, fixture/source-data rights and
independent compatibility review remain open. Runtime copyright files retained in
the images are separate evidence; this package collector does not inspect OCI
layers or qualify the OS notice inventory. The manifest continues to declare
`legal_approved=false`, `notices_complete=false`,
`container_os_coverage=unresolved` and
`corresponding_source_obligations=independent_review_required` even if its selected
archive/text check passes.
