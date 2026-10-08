# Container finding review queue, 2026-10-05

This is a **historical review worklist**, not a VEX document or a release
exception. The
Linux amd64 application base and all-extra scans each have 43 unsuppressed raw
matches. Their finding identities agree, so the 43 application rows below cover
both profiles. The separately shipped PostgreSQL derivative has four raw
matches. All **47 review rows remain `needs_review`**; 14 are High, 20 Medium,
three Low and ten Negligible. The strict release workflow still fails on every
known finding, including Negligible, and `scripts/release_manifest.py` also
requires zero matches and zero ignored findings. No VEX is approved or loaded.

The application scans are from the headless-terminal-trim OCI builds that
preceded commit `568597b2c723b03938b7c129710639023288a1c3`; the nested OCI
manifests identify `linux/amd64` images. A later commit changed only
`.secrets.baseline`. The PostgreSQL derivative scan is bound to
`8253e418a55d28bd61cf05b6ed4d2bd5a6401740`, per its retained receipt.
Neither older image scan is an exact build of the next release commit. Rebuild
and rescan **both** images at that commit before any disposition or publication.
Alpine 3.24 subsequently published `zlib=1.3.2-r1` and OpenSSL `3.5.9-r0`.
The October 7 local PostgreSQL candidate pins these stable updates and passes
three nonroot runtime tests on both arm64 and amd64. Native Syft SBOMs bind
each architecture's image ID to a Grype scan with three unsuppressed
BusyBox-family CVE-2025-60876 matches using the 2026-10-07 advisory database.
The arm64 and amd64 raw JSON SHA-256 values are
`6cc2501243b95d18adaec1114a0d1cb8234084d97e6b3088ca2e6c661f6fad4e`
and `7ebcadeaacd02b9ebb9c7c7dacdf2e50027536e15bdcf56a699df929f10e82de`,
respectively. The four-row table below remains bound to the historical scan;
these local builds did not stand in for a release-workflow OCI archive or silently
approve any review verdict. A later local reproduction of the release workflow's
Linux amd64 OCI builds at `31f59ae` produced **43 application and three
PostgreSQL raw matches**, with zero ignored, using the same October 7 advisory
database. Its native raw Grype JSON SHA-256 values are
`4c857c19344aba9e39420855ddfd41b6fa0bf49ec3fcd2cd08f88bd2cec7cd62`
and `7cbf16244f62932be90eb7a2c10fa668c0adcc31b9563c2f10d02c46b7891b18`.
The release manifest's zero-match check refused both. No VEX or exception was
created. See [container-runtime.md](container-runtime.md).

| Scan | SHA-256 of raw Grype JSON | OCI identity or receipt | Raw |
| --- | --- | --- | ---: |
| Application base | `3904d117cbc5b254662d5aade089984735dac3fb0ba2ca4a30a7a6620b43b560` | OCI index `sha256:d772a2cda6b08b69be6793faaf9cf758584f511ad95795b94a51e91d7828f151` | 43 |
| Application all extras | `d65268921f3edc111b19b6f87296314589db320fa3a489acc7b32920aa4c9dd8` | OCI index `sha256:297bc3bb6fdba7b9989ef76803df010ab70d66126504510029713e3aecc0941e` | 43 |
| PostgreSQL derivative | `baf579eb66af73902e4d8c98bf77aa30347d37c03a6053a23bb16fe6cfc3b376` | OCI digest `sha256:93690fe7d913ffd48cab0d12b674bbaf1b6eb65a8289e3384bdcd3cbc0af02c9` | 4 |

The ignored local review queue can be regenerated without network or package
execution:

```sh
python3 scripts/container_finding_review.py \
  --app-base .local/terminal-trim-scan/base-amd64.vulnerabilities.json \
  --app-extras .local/terminal-trim-scan/extras-amd64.vulnerabilities.json \
  --postgres .local/ga-next/postgres-scan-8253e41/vulnerabilities.json \
  --output .local/release-review-20261005/container-finding-queue.json
```

The script refuses suppressed matches and profile disagreement. Its rows record
the exact advisory namespace, package URL/version, scanner fix state and source
link, while setting every verdict to `needs_review` and every VEX status to
null. The Grype database on these scans was built 2026-10-05 06:45:38 UTC.
Scanner `fixed` means a fix was reported somewhere; it does **not** mean the
installed image contains it.

