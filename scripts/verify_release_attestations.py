"""Verify each release file against the exact repository, workflow, tag and commit."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from release_manifest import verify


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    try:
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository)
                or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", args.tag)
                or not re.fullmatch(r"[a-f0-9]{40}", args.commit)):
            raise ValueError("Invalid release identity")
        manifest = verify(args.directory, repository=args.repository)
        if (not manifest["complete"] or manifest["commit"] != args.commit
                or args.tag != "v" + manifest["version"]):
            raise ValueError("Incomplete or mismatched release")
        workflow = f"{args.repository}/.github/workflows/release.yaml"
        identity = f"https://github.com/{workflow}@refs/tags/{args.tag}"
        files = [item["path"] for item in manifest["artifacts"]] + ["release-manifest.json", "SHA256SUMS"]
        for name in files:
            subprocess.run(["gh", "attestation", "verify", str(args.directory / name),
                # Exact SAN already pins repository, workflow and tag. The CLI
                # refuses SAN together with signer-workflow/signer-repo flags.
                "--repo", args.repository, "--cert-identity", identity,
                "--cert-oidc-issuer", "https://token.actions.githubusercontent.com",
                "--source-ref", "refs/tags/" + args.tag,
                "--source-digest", args.commit, "--signer-digest", args.commit,
                "--deny-self-hosted-runners"], check=True, capture_output=True, timeout=120)
        print(json.dumps({"result": "verified", "files": len(files), "repository": args.repository,
                          "tag": args.tag, "commit": args.commit}))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print("Release signature/provenance verification failed; no publication permitted")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
