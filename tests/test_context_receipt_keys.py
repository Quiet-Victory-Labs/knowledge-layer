"""Key configuration never silently changes signing mode or opens a registry."""

import base64
import json
import os
import secrets

import pytest

from knowledge_layer.context_receipts import CONFIG_NAME, ReceiptKeyProvider, parse_key_ring
from knowledge_layer.store import KnowledgeStore


def ring():
    return {"version": 1, "registry_id": "test-registry", "active_key_id": "first",
            "keys": [{"id": "first", "secret": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")}]}


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    monkeypatch.delenv(CONFIG_NAME, raising=False)
    monkeypatch.delenv(CONFIG_NAME + "_FILE", raising=False)


@pytest.mark.parametrize("change", [
    lambda r: r.update(version=True),
    lambda r: r.update(version=2),
    lambda r: r.update(registry_id=""),
    lambda r: r.update(registry_id="x" * 129),
    lambda r: r.update(registry_id="private registry"),
    lambda r: r.update(active_key_id="absent"),
    lambda r: r.update(extra="unrecognized"),
    lambda r: r.update(keys=[]),
    lambda r: r.update(keys=r["keys"] * 4),
    lambda r: r["keys"].append(dict(r["keys"][0])),
    lambda r: r["keys"].append({**r["keys"][0], "id": "second"}),
    lambda r: r["keys"][0].update(secret=r["keys"][0]["secret"] + "="),
    lambda r: r["keys"][0].update(secret="a" * 42),
    lambda r: r["keys"][0].update(secret="x" * 43),  # noncanonical trailing bits
    lambda r: r["keys"][0].update(secret=123),
    lambda r: r["keys"][0].update(id="x" * 65),
    lambda r: r["keys"][0].update(extra=None),
])
def test_ambiguous_or_invalid_key_material_is_rejected(change):
    value = ring()
    change(value)
    with pytest.raises(ValueError, match="bounded operator key ring") as caught:
        parse_key_ring(json.dumps(value))
    if value["keys"] and isinstance(value["keys"][0]["secret"], str):
        assert value["keys"][0]["secret"] not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("raw", [
    "{invalid-json", "null", "[]", " " * 8193,
    '{"version":1,"version":1}',
    '{"keys":[{"id":"one","id":"two"}]}',
    "[" * 2000 + "]" * 2000,
])
def test_malformed_bounded_json_is_sanitized(raw):
    with pytest.raises(ValueError, match="bounded operator key ring") as caught:
        parse_key_ring(raw)
    assert caught.value.__cause__ is None


def test_key_representation_and_json_cannot_export_secrets():
    value = ring()
    parsed = parse_key_ring(json.dumps(value))
    assert parsed.shared and parsed.active_key_id == "first"
    assert value["keys"][0]["secret"] not in repr(parsed)
    assert "keys=" not in repr(parsed)
    with pytest.raises(TypeError):
        parsed.keys["new"] = b"not allowed"


@pytest.mark.parametrize("raw", ["", "invalid", "{}"])
def test_invalid_inline_config_aborts_before_database_creation(tmp_path, monkeypatch, raw):
    monkeypatch.setenv(CONFIG_NAME, raw)
    database = tmp_path / "must-not-exist" / "registry.db"
    monkeypatch.setenv("KNOWLEDGE_DB_PATH", str(database))
    def forbidden_engine(*args, **kwargs):
        pytest.fail("invalid key config opened a database")
    monkeypatch.setattr("knowledge_layer.store.create_engine", forbidden_engine)
    with pytest.raises(ValueError, match="key configuration"):
        KnowledgeStore()
    assert not database.parent.exists()


def test_ambiguous_inline_and_file_configuration_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_NAME, json.dumps(ring()))
    monkeypatch.setenv(CONFIG_NAME + "_FILE", str(tmp_path / "absent-private-file"))
    with pytest.raises(ValueError, match="one context receipt key source"):
        ReceiptKeyProvider()


def test_mode_inline_value_and_file_location_are_pinned_at_startup(tmp_path, monkeypatch):
    local = ReceiptKeyProvider()
    value = ring()
    monkeypatch.setenv(CONFIG_NAME, json.dumps(value))
    assert not local.current().shared
    inline = ReceiptKeyProvider()
    monkeypatch.setenv(CONFIG_NAME, json.dumps({**value, "registry_id": "other-registry"}))
    assert inline.current().registry_id == "test-registry"
    monkeypatch.delenv(CONFIG_NAME)
    original, replacement = tmp_path / "original", tmp_path / "replacement"
    for file, namespace in ((original, "test-registry"), (replacement, "other-registry")):
        file.write_text(json.dumps({**value, "registry_id": namespace}))
        file.chmod(0o600)
    monkeypatch.setenv(CONFIG_NAME + "_FILE", str(original))
    mounted = ReceiptKeyProvider()
    monkeypatch.setenv(CONFIG_NAME + "_FILE", str(replacement))
    assert mounted.current().registry_id == "test-registry"
    original.write_text(json.dumps({**value, "registry_id": "other-registry"}))
    with pytest.raises(ValueError, match="identity changed"):
        mounted.current()


@pytest.mark.parametrize("failure", ["missing", "public", "symlink", "pipe", "oversized", "invalid", "empty"])
def test_mounted_file_never_uses_cached_keys_after_read_failure(tmp_path, monkeypatch, failure):
    value = ring()
    file = tmp_path / "private-credential-location"
    file.write_text(json.dumps(value))
    file.chmod(0o600)
    monkeypatch.setenv(CONFIG_NAME + "_FILE", str(file))
    provider = ReceiptKeyProvider()
    assert provider.current().shared
    if failure == "missing":
        file.unlink()
    elif failure == "public":
        file.chmod(0o644)
    elif failure in {"symlink", "pipe"}:
        file.unlink()
        if failure == "symlink":
            target = tmp_path / "target"
            target.write_text(json.dumps(value))
            target.chmod(0o600)
            file.symlink_to(target)
        else:
            os.mkfifo(file, 0o600)
    else:
        file.write_text(" " * 8193 if failure == "oversized" else "invalid" if failure == "invalid" else "")
    with pytest.raises(ValueError, match="unavailable or invalid") as caught:
        provider.current()
    assert str(file) not in str(caught.value)
    assert value["keys"][0]["secret"] not in str(caught.value)
    assert caught.value.__cause__ is None
