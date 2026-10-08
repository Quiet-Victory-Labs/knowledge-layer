"""Mounted source secrets rotate without entering the knowledge registry."""

from __future__ import annotations

import os

import pytest

from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.secrets import read_source_secret
from knowledge_layer.operator_secrets import operator_secret


def test_mounted_source_secret_reads_rotated_value_and_rejects_unsafe_modes(tmp_path, monkeypatch):
    name = "KNOWLEDGE_SOURCE_ROTATING"
    path = tmp_path / "source-credential"
    path.write_text("first\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv(f"{name}_FILE", str(path))
    assert read_source_secret(name, unavailable_code="missing") == "first"
    path.write_text("second\n", encoding="utf-8")
    assert read_source_secret(name, unavailable_code="missing") == "second"
    path.chmod(0o644)
    with pytest.raises(ConnectorError, match="must not be accessible"):
        read_source_secret(name, unavailable_code="missing")
    path.chmod(0o600)
    monkeypatch.setenv(name, "inline")
    with pytest.raises(ConnectorError, match="either a source secret or its file reference"):
        read_source_secret(name, unavailable_code="missing")


def test_database_url_file_never_falls_back_to_unprotected_storage(tmp_path, monkeypatch):
    path = tmp_path / "database-url"
    path.write_text("postgresql://user:password@db/knowledge\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL_FILE", str(path))
    assert operator_secret("DATABASE_URL") == "postgresql://user:password@db/knowledge"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="readable only by its owner"):
        operator_secret("DATABASE_URL")


def test_secret_files_reject_symlinks_and_nonregular_files(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_text("private-value\n", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "source-link"
    link.symlink_to(target)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_ROTATING_FILE", str(link))
    with pytest.raises(ConnectorError, match="unavailable"):
        read_source_secret("KNOWLEDGE_SOURCE_ROTATING", unavailable_code="missing")
    monkeypatch.setenv("DATABASE_URL_FILE", str(link))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="cannot be read"):
        operator_secret("DATABASE_URL")

    pipe = tmp_path / "source-pipe"
    os.mkfifo(pipe, 0o600)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_ROTATING_FILE", str(pipe))
    with pytest.raises(ConnectorError, match="regular file"):
        read_source_secret("KNOWLEDGE_SOURCE_ROTATING", unavailable_code="missing")


def test_secret_read_stays_on_open_descriptor_when_path_is_replaced(tmp_path, monkeypatch):
    name = "KNOWLEDGE_SOURCE_ROTATING"
    path = tmp_path / "credential"
    path.write_text("approved\n", encoding="utf-8")
    path.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_text("wrong-secret\n", encoding="utf-8")
    outside.chmod(0o600)
    monkeypatch.setenv(f"{name}_FILE", str(path))
    original_open = os.open

    def replace_after_open(file, flags, *args, **kwargs):
        descriptor = original_open(file, flags, *args, **kwargs)
        if file == str(path):
            path.rename(tmp_path / "original")
            path.symlink_to(outside)
        return descriptor

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", replace_after_open)
        assert read_source_secret(name, unavailable_code="missing") == "approved"
