# Container finding policy

The release builds and scans both shipped Linux amd64 images. It retains the
complete, unsuppressed Grype JSON, the installed software bills of materials
(SBOMs), source-controlled decisions and a derived evaluation as signed release
artifacts. A raw scanner match is a review signal, not proof that an attacker
can reach vulnerable code. Zero raw matches are **not** required.

| Finding | Release behavior |
| --- | --- |
| Critical, High or Unknown severity without a matching decision | Block |
| Critical, High or Unknown with a current, scoped decision | Allow and retain the match and decision |
| Medium, Low or Negligible | Track in the signed evaluation; fix and review by normal maintenance priority |
| Any ignored/suppressed match, invalid scan or stale decision | Block |

Scanner `fix.state` is recorded, but is not a safe release decision by itself.
A Python fix listed on another branch, for example, may be unusable in the
installed branch. A known exploited vulnerability or reachable serious flaw
should be remediated or mitigated urgently even if the scanner rates it Medium.
The automated gate does **not** consult a live known-exploited catalog or prove
reachability; the exact-commit security review must account for those limits.
The release policy does not certify an image as vulnerability-free.

Maintainers place decisions in
[`security/container-risk-decisions.json`](../security/container-risk-decisions.json)
before signing the source tag. Each decision is tied to one image, advisory ID
and namespace, and the exact installed package URL (`purl`). It must include a
specific rationale, evidence reference, reviewer, review date and expiry no
more than 180 days later. Allowed dispositions are `not_affected`, `mitigated`
and `risk_accepted`. A new or changed package cannot inherit an old decision.
An expired or orphaned decision blocks preparation. The reviewer must verify
the evidence and residual risk; the machine checks scope, shape and dates,
not the truth of the rationale. Critical findings warrant a documented fix or
effective mitigation before approval; risk acceptance requires explicit
scrutiny in the existing release review.

Example shape, using a synthetic advisory and package:

```json
{
  "schema_version": "container-risk-decisions-v1",
  "decisions": [
    {
      "image": "application",
      "namespace": "debian:distro:debian:13",
      "advisory": "CVE-2099-10001",
      "purl": "pkg:deb/debian/libexample@1.0",
      "disposition": "not_affected",
      "rationale": "The affected function is absent from this exact installed build.",
      "evidence": ["docs/container-runtime.md"],
      "reviewer": "reviewer-handle",
      "reviewed_on": "2099-01-01",
      "expires_on": "2099-03-01"
    }
  ]
}
```

The example is a format illustration, not an approved exception. Do not copy
it into the live decisions file. Run
`python3 scripts/release_manifest.py RELEASE_DIRECTORY --check-container-scans`
after writing the two raw scan JSON files and copying the decisions file into
that directory. It writes `container-risk-evaluation.json` even when serious
findings block. The release manifest re-evaluates and binds all four files;
publication rechecks them after download. Scanner failure or a missing report
cannot become a passing evaluation.

The October 7 local OCI scans have 43 application matches, including 13 High,
and three PostgreSQL Medium BusyBox-family matches. No decision is approved.
The 13 High matches still block preparation until scoped reviews are recorded.
The three Medium matches remain visible in the signed report and review queue.
Those local scans do not substitute for a new exact-commit release scan.
