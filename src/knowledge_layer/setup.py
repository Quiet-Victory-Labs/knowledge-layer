"""Small authenticated installation handoff; no secrets or cloud provisioning."""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Request


def create_setup_router(store, *, auth_mode, settings):
    router = APIRouter(tags=["setup"])

    @router.get("/api/setup")
    def setup(request: Request):
        origin = settings.public_url if settings else os.getenv(
            "KNOWLEDGE_PUBLIC_URL", str(request.base_url).rstrip("/")
        )
        endpoint = origin.rstrip("/") + "/agent/mcp/"
        auth = "bearer" if auth_mode in {"local", "demo"} else auth_mode
        codex = '[mcp_servers.knowledge_layer]\nurl = ' + json.dumps(endpoint) + '\n'
        servers = {}
        if auth == "bearer":
            codex += 'bearer_token_env_var = "KNOWLEDGE_AGENT_TOKEN"\n'
            for client, reference in (("claude", "${KNOWLEDGE_AGENT_TOKEN}"), ("cursor", "${env:KNOWLEDGE_AGENT_TOKEN}")):
                value = {"url": endpoint, "headers": {"Authorization": "Bearer " + reference}}
                if client == "claude":
                    value["type"] = "http"
                servers[client] = json.dumps({"mcpServers": {"knowledge_layer": value}}, indent=2)
        else:
            # Remote IdP clients must perform their own configured OAuth/IAP
            # exchange; never manufacture a bearer secret or pretend it works.
            for client in ("claude", "cursor"):
                value = {"url": endpoint}
                if client == "claude":
                    value["type"] = "http"
                servers[client] = json.dumps({"mcpServers": {"knowledge_layer": value}}, indent=2)
        total = store.count_connections("demo")
        checks = [
            {"id": "identity", "status": "ready", "title": "Installation access",
             "detail": "Single-owner sign-in is active." if auth_mode == "local" else
                       "Your configured organization identity is active." if auth_mode in {"oidc", "iap"} else
                       "Evaluation mode is active. Use the self-host launcher for owner authentication."},
            {"id": "agent", "status": "action", "title": "Connect your agent",
             "detail": "Load the configuration in your client, then ask it to verify access. Configuration alone does not prove a connection."},
            {"id": "sources", "status": "ready" if total else "action", "title": "Connect your sources",
             "detail": f"{total} visible source(s)." if total else "Add files or a source approved by your operator."},
        ]
        return {"mode": auth_mode, "public_url": origin, "checks": checks,
                "agent": {"endpoint": endpoint, "auth": auth, "token_env": "KNOWLEDGE_AGENT_TOKEN",
                          "token_file_hint": "secrets/agent_token" if auth == "bearer" else None,
                          "clients": {"codex": codex, **servers},
                          "command_template": "./knowledge-layer agent --client CLIENT --project PROJECT",
                          "permissions": "Read connected evidence and propose drafts. Human acceptance remains separate.",
                          "prompt": "Verify that Knowledge Layer is connected. List only sources available to you, then build evidence-backed drafts for human review. Do not accept or publish your own drafts."},
                "sources": {"count": total}, "builds": {"available": True},
                "next_step": "agent" if total == 0 else "build"}

    return router
