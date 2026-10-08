"""Small file-secret boundary for deployment configuration."""

from __future__ import annotations

import os
import stat

_MAX_FILE_BYTES = 64 * 1024


class SecretFileError(ValueError):
    """A mounted secret failed its file boundary; never include the path."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def read_owner_only_file(path: str, *, max_bytes: int = _MAX_FILE_BYTES) -> bytes:
    """Validate and read a regular file through one non-following descriptor."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SecretFileError("unavailable") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise SecretFileError("invalid")
            if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise SecretFileError("permissions")
            if info.st_size > max_bytes:
                raise SecretFileError("too_large")
            contents = handle.read(max_bytes + 1)
            if len(contents) > max_bytes:
                raise SecretFileError("too_large")
            return contents
    except OSError as exc:
        raise SecretFileError("unavailable") from exc


def operator_secret(name: str) -> str | None:
    """Read ``NAME`` or ``NAME_FILE``; mounted files must be owner-only.

    This is intended for operator-controlled database and OIDC credentials.
    Secret-manager integrations can mount a rotated file without persisting its
    contents in the knowledge registry.
    """
    value = os.getenv(name)
    path_value = os.getenv(f"{name}_FILE")
    if value and path_value:
        raise ValueError(f"Configure only one of {name} and {name}_FILE")
    if value:
        return value
    if not path_value:
        return None
    try:
        return read_owner_only_file(path_value).decode("utf-8").rstrip("\r\n")
    except SecretFileError as exc:
        if exc.reason in {"invalid", "too_large"}:
            raise ValueError(f"{name}_FILE must be a small regular file")
        if exc.reason == "permissions":
            raise ValueError(f"{name}_FILE must be readable only by its owner")
        raise ValueError(f"{name}_FILE cannot be read") from exc
    except UnicodeError as exc:
        raise ValueError(f"{name}_FILE cannot be read") from exc
