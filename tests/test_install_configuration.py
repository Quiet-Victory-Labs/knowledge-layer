"""Install inputs cannot smuggle secrets into Compose or replace existing state."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from configure_install import configure, validate_settings


@pytest.fixture
def settings():
    data = json.loads((Path(__file__).resolve().parents[1] / "deploy/settings.example.json").read_text())
    for name in ("image", "postgres_image", "proxy_image"):
        data[name] = data[name].split("@", 1)[0] + "@sha256:" + "a" * 64
    return data


@pytest.mark.parametrize("change", [
    {"image": "knowledge-layer:latest"}, {"client_id": "client\nDATABASE_URL=secret"},
    {"issuer": "https://user:password@identity.example.org"}, {"public_url": "http://example.org"},
    {"public_url": "https://example.org/extra"}, {"enabled_kinds": ["local_files"]},
    {"data_root": "/srv/../etc"}, {"allowed_group": "knowledge-stewards"},
    {"client_id": " "}, {"audience": " "}, {"issuer": "https://identity example.org"},
    {"jwks_uri": "https://-invalid.example.org/keys"},
])
def test_install_settings_refuse_unsafe_or_unqualified_values(settings, change):
    with pytest.raises(ValueError):
        validate_settings({**settings, **change})


def test_new_install_keeps_credentials_in_owner_only_files_and_preserves_existing_state(tmp_path, settings):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps(settings))
    secret = tmp_path / "client-secret"
    secret.write_text("private-test-marker")
    secret.chmod(0o600)
    policy = tmp_path / "source-policy"
    policy.write_text('{"version":1,"sources":[]}')
    policy.chmod(0o600)
    output = tmp_path / "install"
    configure(settings_file, secret, policy, output, os.getuid(), os.getgid())
    assert output.stat().st_mode & 0o077 == 0
    assert "private-test-marker" not in (output / ".env").read_text()
    assert "private-test-marker" not in (output / "compose.yaml").read_text()
    for file in (output / "secrets").iterdir():
        assert file.stat().st_mode & 0o077 == 0
    original = (output / "secrets/database_password").read_bytes()
    with pytest.raises(ValueError):
        configure(settings_file, secret, policy, output, os.getuid(), os.getgid())
    assert (output / "secrets/database_password").read_bytes() == original


def test_install_refuses_symlinked_secret_before_creating_directory(tmp_path, settings):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps(settings))
    secret = tmp_path / "secret"
    secret.write_text("private-test-marker")
    secret.chmod(0o600)
    linked = tmp_path / "linked"
    linked.symlink_to(secret)
    output = tmp_path / "install"
    with pytest.raises(ValueError):
        configure(settings_file, linked, secret, output, os.getuid(), os.getgid())
    assert not output.exists()
