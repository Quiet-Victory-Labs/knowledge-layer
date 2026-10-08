"""Exact source authority for the explicitly selected single-owner profile."""
from __future__ import annotations

import os

from knowledge_layer.local_auth import AGENT, OWNER
from knowledge_layer.source_policy import SourcePolicy, source_identity


class OwnerSourcePolicy(SourcePolicy):
    def __init__(self):
        self.kinds = frozenset(value.strip() for value in os.getenv("KNOWLEDGE_ENABLED_KINDS", "local_files").split(",") if value.strip())
        # The owner's explicit source selection creates the per-source grant
        # in the same transaction. Policy loading must not open a registry
        # connection inside an active review transaction (StaticPool SQLite
        # shares the underlying connection). Reads still require an existing
        # source row and its explicit grant; workers require the owner's manage
        # grant before performing source IO.
        super().__init__(())

    def approved(self, tenant_id, kind, config):
        if tenant_id != "demo" or kind not in self.kinds:
            return False
        source_identity(kind, config)
        return True

    def allows(self, tenant_id, kind, config, principal):
        # The claimed operator's source selection is the approval, constrained
        # by enabled kinds, validated selectors and separately mounted secrets.
        if principal.subject == OWNER and tenant_id == "demo" and kind in self.kinds:
            source_identity(kind, config)
            return True
        return principal.subject == AGENT and self.approved(tenant_id, kind, config)


def owner_source_policy(store):
    if os.getenv("KNOWLEDGE_SOURCE_POLICY_FILE") or "KNOWLEDGE_SOURCE_POLICY" in os.environ:
        return SourcePolicy.from_environment()
    return OwnerSourcePolicy()