## Required review tracks

| Track | Evidence needed before a disposition |
| --- | --- |
| C++ runtime | Inspect the shipped `libstdc++` implementation and every native wheel/load path for the named `erase_if` or aligned allocation behavior; install a supported patched runtime if available. GCC source-package matches on `gcc-14-base` and `libgcc-s1` are not automatically the same executable sink. |
| glibc | Map each advisory's exact affected function and exploit prerequisites to shipped ELF symbols and runtime callers. `strfmon` and deprecated resolver-printing code remain in the Debian source; a weak exposure story is not proof of absence. Named tool and architecture-only cases need separate exact-file/architecture evidence. |
| util-linux | Resolve each `libuuid1` source-package match to its named command or library code, prove whether the affected file and call path are in the final root, and retest after any package removal. The library is retained. |
| zlib | Compare the installed source and patch set with the upstream affected range and fix; test the loaded `gz*` and CRC call paths. Debian's Trixie source and the historical Alpine `1.3.2-r0` scan remain open; Alpine's new `1.3.2-r1` backport needs an exact-image recheck. |
| Python | Check exact CPython 3.14.8 standard-library bytes and the shipped targeted `tempfile` repair. Verify whether `poplib` remains in the final root and whether `tarfile` fallback behavior can be reached. A pending upstream backport or removed module alone is not an approved disposition. |
| SQLite/bzip2 | Identify the exact affected parser or extension path, compiled library bytes, direct and transitive callers, and hostile-input conditions before deciding applicability. |
| BusyBox | The PostgreSQL parent still needs BusyBox shell/SSL-client packages. Verify whether its `wget` applet contains the request-target issue and use a supported Alpine backport; three APK package records are not three independent exploits. |

