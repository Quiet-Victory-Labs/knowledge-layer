"""Portable company guidance tools shared by compact and advanced MCP."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import StrictInt


def register_guidance_tools(server: Any, store: Any, *, tenant_id: str = "demo") -> None:
    from knowledge_layer.guidance import GuidanceRegistry
    from knowledge_layer.guidance_export import GuidanceExportError, export_guidance
    from knowledge_layer.mcp import READ_ONLY_ANNOTATIONS, _contract_tool_response
    from knowledge_layer.store import KnowledgeError, KnowledgeNotFound

    def result(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        from mcp.server.mcpserver.exceptions import ToolError

        try:
            return _contract_tool_response(call())
        except (KnowledgeError, GuidanceExportError) as exc:
            raise ToolError(str(exc)) from exc

    def registry() -> GuidanceRegistry:
        return GuidanceRegistry(store)

    @server.tool(description="Resolve current published company skills and rules for this authenticated audience and task. Baselines always apply; narrow scopes require explicit context. Conflicts or unavailable dependencies block use. Prose guidance does not grant tool permissions.", annotations=READ_ONLY_ANNOTATIONS)
    def resolve_guidance(
        business_unit: str | None = None, task: str | None = None,
        connection_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return result(lambda: registry().resolve(
            tenant_id, business_unit=business_unit, task=task, connection_ids=connection_ids,
        ))

    @server.tool(description="Read an authorized guidance revision for review. Draft and historical text must not be used as current instructions; use resolve_guidance for effective published guidance.", annotations=READ_ONLY_ANNOTATIONS)
    def get_guidance(slug: str, revision: StrictInt | None = None) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            record = registry().get(tenant_id, slug, revision=revision)
            if record is None:
                raise KnowledgeNotFound("Guidance was not found")
            return record

        return result(read)

    @server.tool(description="Draft an immutable company skill/rule revision. Requires source-manager privileges and expected latest revision. Publication requires separate steward action and is unavailable to agents.")
    def draft_guidance(
        slug: str, payload: dict[str, Any], expected_revision: StrictInt = 0,
    ) -> dict[str, Any]:
        return result(lambda: registry().draft(
            tenant_id, slug, payload, expected_revision=expected_revision,
        ))

    @server.tool(description="Export complete current published guidance as portable skill files or thin client layouts. Refuses conflicts, stale dependencies and unavailable guidance. Revalidate with resolve_guidance before use; export cannot grant permissions or execute tools.", annotations=READ_ONLY_ANNOTATIONS)
    def export_guidance_bundle(
        client: str = "portable", business_unit: str | None = None,
        task: str | None = None, connection_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return result(lambda: export_guidance(registry().resolve(
            tenant_id, business_unit=business_unit, task=task, connection_ids=connection_ids,
        ), client=client))
