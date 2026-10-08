"""Operator-owned receipt keys; never stored in company knowledge or exports.

An optional key ring lets replicas of one registry revalidate short-lived
context. Mounted regular files are read for every request, including revocation.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import Lock
from types import MappingProxyType

from knowledge_layer.operator_secrets import read_owner_only_file

CONFIG_NAME = "KNOWLEDGE_CONTEXT_RECEIPT_KEYS"
MAX_RECEIPT_SECONDS = 300
_CONFIG_ERROR = "Context receipt keys must be a valid bounded operator key ring"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


@dataclass(frozen=True)
class ReceiptKeys:
    registry_id: str | None
    active_key_id: str | None
    keys: Mapping[str | None, bytes] = field(repr=False)

    @property
    def shared(self) -> bool:
        return self.registry_id is not None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_CONFIG_ERROR)
        result[key] = value
    return result


def parse_key_ring(raw: str) -> ReceiptKeys:
    """Strict finite JSON and canonical 256-bit base64url key material."""
    try:
        if len(raw.encode("utf-8")) > 8192:
            raise ValueError(_CONFIG_ERROR)
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if (not isinstance(value, dict)
                or set(value) != {"version", "registry_id", "active_key_id", "keys"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["registry_id"], str) or not _ID.fullmatch(value["registry_id"])
                or not isinstance(value["active_key_id"], str) or not _KEY_ID.fullmatch(value["active_key_id"])
                or not isinstance(value["keys"], list) or not 1 <= len(value["keys"]) <= 3):
            raise ValueError(_CONFIG_ERROR)
        keys = {}
        for item in value["keys"]:
            if (not isinstance(item, dict) or set(item) != {"id", "secret"}
                    or not isinstance(item["id"], str) or not _KEY_ID.fullmatch(item["id"])
                    or item["id"] in keys or not isinstance(item["secret"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{43}", item["secret"])):
                raise ValueError(_CONFIG_ERROR)
            key = base64.b64decode(item["secret"] + "=", altchars=b"-_", validate=True)
            if (len(key) != 32 or base64.urlsafe_b64encode(key).decode().rstrip("=") != item["secret"]
                    or key in keys.values()):
                raise ValueError(_CONFIG_ERROR)
            keys[item["id"]] = key
        if value["active_key_id"] not in keys:
            raise ValueError(_CONFIG_ERROR)
        return ReceiptKeys(value["registry_id"], value["active_key_id"], MappingProxyType(keys))
    except (TypeError, ValueError, UnicodeError, binascii.Error, RecursionError):
        raise ValueError(_CONFIG_ERROR) from None


class ReceiptKeyProvider:
    """Freeze local/shared mode and registry identity at startup, reload keys.

    A configured ring never falls back to a process key. Deployments must use
    distinct registry IDs and key material; sharing a key ring is a trust grant
    to all of its holders, not a substitute for source or user authorization.
    """

    def __init__(self) -> None:
        inline, path = os.getenv(CONFIG_NAME), os.getenv(f"{CONFIG_NAME}_FILE")
        if inline is not None and path is not None:
            raise ValueError("Configure one context receipt key source")
        self._configured = inline is not None or path is not None
        self._inline = inline
        self._path = path
        self._registry_id = self._read().registry_id if self._configured else None
        self._local: ReceiptKeys | None = None
        self._lock = Lock()

    def _read(self) -> ReceiptKeys:
        try:
            raw = (read_owner_only_file(self._path, max_bytes=8192).decode("utf-8")
                   if self._path else self._inline)
            if not raw:
                raise ValueError(_CONFIG_ERROR)
            return parse_key_ring(raw)
        except (ValueError, UnicodeError):
            raise ValueError("Context receipt key configuration is unavailable or invalid") from None

    def current(self) -> ReceiptKeys:
        if self._configured:
            ring = self._read()
            if ring.registry_id != self._registry_id:
                raise ValueError("Context receipt registry identity changed; restart with the intended configuration")
            return ring
        with self._lock:
            if self._local is None:
                self._local = ReceiptKeys(None, None, MappingProxyType({None: secrets.token_bytes(32)}))
            return self._local