Current upstream evidence: Debian still lists shipped Trixie
[glibc `strfmon`](https://security-tracker.debian.org/tracker/CVE-2026-19499),
[glibc resolver printing](https://security-tracker.debian.org/tracker/CVE-2026-5435),
[GCC aligned allocation](https://security-tracker.debian.org/tracker/CVE-2026-95619),
and [zlib](https://security-tracker.debian.org/tracker/CVE-2026-85091) as
vulnerable. Alpine's [stable zlib](https://raw.githubusercontent.com/alpinelinux/aports/3.24-stable/main/zlib/APKBUILD)
and [BusyBox](https://raw.githubusercontent.com/alpinelinux/aports/3.24-stable/main/busybox/APKBUILD)
recipes identify the currently published versions; Alpine zlib advanced from
the scanned `1.3.2-r0` to `1.3.2-r1`. These sources establish package
status; they do not establish whether a given application call path is reachable.

## Match-by-match queue

Each row below names one distinct advisory/package match. Application rows
occur in **both** scanned application profiles. All rows have verdict
`needs_review` and no VEX status. The track identifies the next source/control/
sink proof, not a conclusion.

| Image | Advisory | Installed package | Severity | Scanner fix | Track |
| --- | --- | --- | --- | --- | --- |
| App ×2 | [CVE-2026-102010](https://security-tracker.debian.org/tracker/CVE-2026-102010) | `gcc-14-base 14.2.0-19` | High | not-fixed | C++ runtime |
| App ×2 | [CVE-2026-102010](https://security-tracker.debian.org/tracker/CVE-2026-102010) | `libgcc-s1 14.2.0-19` | High | not-fixed | C++ runtime |
| App ×2 | [CVE-2026-102010](https://security-tracker.debian.org/tracker/CVE-2026-102010) | `libstdc++6 14.2.0-19` | High | not-fixed | C++ runtime |
| App ×2 | [CVE-2026-19499](https://security-tracker.debian.org/tracker/CVE-2026-19499) | `libc6 2.41-12+deb13u4` | High | wont-fix | glibc |
| App ×2 | [CVE-2026-5435](https://security-tracker.debian.org/tracker/CVE-2026-5435) | `libc6 2.41-12+deb13u4` | High | wont-fix | glibc |
| App ×2 | [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642) | `libuuid1 2.41.5-0+deb13u1` | High | wont-fix | util-linux |
| App ×2 | [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408) | `libuuid1 2.41.5-0+deb13u1` | High | wont-fix | util-linux |
| App ×2 | [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409) | `libuuid1 2.41.5-0+deb13u1` | High | wont-fix | util-linux |
| App ×2 | [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410) | `libuuid1 2.41.5-0+deb13u1` | High | wont-fix | util-linux |
| App ×2 | [CVE-2026-85091](https://security-tracker.debian.org/tracker/CVE-2026-85091) | `zlib1g 1:1.3.dfsg+really1.3.1-1+b1` | High | not-fixed | zlib |
| App ×2 | [CVE-2026-95619](https://security-tracker.debian.org/tracker/CVE-2026-95619) | `gcc-14-base 14.2.0-19` | High | not-fixed | C++ runtime |
| App ×2 | [CVE-2026-95619](https://security-tracker.debian.org/tracker/CVE-2026-95619) | `libgcc-s1 14.2.0-19` | High | not-fixed | C++ runtime |
| App ×2 | [CVE-2026-95619](https://security-tracker.debian.org/tracker/CVE-2026-95619) | `libstdc++6 14.2.0-19` | High | not-fixed | C++ runtime |
| App ×2 | [CVE-2025-15367](https://nvd.nist.gov/vuln/detail/CVE-2025-15367) | `python 3.14.8` | Medium | fixed | Python |
| App ×2 | [CVE-2026-12345](https://nvd.nist.gov/vuln/detail/CVE-2026-12345) | `python 3.14.8` | Medium | fixed | Python |
| App ×2 | [CVE-2026-18374](https://security-tracker.debian.org/tracker/CVE-2026-18374) | `libc6 2.41-12+deb13u4` | Medium | wont-fix | glibc |
| App ×2 | [CVE-2026-19542](https://security-tracker.debian.org/tracker/CVE-2026-19542) | `libc6 2.41-12+deb13u4` | Medium | wont-fix | glibc |
| App ×2 | [CVE-2026-27171](https://security-tracker.debian.org/tracker/CVE-2026-27171) | `zlib1g 1:1.3.dfsg+really1.3.1-1+b1` | Medium | wont-fix | zlib |
| App ×2 | [CVE-2026-3184](https://security-tracker.debian.org/tracker/CVE-2026-3184) | `libuuid1 2.41.5-0+deb13u1` | Medium | wont-fix | util-linux |
| App ×2 | [CVE-2026-42250](https://security-tracker.debian.org/tracker/CVE-2026-42250) | `libbz2-1.0 1.0.8-6` | Medium | wont-fix | SQLite/bzip2 |
| App ×2 | [CVE-2026-50812](https://security-tracker.debian.org/tracker/CVE-2026-50812) | `libsqlite3-0 3.46.1-7+deb13u2` | Medium | wont-fix | SQLite/bzip2 |
| App ×2 | [CVE-2026-50813](https://security-tracker.debian.org/tracker/CVE-2026-50813) | `libsqlite3-0 3.46.1-7+deb13u2` | Medium | wont-fix | SQLite/bzip2 |
| App ×2 | [CVE-2026-6238](https://security-tracker.debian.org/tracker/CVE-2026-6238) | `libc6 2.41-12+deb13u4` | Medium | wont-fix | glibc |
| App ×2 | [CVE-2026-6791](https://security-tracker.debian.org/tracker/CVE-2026-6791) | `libc6 2.41-12+deb13u4` | Medium | wont-fix | glibc |
| App ×2 | [CVE-2026-77117](https://security-tracker.debian.org/tracker/CVE-2026-77117) | `libc6 2.41-12+deb13u4` | Medium | wont-fix | glibc |
| App ×2 | [CVE-2026-80489](https://security-tracker.debian.org/tracker/CVE-2026-80489) | `libc6 2.41-12+deb13u4` | Medium | wont-fix | glibc |
| App ×2 | [CVE-2026-8674](https://security-tracker.debian.org/tracker/CVE-2026-8674) | `libc6 2.41-12+deb13u4` | Medium | wont-fix | glibc |
| App ×2 | [CVE-2026-86805](https://security-tracker.debian.org/tracker/CVE-2026-86805) | `libc6 2.41-12+deb13u4` | Medium | wont-fix | glibc |
| App ×2 | [CVE-2026-87910](https://nvd.nist.gov/vuln/detail/CVE-2026-87910) | `python 3.14.8` | Medium | fixed | Python |
| App ×2 | [CVE-2026-89092](https://security-tracker.debian.org/tracker/CVE-2026-89092) | `libc6 2.41-12+deb13u4` | Medium | wont-fix | glibc |
| App ×2 | [CVE-2026-6368](https://security-tracker.debian.org/tracker/CVE-2026-6368) | `libc6 2.41-12+deb13u4` | Low | wont-fix | glibc |
| App ×2 | [CVE-2026-95818](https://security-tracker.debian.org/tracker/CVE-2026-95818) | `libc6 2.41-12+deb13u4` | Low | wont-fix | glibc |
| App ×2 | [CVE-2026-97399](https://security-tracker.debian.org/tracker/CVE-2026-97399) | `libc6 2.41-12+deb13u4` | Low | wont-fix | glibc |
| App ×2 | [CVE-2010-4756](https://security-tracker.debian.org/tracker/CVE-2010-4756) | `libc6 2.41-12+deb13u4` | Negligible | not-fixed | glibc |
| App ×2 | [CVE-2018-20796](https://security-tracker.debian.org/tracker/CVE-2018-20796) | `libc6 2.41-12+deb13u4` | Negligible | not-fixed | glibc |
| App ×2 | [CVE-2019-1010022](https://security-tracker.debian.org/tracker/CVE-2019-1010022) | `libc6 2.41-12+deb13u4` | Negligible | not-fixed | glibc |
| App ×2 | [CVE-2019-1010023](https://security-tracker.debian.org/tracker/CVE-2019-1010023) | `libc6 2.41-12+deb13u4` | Negligible | not-fixed | glibc |
| App ×2 | [CVE-2019-1010024](https://security-tracker.debian.org/tracker/CVE-2019-1010024) | `libc6 2.41-12+deb13u4` | Negligible | not-fixed | glibc |
| App ×2 | [CVE-2019-1010025](https://security-tracker.debian.org/tracker/CVE-2019-1010025) | `libc6 2.41-12+deb13u4` | Negligible | not-fixed | glibc |
| App ×2 | [CVE-2019-9192](https://security-tracker.debian.org/tracker/CVE-2019-9192) | `libc6 2.41-12+deb13u4` | Negligible | not-fixed | glibc |
| App ×2 | [CVE-2021-45346](https://security-tracker.debian.org/tracker/CVE-2021-45346) | `libsqlite3-0 3.46.1-7+deb13u2` | Negligible | not-fixed | SQLite/bzip2 |
| App ×2 | [CVE-2022-0563](https://security-tracker.debian.org/tracker/CVE-2022-0563) | `libuuid1 2.41.5-0+deb13u1` | Negligible | not-fixed | util-linux |
| App ×2 | [CVE-2025-70873](https://security-tracker.debian.org/tracker/CVE-2025-70873) | `libsqlite3-0 3.46.1-7+deb13u2` | Negligible | not-fixed | SQLite/bzip2 |
| PostgreSQL | [CVE-2026-85091](https://nvd.nist.gov/vuln/detail/CVE-2026-85091) | `zlib 1.3.2-r0` | High | unknown | zlib |
| PostgreSQL | [CVE-2025-60876](https://nvd.nist.gov/vuln/detail/CVE-2025-60876) | `busybox 1.37.0-r31` | Medium | unknown | BusyBox |
| PostgreSQL | [CVE-2025-60876](https://nvd.nist.gov/vuln/detail/CVE-2025-60876) | `busybox-binsh 1.37.0-r31` | Medium | unknown | BusyBox |
| PostgreSQL | [CVE-2025-60876](https://nvd.nist.gov/vuln/detail/CVE-2025-60876) | `ssl_client 1.37.0-r31` | Medium | unknown | BusyBox |

For a proposed `not_affected` VEX, the reviewer must bind the *new exact image
digest*, OS/architecture, package URL/version, advisory namespace/ID, affected
symbol or file, source-to-sink/reachability evidence, negative controls, and
the independently reviewed decision. Record a validity/recheck condition for
new images, package versions, scanner data or changed source paths. A package
being present, a missing command name, nonroot defaults or a low CVSS score by
itself cannot justify `not_affected`. Until that review exists, leave the raw
scanner gate unchanged and keep this queue outside the release artifacts.
