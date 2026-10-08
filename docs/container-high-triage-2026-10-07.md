# High container finding triage, 2026-10-07

This is a technical review of the 13 High **package matches** in the saved
Linux amd64 application image. It is not a release decision, VEX statement,
scanner suppression, or approval for another build. The source was the
unsuppressed Grype JSON at
`.local/ga-current/container-final-native-vulnerabilities.json` (SHA-256
`0c03c1dac646493834cd38bd631f4e161dd341c13d48165f4800beb9675a9278`).
Its target is the saved OCI archive `container-final.oci.tar`, index digest
`sha256:7d7d1136800e0088b7cb777edfe995b6e2309b47e74c901667aaeb7e84d17526`
and Linux amd64 manifest
`sha256:3bec0bbc9412bf69da928c15b3bfd529e3d4bfd31136b6a79fbee0f1f21b68dc`.
The image was built during the October 7 local release-workflow reproduction;
its saved build receipt does not attest a source commit. The earlier documented
reproduction began at `31f59ae`; rebuild and rescan the exact proposed release
commit. The all-extras
scan has the same 13 advisory/package identities. The scanner continues to
report all matches, and all 13 remain **unapproved** under the
[container finding policy](container-security-policy.md).

Static inspection of the OCI filesystem found 6,389 paths. It retains
`libc.so.6`, `libresolv.so.2`, `libstdc++.so.6.0.33`, `libuuid.so.1.3.0`, and
`libz.so.1.3.1`; it contains no `mount`, `nsenter`, `umount`, or `/etc/fstab`.
The runtime receipt binds those libraries to the original Debian binary and
source package versions. Static ELF dynamic-symbol inspection found defined
`strfmon`/`strfmon_l` in libc and `__fp_nquery` in libresolv; libstdc++ has an
undefined `posix_memalign` import and no `aligned_alloc` import. A search of
application source and 44 shipped native
wheel files found no literal `__gnu_pbds`, `binary_heap_tag`, `erase_if`,
`strfmon`, or `fp_nquery` use. Inlined or stripped compiled C++ and indirect
native callers remain possible. No application or exploit was run for this
triage. The [repository security policy](../SECURITY.md) limits the current
product to a local demonstration and OIDC pilot, so this review does not
qualify a source-ACL-sensitive enterprise installation.

A second static pass inspected the dynamic symbol tables of **all** 413 ELF
files in the base image and 530 in the all-extras image. None imports
`strfmon`, `strfmon_l`, `fp_nquery`, or `ns_printrr`. No literal occurrence of
these names was found in application or Python-runtime files (4,272 base and
11,862 all-extras files). This narrows both glibc exposure stories, but it
does not rule out a constructed dynamic lookup or code loaded by a customer
after image build.

`not_actionable` below is a **static finding verdict for this exact image**, not
an approved risk decision. `needs_review` names the smallest unresolved fact.
Rows preserve the order of the saved scanner inputs. Rank applies only within
the five `needs_review` rows (1 = first to investigate); the other rows have no
rank.

