"""Request-scoped identity for source metadata authorization."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Principal:
    subject: str
    groups: frozenset[str]
    scopes: frozenset[str] = frozenset()


_PRINCIPAL: ContextVar[Principal | None] = ContextVar("knowledge_principal", default=None)


def principal_from_claims(claims: dict[str, Any], *, group_claim: str = "groups") -> Principal:
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise ValueError("A bounded subject claim is required")
    value = claims.get(group_claim)
    if isinstance(value, str):
        groups = frozenset(value.split())
    elif isinstance(value, list):
        groups = frozenset(item for item in value if isinstance(item, str))
    else:
        groups = frozenset()
    if any(not group or len(group) > 255 for group in groups):
        raise ValueError("Group claims must be bounded strings")
    scope_values = (claims.get("scope"), claims.get("scp"))
    scopes: set[str] = set()
    for scope_value in scope_values:
        if isinstance(scope_value, str):
            scopes.update(scope_value.split())
        elif isinstance(scope_value, list):
            scopes.update(item for item in scope_value if isinstance(item, str))
    return Principal(subject=subject, groups=groups, scopes=frozenset(scopes))


def current_principal() -> Principal | None:
    return _PRINCIPAL.get()


@contextmanager
def principal_scope(principal: Principal) -> Iterator[None]:
    token = _PRINCIPAL.set(principal)
    try:
        yield
    finally:
        _PRINCIPAL.reset(token)
