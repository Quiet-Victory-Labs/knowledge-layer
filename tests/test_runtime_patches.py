from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATCHES = ROOT / "third_party/runtime-patches"


@pytest.fixture
def patches(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("apply_runtime_patches")


def test_reviewed_source_method_license_and_changed_source_refusal(patches):
    original = (PATCHES / "tempfile-rmtree-original.txt").read_bytes()
    assert hashlib.sha256(original).hexdigest() == patches.METHOD_SHA256
    assert hashlib.sha256((PATCHES / "CPython-LICENSE.txt").read_bytes()).hexdigest() == patches.LICENSE_SHA256
    with pytest.raises(ValueError, match="source changed"):
        patches.rewrite_source(b"different upstream runtime", original)
    with pytest.raises(ValueError, match="source changed"):
        patches.rewrite_source(b"different upstream runtime", original + b"\n")


def test_cleanup_uses_existing_descriptor_safe_removal_and_never_permission_repair(patches, tmp_path):
    # Execute only the fixed repository policy in a small synthetic class.
    scope = {"_shutil": shutil}
    exec(b"class SyntheticCleanup:\n" + patches.REPLACEMENT, scope)  # noqa: S102
    target = tmp_path / "temporary"
    (target / "child").mkdir(parents=True)
    (target / "child/file").write_bytes(b"legitimate temporary content")
    scope["SyntheticCleanup"]._rmtree(str(target))
    assert not target.exists()
    assert b"chmod" not in patches.REPLACEMENT and b"_resetperms" not in patches.REPLACEMENT


def test_unavailable_descriptor_safety_refuses_even_when_ignore_errors_requested(patches):
    calls = []
    def removal(*args, **kwargs):
        calls.append((args, kwargs))
    removal.avoids_symlink_attacks = False
    scope = {"_shutil": SimpleNamespace(rmtree=removal)}
    exec(b"class SyntheticCleanup:\n" + patches.REPLACEMENT, scope)  # noqa: S102
    with pytest.raises(RuntimeError, match="Descriptor-safe"):
        scope["SyntheticCleanup"]._rmtree("synthetic", ignore_errors=True)
    assert calls == []


@pytest.fixture
def assembled(patches, monkeypatch, tmp_path):
    root = tmp_path / "assembled"
    source = root / "usr/local/lib/python3.14/tempfile.py"
    source.parent.mkdir(parents=True)
    original = (PATCHES / "tempfile-rmtree-original.txt").read_bytes()
    raw = b"class SyntheticTemporaryDirectory:\n" + original
    source.write_bytes(raw)
    # Synthetic archive mechanics only. Real preimage binding is qualified by
    # the exact-source tests and explicit bounded Linux image control below.
    monkeypatch.setattr(patches, "SOURCE_SHA256", hashlib.sha256(raw).hexdigest())
    return root, source, raw


def apply_fixture(patches, monkeypatch, root):
    with monkeypatch.context() as version:
        version.setattr(patches.sys, "version_info", (3, 14, 8))
        return patches.apply(root, PATCHES)


def test_stale_unchecked_bytecode_is_removed_and_original_version_evidence_stays(patches, monkeypatch, assembled):
    root, source, raw = assembled
    cached = source.parent / "__pycache__/tempfile.cpython-314.pyc"
    cached.parent.mkdir()
    cached.write_bytes(b"synthetic unchecked-hash bytecode")
    metadata = root / "var/lib/dpkg/status"
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(b"Version: original\n")
    receipt = apply_fixture(patches, monkeypatch, root)
    assert not cached.exists()
    assert metadata.read_bytes() == b"Version: original\n"
    assert receipt["package_versions_changed"] is False and receipt["cve_disposition"] == "unresolved"
    assert receipt["modified_sources"][0]["original_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["modified_sources"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert receipt["removed_bytecode"][0]["sha256"] == hashlib.sha256(b"synthetic unchecked-hash bytecode").hexdigest()
    assert (root / "usr/share/knowledge-layer/CPython-LICENSE.txt").read_bytes() == (PATCHES / "CPython-LICENSE.txt").read_bytes()


@pytest.mark.parametrize("target", ["source", "bytecode", "license_directory"])
def test_aliases_refuse_before_source_or_cache_mutation(patches, monkeypatch, assembled, tmp_path, target):
    root, source, raw = assembled
    outside = tmp_path / "outside"
    outside.write_bytes(raw)
    if target == "source":
        source.unlink()
        source.symlink_to(outside)
    elif target == "bytecode":
        cached = source.parent / "__pycache__/tempfile.cpython-314.pyc"
        cached.parent.mkdir()
        cached.symlink_to(outside)
    else:
        directory = root / "usr/share"
        directory.mkdir()
        (directory / "knowledge-layer").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        apply_fixture(patches, monkeypatch, root)
    assert outside.read_bytes() == raw and source.read_bytes() == raw


def test_runtime_release_mismatch_and_double_application_refuse(patches, monkeypatch, assembled):
    root, source, _ = assembled
    with monkeypatch.context() as version:
        version.setattr(patches.sys, "version_info", (3, 14, 9))
        with pytest.raises(ValueError, match="runtime changed"):
            patches.apply(root, PATCHES)
    apply_fixture(patches, monkeypatch, root)
    before = source.read_bytes()
    with pytest.raises(ValueError, match="source changed"):
        apply_fixture(patches, monkeypatch, root)
    assert source.read_bytes() == before


LINUX_CONTROL = '''
import hashlib, importlib.util, json, os, pathlib, shutil, sys, tempfile
source = pathlib.Path(tempfile.__file__)
original = source.read_bytes()
spec = importlib.util.spec_from_file_location("runtime_policy", "/reviewed/scripts/apply_runtime_patches.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
fragment = pathlib.Path("/reviewed/third_party/runtime-patches/tempfile-rmtree-original.txt").read_bytes()
patched = policy.rewrite_source(original, fragment)
assert hashlib.sha256(original).hexdigest() == policy.SOURCE_SHA256
assert shutil.rmtree.avoids_symlink_attacks
scope = {"__name__": "qualified_tempfile"}
exec(compile(patched, "reviewed-tempfile.py", "exec"), scope)
with scope["TemporaryDirectory"](dir="/tmp") as directory:
    target = pathlib.Path(directory)
    (target/"child").mkdir()
    (target/"child/file").write_bytes(b"ordinary temporary content")
assert not target.exists()
with scope["TemporaryDirectory"](dir="/tmp") as directory:
    target = pathlib.Path(directory)
    child = target/"read-only"
    child.mkdir()
    (child/"file").write_bytes(b"ordinary read-only content")
    child.chmod(0o500)
    try:
        scope["TemporaryDirectory"]._rmtree(directory)
    except PermissionError:
        assert child.stat().st_mode & 0o777 == 0o500
    else:
        raise AssertionError("Permission repair must refuse for the non-root runtime")
    finally:
        child.chmod(0o700)
print(json.dumps({"result":"passed", "source_sha256":policy.SOURCE_SHA256,
                  "patched_sha256":hashlib.sha256(patched).hexdigest(), "source_version":sys.version.split()[0],
                  "ordinary_cleanup":True,"inaccessible_cleanup_refused":True,"runtime_not_modified":True}))
'''


@pytest.mark.skipif(not os.environ.get("KNOWLEDGE_PATCH_SOURCE_IMAGE"), reason="Explicit reviewed source image required")
def test_exact_linux_source_and_legitimate_cleanup_with_no_runtime_mutation():
    result = subprocess.run(["docker", "run", "--rm", "-i", "--network", "none", "--read-only",
        "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", "128m", "--cpus", "0.5", "--pids-limit", "32", "--tmpfs", "/tmp:size=16m,mode=1777",
        "--mount", f"type=bind,src={ROOT},dst=/reviewed,readonly", "--entrypoint", "python",
        os.environ["KNOWLEDGE_PATCH_SOURCE_IMAGE"], "-"], input=LINUX_CONTROL, text=True,
        capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["result"] == "passed" and receipt["source_version"] == "3.14.8"
    assert receipt["ordinary_cleanup"] and receipt["inaccessible_cleanup_refused"] and receipt["runtime_not_modified"]
