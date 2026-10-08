"""Resolve operator-provisioned source credentials without persisting their values."""

from __future__ import annotations

import os
import re

from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.operator_secrets import SecretFileError, read_owner_only_file

_REFERENCE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_MAX_BYTES = 64 * 1024


def read_source_secret(reference: str, *, unavailable_code: str) -> str:
    """Read an env secret or a mounted file referenced by ``<name>_FILE``.

    Files allow Kubernetes/Compose secret mounts and rotation on the next
    refresh. No connection record or tool response ever contains this value.
    """
    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise ConnectorError("invalid_secret_reference", "A named server-side source reference is required")
    value = os.environ.get(reference)
    path_value = os.environ.get(f"{reference}_FILE")
    if value and path_value:
        raise ConnectorError("ambiguous_secret_reference", "Configure either a source secret or its file reference")
    if value:
        if len(value.encode("utf-8")) > _MAX_BYTES:
            raise ConnectorError("source_secret_too_large", "The configured source secret exceeds the size limit")
        return value
    if path_value:
        try:
            contents = read_owner_only_file(path_value, max_bytes=_MAX_BYTES)
            return contents.decode("utf-8").rstrip("\r\n")
        except SecretFileError as exc:
            if exc.reason == "permissions":
                raise ConnectorError("source_secret_permissions", "The mounted source secret must not be accessible to group or others") from exc
            if exc.reason == "too_large":
                raise ConnectorError("source_secret_too_large", "The configured source secret exceeds the size limit") from exc
            if exc.reason == "invalid":
                raise ConnectorError("source_secret_invalid", "The mounted source secret must be a small regular file") from exc
            raise ConnectorError(unavailable_code, "The configured source secret file is unavailable") from exc
        except UnicodeError as exc:
            raise ConnectorError(unavailable_code, "The configured source secret file is unavailable") from exc
    raise ConnectorError(unavailable_code, "The configured source secret reference is unavailable")