| Scan row | Advisory and matched package | Static verdict | Rank | Evidence and next proof |
| ---: | --- | --- | ---: | --- |
| 0 | [CVE-2026-85091](https://security-tracker.debian.org/tracker/CVE-2026-85091), `zlib1g 1:1.3.dfsg+really1.3.1-1+b1` | `not_actionable` (high) | — | [Debian's installed source](https://sources.debian.org/src/zlib/1%3A1.3.dfsg%2Breally1.3.1-1/gzwrite.c/) has no `gz_vacate`; [upstream introduced](https://github.com/madler/zlib/commit/81cc0bebedd935daeb81b0b6e475d8786b51af3d) the affected nonblocking-write path in 1.3.1.2. The retained binary reports 1.3.1. Recheck if source or image changes; Debian's broad source-package tracker still calls Trixie vulnerable. |
| 3 | [CVE-2026-5435](https://security-tracker.debian.org/tracker/CVE-2026-5435), `libc6 2.41-12+deb13u4` | `needs_review` (medium) | 1 | The affected deprecated DNS printing path is retained: `libresolv.so.2` exports `__fp_nquery`. No shipped ELF imports it and no application/runtime file names it. Establish whether an indirect or customer-loaded caller can reach it with attacker-influenced TSIG data and a susceptible buffer. No supported Trixie fix is listed. |
| 5 | [CVE-2026-95619](https://security-tracker.debian.org/tracker/CVE-2026-95619), `gcc-14-base 14.2.0-19` | `not_actionable` (medium) | — | Source-package fanout: this metadata package has no aligned `operator new` implementation. The shipped `libstdc++` imports `posix_memalign` and does not import `aligned_alloc`; [upstream's fix](https://github.com/gcc-mirror/gcc/commit/59d235ffa5a69231eb42e5290d52dc8c90d28b7a) says that `posix_memalign` path is unaffected. Re-evaluate with a changed ABI or library. |
| 6 | [CVE-2026-95619](https://security-tracker.debian.org/tracker/CVE-2026-95619), `libgcc-s1 14.2.0-19` | `not_actionable` (medium) | — | The affected implementation is in libstdc++, not libgcc; the exact Linux library imports `posix_memalign` and not `aligned_alloc`. Same architecture and rebuild limit as row 5. |
| 7 | [CVE-2026-95619](https://security-tracker.debian.org/tracker/CVE-2026-95619), `libstdc++6 14.2.0-19` | `not_actionable` (medium) | — | This is the actual aligned-allocation library, but the saved Linux amd64 ELF imports `posix_memalign` and has no `aligned_alloc` import; upstream identifies the former as outside the affected rounding branch. This static import evidence needs an independent reviewer before any scoped decision. |
| 9 | [CVE-2026-19499](https://security-tracker.debian.org/tracker/CVE-2026-19499), `libc6 2.41-12+deb13u4` | `needs_review` (medium) | 2 | The retained libc contains `strfmon_l`, but no shipped ELF imports it and no application/runtime file names it. Establish an indirect caller that can apply right-justified width padding to an undersized destination with attacker-influenced values, or prove all supported callers absent. No supported Trixie fix is listed. |
| 14 | [CVE-2026-102010](https://security-tracker.debian.org/tracker/CVE-2026-102010), `gcc-14-base 14.2.0-19` | `needs_review` (low) | 5 | [Upstream changed a C++ template header](https://github.com/gcc-mirror/gcc/commit/aaa8351f4d2e636f9680a1f0a8ebc2f0a60611e6), absent from the runtime root; the source-package match does not prove vulnerable code in this metadata package. Prove whether any shipped wheel has already compiled the affected PBDS binary-heap `erase_if` path. |
| 15 | [CVE-2026-102010](https://security-tracker.debian.org/tracker/CVE-2026-102010), `libgcc-s1 14.2.0-19` | `needs_review` (low) | 4 | Same header-only source-package fanout. The affected template is not a libgcc API, but an installed native wheel could embed it; inspect the build provenance or symbols before closing this row. |
| 16 | [CVE-2026-102010](https://security-tracker.debian.org/tracker/CVE-2026-102010), `libstdc++6 14.2.0-19` | `needs_review` (low) | 3 | Runtime headers are absent and no literal `__gnu_pbds`/`erase_if` marker was found in 44 native wheel files; optimized template instantiations need not retain names. Establish whether any shipped native wheel calls the vulnerable `erase_if` path. |
| 18 | [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642), `libuuid1 2.41.5-0+deb13u1` | `not_actionable` (high) | — | The advisory requires the util-linux `mount` command and post-mount hooks. The saved filesystem has no `mount` command or `/etc/fstab`; retained `libuuid.so.1.3.0` is not the affected command. Recheck if operators extend the image. |
| 22 | [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408), `libuuid1 2.41.5-0+deb13u1` | `not_actionable` (high) | — | The advisory requires `nsenter --join-cgroup` and a privileged invocation. The final image has no `nsenter` command; the library match is util-linux source-package fanout. |
| 27 | [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410), `libuuid1 2.41.5-0+deb13u1` | `not_actionable` (high) | — | Restricted bind mounting requires util-linux `mount`, an fstab entry and a privileged/suid path. Neither command nor fstab is in the final image. |
| 29 | [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409), `libuuid1 2.41.5-0+deb13u1` | `not_actionable` (high) | — | The `X-mount.subdir` attack needs util-linux `mount` and an fstab-authorized local user; both are absent from this image. Host mount tools remain the host operator's responsibility. |

The [CISA known-exploited catalog](https://www.cisa.gov/known-exploited-vulnerabilities-catalog)
feed published 2026-10-04 contains none of these nine advisory IDs; catalog
absence is not a safety finding. Debian Trixie publishes no compatible fixed
version for the two glibc advisories or either GCC advisory at this review.
Changing to an unstable package would cross the supported release boundary.
The optional vendor image has different findings and lacks the Snowflake extra;
it is not a drop-in resolution for the shipped application profile.

For the next exact-commit scan, an independent reviewer can bind any defensible
`not_affected` decision to each advisory, image, namespace, exact package URL,
review date and expiry. The five `needs_review` rows require native caller
evidence, an effective mitigation, or a supported patched package. Do not copy
these verdicts into `security/container-risk-decisions.json` without that review.
