"""Refuse false native-client qualification and unsafe fixture source execution."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

SPEC = importlib.util.spec_from_file_location("native_drill", Path(__file__).parents[1] / "scripts/drill_native_harness.py")
drill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drill)
ANSWER = {"finance_known_revenue": 20, "sales_known_revenue": 1040, "combined_revenue": None,
          "finance_missing_rows": 1, "finance_zero_rows": 1, "refused": False}
CLAUDE_INIT = {"type": "system", "subtype": "init", "mcp_servers": [
    {"name": "knowledge_layer"}, {"name": "fixture_source"}]}
OFFLINE_VALUE = "private"


def test_real_sdk_wire_observer_and_separate_read_only_source():
    observer, executions = drill.Observer(), []
    with drill._fixture() as fixture:
        from contextlib import asynccontextmanager

        from fastapi import FastAPI

        knowledge = drill.build_quick_mcp_server(fixture["store"])
        source = drill._source_server(fixture, observer, executions)

        @asynccontextmanager
        async def lifespan(app):
            async with knowledge.session_manager.run(), source.session_manager.run():
                yield

        app = FastAPI(lifespan=lifespan)
        app.mount("/knowledge", observer.wrap(knowledge.streamable_http_app(streamable_http_path="/", json_response=True, stateless_http=True), "/agent/mcp"))
        app.mount("/source", observer.wrap(source.streamable_http_app(streamable_http_path="/", json_response=True, stateless_http=True), "/source/mcp"))
        with TestClient(app, base_url="http://127.0.0.1:4173") as client:
            def call(route, name, args):
                response = client.post(route, headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
                assert response.status_code == 200
                return response.json()["result"]

            cid = fixture["bindings"]["finance"]["connection_id"]
            aid = fixture["bindings"]["finance"]["assertion_id"]
            scope = [b["connection_id"] for b in fixture["bindings"].values()] + [fixture["document_connection_id"]]
            packet = call("/knowledge/", "get_task_context", {"query": "Revenue", "connection_ids": scope})["structuredContent"]
            receipt = packet["receipt"]
            assert call("/source/", "execute_reviewed_plan", {"connection_id": cid, "receipt": receipt})["isError"]
            refreshed = call("/knowledge/", "revalidate_task_context", {"receipt": receipt})["structuredContent"]["receipt"]
            call("/knowledge/", "plan_metric_query", {"connection_id": cid, "assertion_id": aid, "request": {"group_by": ["region"]}})
            source_result = call("/source/", "execute_reviewed_plan", {"connection_id": cid, "receipt": refreshed})
            assert "structuredContent" in source_result, observer.source_failure
            result = source_result["structuredContent"]
            assert result["rows"][0]["metric_value"] == 20
            assert sum(r["missing_measure_rows"] for r in result["rows"]) == 1
            assert sum(r["zero_measure_rows"] for r in result["rows"]) == 1
            assert executions == [cid]
            fixture["store"].revoke_publication("demo", cid, reason="Test owner withdrew meaning.")
            assert call("/knowledge/", "revalidate_task_context", {"receipt": receipt})["isError"]
            assert call("/source/", "execute_reviewed_plan", {"connection_id": cid, "receipt": refreshed})["isError"]
            assert executions == [cid]
            assert observer.events[-1] == {"surface": "/source/mcp", "tool": "execute_reviewed_plan", "success": False}


def test_forged_observed_plan_never_executes():
    with drill._fixture() as fixture:
        from knowledge_layer.task_context import task_context

        observer, executions = drill.Observer(), []
        cid = fixture["bindings"]["finance"]["connection_id"]
        receipt = task_context(fixture["store"], tenant_id="demo", query="Revenue", connection_ids=[cid])["receipt"]
        observer.validated.add(drill._digest(receipt))
        observer.plans[cid] = ({"connection_id": cid, "assertion_id": fixture["bindings"]["finance"]["assertion_id"],
                               "request": {"group_by": ["region"]}}, {"sql": "DELETE FROM orders"})
        with pytest.raises(ToolError):
            asyncio.run(drill._source_server(fixture, observer, executions).call_tool("execute_reviewed_plan", {"connection_id": cid, "receipt": receipt}))
        assert executions == []


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_parser_keeps_finite_results_without_raw_text(client):
    event = ({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(ANSWER)}} if client == "codex"
             else {"type": "result", "structured_output": ANSWER, "result": "private model prose", "usage": {"input_tokens": 40, "arbitrary_secret": OFFLINE_VALUE}})
    output = "\n".join(json.dumps(e) for e in ([event] if client == "codex" else [CLAUDE_INIT, event])).encode()
    parsed = drill._parse_output(client, output)
    assert parsed["answer"] == ANSWER
    assert "private" not in json.dumps(parsed)


@pytest.mark.parametrize("output", [b"", b'{"type":"result","structured_output":{}}',
    json.dumps({"type": "result", "structured_output": ANSWER | {"untrusted": "private"}}).encode()])
def test_exit_zero_without_valid_task_result_refuses(output):
    with pytest.raises(drill.DrillFailure, match="invalid_answer"):
        drill._parse_output("claude", output)


@pytest.mark.parametrize("event", [
    {"type": "item.completed", "item": {"type": "command_execution"}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__unrelated__read"}]}}
])
def test_unrelated_tools_fail_qualification(event):
    output = "\n".join(json.dumps(e) for e in [event, {"type": "result", "structured_output": ANSWER}]).encode()
    with pytest.raises(drill.DrillFailure, match="unexpected_client_tool"):
        drill._parse_output("claude", output)


def test_commands_never_include_secret_or_global_config_mutations(tmp_path):
    (tmp_path / "answer-schema.json").write_text(json.dumps(drill.ANSWER_SCHEMA))
    for client in ("codex", "claude"):
        cmd = drill._command(client, "http://127.0.0.1:12345", tmp_path)
        assert not set(cmd) & {"add", "login", "logout", "--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox"}
        assert "KNOWLEDGE_NATIVE_DRILL_TOKEN" in " ".join(cmd) or client == "claude"
    config = (tmp_path / "mcp.json").read_text()
    assert "${KNOWLEDGE_NATIVE_DRILL_TOKEN}" in config
    assert (tmp_path / "mcp.json").stat().st_mode & 0o777 == 0o600


def test_failed_mcp_result_cannot_register_plan_or_revalidated_receipt():
    observer = drill.Observer()
    for name, arguments in [("plan_metric_query", {"connection_id": "source"}), ("revalidate_task_context", {"receipt": {"fake": True}})]:
        observer.record("knowledge", {"method": "tools/call", "params": {"name": name, "arguments": arguments}},
                        {"result": {"isError": True, "structuredContent": {"sql": "DELETE"}}})
    assert observer.plans == {} and observer.validated == set()


def test_observation_budget_refuses_instead_of_partial_proof():
    observer = drill.Observer()
    observer.events = [{}] * 500
    with pytest.raises(drill.DrillFailure, match="observation_budget"):
        observer.record("knowledge", {"method": "tools/call"}, {})


@pytest.mark.parametrize("extra", [{"plugins": ["ambient"]}, {"plugin_errors": ["private diagnostic"]},
                                   {"mcp_server_errors": ["private diagnostic"]}])
def test_ambient_claude_customization_refuses_without_printing_diagnostics(extra):
    output = "\n".join(json.dumps(e) for e in [CLAUDE_INIT | extra, {"type": "result", "structured_output": ANSWER}]).encode()
    with pytest.raises(drill.DrillFailure, match="unexpected_client_tool"):
        drill._parse_output("claude", output)


@pytest.mark.parametrize("servers", [[], [{"name": "knowledge_layer"}],
    [{"name": "knowledge_layer"}, {"name": "fixture_source"}, {"name": "unrelated"}]])
def test_missing_or_additional_claude_mcp_server_refuses(servers):
    output = "\n".join(json.dumps(e) for e in [CLAUDE_INIT | {"mcp_servers": servers},
        {"type": "result", "structured_output": ANSWER}]).encode()
    with pytest.raises(drill.DrillFailure, match="client_init_evidence"):
        drill._parse_output("claude", output)
