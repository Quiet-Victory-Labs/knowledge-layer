"""Local discovery limits apply during enumeration and HTML callbacks."""

from __future__ import annotations

import pytest

from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.local_files import (
    MAX_DOCUMENT_TEXT_CHARS,
    _candidate_paths,
    _HTMLText,
    _safe_file_bytes,
)


def test_directory_enumeration_stops_before_all_nonmatching_entries(tmp_path):
    for index in range(1002):
        (tmp_path / f"entry-{index:04d}.txt").touch()
    candidates, file_limit, enumeration_limit = _candidate_paths(tmp_path, tmp_path, "*.csv", 1)
    assert candidates == []
    assert file_limit is False
    assert enumeration_limit is True


def test_html_text_callback_caps_content_incrementally():
    parser = _HTMLText()
    for _ in range(300):
        parser.handle_data("A" * 1000)
    assert parser.chars == MAX_DOCUMENT_TEXT_CHARS
    assert sum(map(len, parser.parts)) == MAX_DOCUMENT_TEXT_CHARS


def test_file_open_rejects_replaced_leaf_and_parent_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    folder = root / "approved"
    folder.mkdir()
    outside = tmp_path / "private.csv"
    outside.write_text("secret,value\n", encoding="utf-8")
    source = folder / "orders.csv"
    source.write_text("order_id,total\n", encoding="utf-8")
    assert _safe_file_bytes(root, "approved/orders.csv", 1024)[2] == b"order_id,total\n"
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(ConnectorError):
        _safe_file_bytes(root, "approved/orders.csv", 1024)
    source.unlink()
    folder.rmdir()
    folder.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ConnectorError):
        _safe_file_bytes(root, "approved/private.csv", 1024)
    with pytest.raises(ConnectorError):
        _candidate_paths(root, folder, "*.csv", 10)
