"""Remove unsupported permission repair from the reviewed Linux CPython runtime.

Original package identities and scanner findings are retained. This is a bounded
Knowledge Layer cleanup policy, not an upstream release or CVE disposition.
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

SOURCE_COMMIT = "8e6e75d9102e39bed2a2b279203a396741180f12"
SOURCE_SHA256 = "3714372b63f6bc05a5273425d446f6ad3a2112a659c71c321bb4575fd082d634"
METHOD_SHA256 = "48fd4fb808ef3455995a5f94b5770596d85b3c9512517cec4569a9bc7fd46771"
LICENSE_SHA256 = "b0e25a78cffb43f4d92de8b61ccfa1f1f98ecbc22330b54b5251e7b6ba010231"
MAX_SOURCE = 256 * 1024
REPLACEMENT = b'''    @classmethod
    def _rmtree(cls, name, ignore_errors=False, repeated=False):
        # Knowledge Layer supports descriptor-safe Linux cleanup only. Automatic
        # permission/flag repair can re-resolve paths outside the cleanup tree.
        # Leave inaccessible files for explicit operator cleanup instead.
        if not _shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("Descriptor-safe temporary cleanup is required")
        _shutil.rmtree(name, ignore_errors=ignore_errors)

'''


def read_regular(path: Path, limit=MAX_SOURCE) -> bytes:
    if any(parent.is_symlink() for parent in (path, *path.absolute().parents)):
        raise ValueError("Runtime patch symlinks are refused")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Runtime patch requires one regular file")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Runtime patch byte budget exceeded")
    return raw


def rewrite_source(raw: bytes, original: bytes) -> bytes:
    if (hashlib.sha256(raw).hexdigest() != SOURCE_SHA256
            or hashlib.sha256(original).hexdigest() != METHOD_SHA256 or raw.count(original) != 1):
        raise ValueError("Reviewed CPython cleanup source changed")
    return raw.replace(original, REPLACEMENT)


def apply(root: Path, patches: Path) -> dict:
    if sys.version_info[:3] != (3, 14, 8):
        raise ValueError("Reviewed CPython cleanup runtime changed")
    resolved = root.resolve(strict=True)
    original = read_regular(patches / "tempfile-rmtree-original.txt")
    license_raw = read_regular(patches / "CPython-LICENSE.txt", 64 * 1024)
    if hashlib.sha256(license_raw).hexdigest() != LICENSE_SHA256:
        raise ValueError("Reviewed CPython license changed")
    updates, cached = [], []
    for relative in ("usr/local/lib/python3.14", "usr/lib/python3.14", "app/.venv/lib/python3.14"):
        directory = resolved / relative
        path = directory / "tempfile.py"
        if not path.exists() and not path.is_symlink():
            continue
        raw = read_regular(path)
        patched = rewrite_source(raw, original)
        caches = [directory / "tempfile.pyc", *(directory / "__pycache__").glob("tempfile.*.pyc")]
        if len(caches) > 64 or len(cached) + len(caches) > 128:
            raise ValueError("Runtime bytecode inventory budget exceeded")
        for cache in caches:
            if cache.exists() or cache.is_symlink():
                payload = read_regular(cache)
                cached.append((cache, hashlib.sha256(payload).hexdigest()))
        updates.append((path, raw, patched))
    if not updates:
        raise ValueError("Reviewed CPython cleanup source is missing")
    # Validate every source/cache and license destination before mutation.
    license_path = resolved / "usr/share/knowledge-layer/CPython-LICENSE.txt"
    if any(parent.is_symlink() for parent in (license_path, *license_path.parents)):
        raise ValueError("Runtime license destination escapes the assembled root")
    if license_path.exists() and read_regular(license_path, 64 * 1024) != license_raw:
        raise ValueError("Runtime license destination changed")
    for path, raw, _ in updates:
        if read_regular(path) != raw:
            raise ValueError("Runtime cleanup source changed during validation")
    for path, _, patched in updates:
        path.write_bytes(patched)
    for cache, _ in cached:
        cache.unlink()
    license_path.parent.mkdir(parents=True, exist_ok=True)
    license_path.write_bytes(license_raw)
    return {"policy": "descriptor_safe_cleanup_without_permission_repair", "upstream_release": "3.14.8",
        "source_commit": SOURCE_COMMIT, "cve_disposition": "unresolved", "package_versions_changed": False,
        "modified_sources": [{"path": "/" + path.relative_to(resolved).as_posix(),
            "original_sha256": hashlib.sha256(raw).hexdigest(), "sha256": hashlib.sha256(patched).hexdigest()}
            for path, raw, patched in updates],
        "removed_bytecode": [{"path": "/" + path.relative_to(resolved).as_posix(), "sha256": checksum}
                             for path, checksum in cached],
        "license_sha256": LICENSE_SHA256,
        "limitations": ["Inaccessible temporary files can require explicit operator cleanup.",
                        "This policy does not provide a vendor package update or suppress scanner findings."]}
