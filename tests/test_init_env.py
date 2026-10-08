"""The local Compose initializer must never leave readable credentials behind."""

import importlib.util
import os
import stat
from pathlib import Path

import pytest


def _initializer():
    path = Path(__file__).resolve().parents[1] / "scripts" / "init_env.py"
    spec = importlib.util.spec_from_file_location("knowledge_init_env_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_credentials_are_owner_only_on_create_and_existing_file(tmp_path):
    module = _initializer()
    module.ENV_PATH = tmp_path / ".env"
    previous_umask = os.umask(0o022)
    try:
        module.main()
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(module.ENV_PATH.stat().st_mode) == 0o600
    assert all(f"{name}=" in module.ENV_PATH.read_text() for name in module.REQUIRED)
    module.ENV_PATH.chmod(0o644)
    module.main()
    assert stat.S_IMODE(module.ENV_PATH.stat().st_mode) == 0o600


def test_local_credentials_reject_symlink_without_touching_target(tmp_path):
    module = _initializer()
    target = tmp_path / "other"
    target.write_text("unrelated\n", encoding="utf-8")
    module.ENV_PATH = tmp_path / ".env"
    module.ENV_PATH.symlink_to(target)
    with pytest.raises(OSError):
        module.main()
    assert target.read_text(encoding="utf-8") == "unrelated\n"
