"""Opt-in native CLI consumption drill on disposable synthetic enterprise data.

Uses existing authenticated Codex/Claude CLIs, sequentially, with finite wall and
output bounds. No customer sources, config edits, OAuth login, or model dependency.
Raw CLI output, prompts, tokens and receipts are never included in the report.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import AsyncExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import JSONResponse

from knowledge_layer.auth import AgentBearerBoundary
from knowledge_layer.guidance import GuidanceRegistry
from knowledge_layer.mcp import READ_ONLY_ANNOTATIONS
from knowledge_layer.mcp_transport import MAX_MCP_REQUEST_BYTES, McpEnvelopeBoundary
from knowledge_layer.planning import metric_plan
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.task_context import revalidate_context

ROOT = Path(__file__).resolve().parents[1]
MAX_OUTPUT = 2 * 1024 * 1024
READ_TOOLS = ["get_workspace", "get_capabilities", "resolve_guidance", "get_task_context",
              "revalidate_task_context", "plan_metric_query"]
BUILTIN_PLUGINS = {"agents-md", "telemetry"}
ANSWER_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "finance_known_revenue": {"type": ["number", "null"]},
    "sales_known_revenue": {"type": ["number", "null"]},
    "combined_revenue": {"type": "null"},
    "finance_missing_rows": {"type": ["integer", "null"]},
    "finance_zero_rows": {"type": ["integer", "null"]},
    "refused": {"type": "boolean"}}, "required": ["finance_known_revenue",
        "sales_known_revenue", "combined_revenue", "finance_missing_rows", "finance_zero_rows", "refused"]}


class DrillFailure(RuntimeError):
    """Fixed diagnostic codes; never relay arbitrary process or tool text."""

    def __init__(self, code, details=None):
        super().__init__(code)
        self.details = details or {}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _fixture():
    spec = importlib.util.spec_from_file_location("native_enterprise_fixture", ROOT / "examples/enterprise_consumer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.enterprise_fixture()


class Observer:
    """Observe actual JSON-response MCP wire results, keeping sensitive state in memory."""

    def __init__(self):
        self.events = []
        self.plans = {}
        self.receipt = None
        self.validated = set()
        self.protocols = set()
        self.available = True
        self.failure = None
        self.denied = []
        self.wire_events = []
        self.source_failure = None

    def record(self, surface, request, response):
        if len(self.events) >= 500 or len(self.wire_events) >= 500:
            self.failure = "observation_budget"
            raise DrillFailure("observation_budget")
        method = request.get("method")
        params = request.get("params") or {}
        result = response.get("result") or {}
        self.wire_events.append({"method": method if method in {"initialize", "tools/list", "tools/call", "notifications/initialized"} else "other",
                                 "error_code": (response.get("error") or {}).get("code"),
                                 "protocol": result.get("protocolVersion")})
        if method == "initialize" and result.get("protocolVersion"):
            self.protocols.add(result["protocolVersion"])
        if method != "tools/call":
            return
        name = params.get("name")
        if name not in READ_TOOLS + ["execute_reviewed_plan"]:
            self.failure = "unexpected_tool"
            raise DrillFailure("unexpected_tool")
        success = "error" not in response and result.get("isError") is not True
        content = result.get("structuredContent") or {}
        self.events.append({"surface": surface, "tool": name, "success": success})
        arguments = params.get("arguments") or {}
        if not success:
            self.denied.append((name, _digest(arguments)))
        if success and name == "get_task_context":
            self.receipt = content.get("receipt")
        if success and name == "revalidate_task_context":
            self.validated.add(_digest(arguments.get("receipt")))
            if isinstance(content.get("receipt"), dict):
                self.validated.add(_digest(content["receipt"]))
        if success and name == "plan_metric_query":
            self.plans[arguments.get("connection_id")] = (arguments, content)

    def wrap(self, app, surface):
        async def observed(scope, receive, send):
            if scope["type"] != "http" or scope["method"] != "POST":
                await app(scope, receive, send)
                return
            if not self.available:
                await JSONResponse({"detail": "Disposable registry unavailable"}, status_code=503)(scope, receive, send)
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > MAX_MCP_REQUEST_BYTES:
                    await JSONResponse({}, status_code=413)(scope, receive, send)
                    return
                if not message.get("more_body"):
                    break
            replayed, output = False, bytearray()

            async def replay():
                nonlocal replayed
                if replayed:
                    return await receive()
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            async def capture(message):
                if message["type"] == "http.response.body":
                    output.extend(message.get("body", b""))
                    if len(output) > MAX_OUTPUT:
                        self.failure = "observation_budget"
                        raise DrillFailure("observation_budget")
                    if not message.get("more_body"):
                        try:
                            response = json.loads(output)
                        except (ValueError, UnicodeError):
                            response = None
                        if isinstance(request, dict) and isinstance(response, dict):
                            self.record(surface, request, response)
                await send(message)

            try:
                request = json.loads(body)
            except (ValueError, UnicodeError):
                request = {}
            if isinstance(request, dict) and request.get("method") == "tools/call":
                allowed = READ_TOOLS if surface == "/agent/mcp" else ["execute_reviewed_plan"]
                if (request.get("params") or {}).get("name") not in allowed:
                    self.failure = "unexpected_tool"
                    await JSONResponse({"jsonrpc": "2.0", "id": request.get("id"), "error": {
                        "code": -32601, "message": "Tool outside drill scope"}}, status_code=400)(scope, receive, send)
                    return
            try:
                await app(scope, replay, capture)
            except Exception:
                self.failure = self.failure or "observer_failed"
                raise
        return observed


def _source_server(fixture, observer, executions):
    server = MCPServer("fixture-source", description="Independent read-only synthetic SQL source; no customer access.")

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, description="Execute an already observed approved metric plan on the synthetic source. Requires a successfully revalidated current receipt. No arbitrary SQL or writes are accepted.")
    def execute_reviewed_plan(connection_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        try:
            if _digest(receipt) not in observer.validated or connection_id not in observer.plans:
                raise DrillFailure("unobserved_plan_or_revalidation")
            revalidate_context(fixture["store"], tenant_id="demo", receipt=receipt)
            arguments, issued = observer.plans[connection_id]
            current = metric_plan(fixture["store"], tenant_id="demo", **arguments)
            if _digest(issued) != _digest(current):
                raise DrillFailure("changed_plan")
            bindings = [b for b in fixture["bindings"].values() if b["connection_id"] == connection_id]
            if len(bindings) != 1:
                raise DrillFailure("unknown_fixture")
            with sqlite3.connect(f"file:{bindings[0]['database']}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only = ON")
                started = time.monotonic()
                conn.set_progress_handler(lambda: int(time.monotonic() - started > 5), 1000)
                rows = [dict(row) for row in conn.execute(current["sql"], current["parameters"]).fetchmany(101)]
                if len(rows) > 100 or conn.execute(current["diagnostics"]["grain_check_sql"], current["parameters"]).fetchone():
                    raise DrillFailure("source_validation")
            executions.append(connection_id)
            observer.source_failure = None
            return {"rows": rows, "fixture_only": True, "execution": "independent_read_only_sqlite"}
        except Exception as exc:
            observer.source_failure = str(exc) if isinstance(exc, DrillFailure) else type(exc).__name__
            raise ToolError("Fixture plan execution refused") from exc
    return server


@contextmanager
def _serve(fixture, observer, token, executions):
    knowledge = build_quick_mcp_server(fixture["store"])
    source = _source_server(fixture, observer, executions)

    @contextmanager
    def listener():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            yield sock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for server in (knowledge, source):
                await stack.enter_async_context(server.session_manager.run())
            yield

    app = FastAPI(lifespan=lifespan)
    for route, server in (("/agent/mcp", knowledge), ("/source/mcp", source)):
        transport = server.streamable_http_app(streamable_http_path="/", json_response=True, stateless_http=True)
        app.mount(route, AgentBearerBoundary(observer.wrap(McpEnvelopeBoundary(transport), route), token))
    with listener() as sock:
        runtime = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=runtime.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not runtime.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise DrillFailure("server_start")
                time.sleep(0.05)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            runtime.should_exit = True
            thread.join(timeout=15)
            if thread.is_alive():
                raise DrillFailure("server_cleanup")


def _command(client, base, root, model=None):
    if client == "codex":
        args = ["codex", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "--json", "--color", "never", "-C", str(root),
                "--output-schema", str(root / "answer-schema.json")]
        config = {"approval_policy": "never", "web_search": "disabled", "features.shell_tool": False,
                  "features.unified_exec": False, "features.memories": False, "features.multi_agent": False,
                  "features.apps": False, "features.shell_snapshot": False}
        if model:
            config["model"] = model
        for name, route, tools in (("knowledge_layer", "agent/mcp", READ_TOOLS),
                                  ("fixture_source", "source/mcp", ["execute_reviewed_plan"])):
            config.update({f"mcp_servers.{name}.url": f"{base}/{route}/",
                           f"mcp_servers.{name}.bearer_token_env_var": "KNOWLEDGE_NATIVE_DRILL_TOKEN",
                           f"mcp_servers.{name}.required": True,
                           f"mcp_servers.{name}.enabled_tools": tools,
                           f"mcp_servers.{name}.default_tools_approval_mode": "auto"})
        for key, value in config.items():
            args += ["-c", key + "=" + json.dumps(value)]
        return args + ["-"]
    servers = {name: {"type": "http", "url": f"{base}/{route}/",
                     "headers": {"Authorization": "Bearer ${KNOWLEDGE_NATIVE_DRILL_TOKEN}"}}
               for name, route in (("knowledge_layer", "agent/mcp"), ("fixture_source", "source/mcp"))}
    path = root / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    path.chmod(0o600)
    return ["claude", "-p", "--restricted", "--setting-sources", "", "--settings",
            '{"disableAllHooks":true}', "--tools", "", "--strict-mcp-config", "--mcp-config", str(path),
            "--no-session-persistence", "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--allowedTools", ",".join(["mcp__knowledge_layer__" + name for name in READ_TOOLS]
                                       + ["mcp__fixture_source__execute_reviewed_plan"]),
            "--output-format", "stream-json", "--verbose", "--max-budget-usd", "3",
            "--json-schema", json.dumps(ANSWER_SCHEMA)] + (["--model", model] if model else [])


def _parse_output(client, output):
    answer, models, usage, violation, event_types = None, set(), None, False, set()
    native_servers, initialized = set(), False
    mcp_calls = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item") or {}
        message = event.get("message") or {}
        if client == "claude" and event.get("type") == "system" and event.get("subtype") == "init":
            initialized = True
            native_servers = {item.get("name") for item in event.get("mcp_servers", []) if isinstance(item, dict)}
            plugins = event.get("plugins") or []
            if (any(not isinstance(plugin, dict) or plugin.get("name") not in BUILTIN_PLUGINS
                    or plugin.get("path") != "builtin" or plugin.get("source") != plugin["name"] + "@builtin"
                    for plugin in plugins) or event.get("plugin_errors") or event.get("mcp_server_errors")):
                violation = True
        if event.get("subtype") in {"hook_started", "hook_response"}:
            violation = True
        for value in (event.get("type"), item.get("type")):
            if isinstance(value, str) and re.fullmatch(r"[a-z_.]{1,60}", value):
                event_types.add(value)
        if event.get("type") == "item.completed" and item.get("type") == "mcp_tool_call":
            text = json.dumps(item.get("error") or {}).lower()
            server, tool = item.get("server"), item.get("tool")
            mcp_calls.append({"server": server if server in {"knowledge_layer", "fixture_source"} else "other",
                "tool": tool if tool in READ_TOOLS + ["execute_reviewed_plan"] else "other",
                "error": bool(item.get("error")), "error_categories": [word for word in (
                    "approval", "cancel", "denied", "allowed", "transport", "connection", "unsupported",
                    "unknown", "timeout", "auth", "http", "disabled", "invalid", "serialize", "schema") if word in text]})
            if server not in {"knowledge_layer", "fixture_source"} or tool not in READ_TOOLS + ["execute_reviewed_plan"]:
                violation = True
        if isinstance(message.get("model"), str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", message["model"]):
            models.add(message["model"])
        if client == "codex" and event.get("type") == "item.completed" and item.get("type") == "agent_message":
            try:
                answer = json.loads(item.get("text", ""))
            except ValueError:
                pass
        if client == "claude" and event.get("type") == "result":
            answer = event.get("structured_output")
            usage = event.get("usage")
        if event.get("type") == "turn.completed":
            usage = event.get("usage")
        if item.get("type") is not None and item["type"] not in {"agent_message", "reasoning", "mcp_tool_call"}:
            violation = True
        for block in message.get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "")
                allowed = {"mcp__knowledge_layer__" + name for name in READ_TOOLS} | {"mcp__fixture_source__execute_reviewed_plan", "StructuredOutput"}
                if name not in allowed:
                    violation = True
    from jsonschema import validate
    try:
        validate(answer, ANSWER_SCHEMA)
    except Exception as exc:
        raise DrillFailure("invalid_answer") from exc
    if violation:
        raise DrillFailure("unexpected_client_tool")
    if client == "claude" and (not initialized or native_servers != {"knowledge_layer", "fixture_source"}):
        raise DrillFailure("client_init_evidence")
    safe_usage = {key: value for key, value in (usage or {}).items()
                  if key in {"input_tokens", "output_tokens", "cached_input_tokens",
                             "cache_read_input_tokens", "cache_creation_input_tokens"} and type(value) is int}
    return {"answer": answer, "observed_models": sorted(models), "usage": safe_usage,
            "event_types": sorted(event_types), "mcp_calls": mcp_calls}


async def _stop_process_group(process):
    def exists():
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise DrillFailure("cli_cleanup") from exc
        return True

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError as exc:
            raise DrillFailure("cli_cleanup") from exc
        deadline = time.monotonic() + 5
        while exists() and time.monotonic() < deadline:
            # Reap the direct child independently of descendants.
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), 0.05)
                except TimeoutError:
                    pass
            await asyncio.sleep(0.05)
        if not exists():
            break
    if exists():
        raise DrillFailure("cli_cleanup")
    await process.wait()


async def _invoke(client, base, root, token, prompt, timeout, model=None):
    env = os.environ.copy()
    # Keep native authentication in the CLI; never read/copy/print credentials.
    for key in list(env):
        if key.startswith(("KNOWLEDGE_", "DATABASE_URL", "OTEL_", "CLAUDE_CODE_", "CODEX_")):
            env.pop(key)
    env["KNOWLEDGE_NATIVE_DRILL_TOKEN"] = token
    env["DISABLE_TELEMETRY"] = "1"
    env["DISABLE_ERROR_REPORTING"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"
    process = await asyncio.create_subprocess_exec(*_command(client, base, root, model), cwd=root, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)

    async def bounded(stream):
        data = bytearray()
        while chunk := await stream.read(16384):
            data.extend(chunk)
            if len(data) > MAX_OUTPUT:
                raise DrillFailure("cli_output_budget")
        return bytes(data)

    async def communicate():
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()
        stdout, stderr = await asyncio.gather(bounded(process.stdout), bounded(process.stderr))
        await process.wait()
        return stdout, stderr

    try:
        stdout, stderr = await asyncio.wait_for(communicate(), timeout)
        if process.returncode:
            diagnostics = (stdout + stderr).lower()
            details = {"returncode": process.returncode, "diagnostic_categories": [
                word for word in ("model", "not supported", "not found", "auth", "expired", "schema", "budget", "permission")
                if word.encode() in diagnostics], "unknown_flags": [flag.decode() for flag in
                    re.findall(rb"unknown option ['\"]?(--[a-z-]{1,60})", diagnostics)],
                "flag_conflict": b"incompatible" in diagnostics or b"cannot be used" in diagnostics}
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(event, dict) and event.get("type") == "rate_limit_event":
                    rate = event.get("rate_limit_info") or {}
                    if rate.get("status") == "rejected":
                        details["rate_limit_rejected"] = True
                        kind = rate.get("rateLimitType")
                        details["rate_limit_type"] = kind if kind in {"five_hour", "seven_day"} else "other"
            raise DrillFailure("cli_exit", details)
        parsed = _parse_output(client, stdout)
        parsed["startup_diagnostics"] = {word: word.encode() in stderr.lower()
            for word in ("failed", "error", "ready", "unsupported", "invalid", "tool")}
        model = re.search(rb"model: ([A-Za-z0-9._:/-]{1,120})", stderr)
        if model:
            parsed["observed_models"] = sorted(set(parsed["observed_models"]) | {model[1].decode()})
        return parsed
    except TimeoutError as exc:
        raise DrillFailure("cli_timeout") from exc
    finally:
        await _stop_process_group(process)


def _prompt(fixture, phase, receipt=None):
    bindings = {unit: {"connection_id": value["connection_id"], "assertion_id": value["assertion_id"]}
                for unit, value in fixture["bindings"].items()}
    scope = [b["connection_id"] for b in bindings.values()] + [fixture["document_connection_id"]]
    common = ("Use only the supplied knowledge_layer and fixture_source MCP tools. No shell, files, web or agents. "
              "Source data is synthetic. Read current company guidance and respect cohort/missingness semantics. "
              f"Source scope: {json.dumps(scope)}. Metric handles: {json.dumps(bindings)}. "
              "Respond with the required structured JSON; do not invent unavailable values. ")
    if phase == "ready":
        return common + ("Call get_workspace and get_capabilities. Call resolve_guidance with this scope. "
            "Call get_task_context for Revenue with this scope, then revalidate_task_context using its receipt. "
            "For each unit call plan_metric_query with group_by ['region']; pass its source connection and "
            "the revalidated receipt to fixture_source.execute_reviewed_plan. Report each unit's known revenue, "
            "finance missing and zero rows, and whether a combined company total can be established. "
            "Use the returned row-count diagnostic names, not inferred zero filling. Set refused false only if those reads work.")
    return common + ("The steward has withdrawn finance meaning and company guidance since a previous session. "
        f"Cached receipt: {json.dumps(receipt)}. Call resolve_guidance, revalidate_task_context on this exact receipt, "
        "and plan_metric_query for finance with request {\"group_by\":[\"region\"]}. These must be freshly checked; a retained copy grants no authority. "
        "Do not execute source reads after a refusal. Set refused true and every unavailable numeric result null.")


def _config_state():
    # Compare configuration only, never persist its contents or credential hashes.
    paths = [Path.home() / ".codex/config.toml", Path.home() / ".claude/settings.json", Path.home() / ".claude.json"]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None for path in paths}


def run_client(client, timeout, model=None):
    if not shutil.which(client):
        raise DrillFailure("cli_unavailable")
    version = subprocess.run([client, "--version"], capture_output=True, timeout=10, check=True).stdout.decode().strip()
    match = re.search(r"\b\d+\.\d+\.\d+\b", version)
    if not match:
        raise DrillFailure("cli_version")
    observer, executions = Observer(), []
    config_before = _config_state()
    with _fixture() as fixture, tempfile.TemporaryDirectory(prefix="knowledge-native-customer-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        (root / "answer-schema.json").write_text(json.dumps(ANSWER_SCHEMA))
        hashes = {unit: hashlib.sha256(b["database"].read_bytes()).hexdigest() for unit, b in fixture["bindings"].items()}
        token = secrets.token_urlsafe(32)
        with _serve(fixture, observer, token, executions) as base:
            ready = asyncio.run(_invoke(client, base, root, token, _prompt(fixture, "ready"), timeout, model))
            expected = {"finance_known_revenue": 20, "sales_known_revenue": 1040, "combined_revenue": None,
                        "finance_missing_rows": 1, "finance_zero_rows": 1, "refused": False}
            called = {e["tool"] for e in observer.events if e["success"]}
            if ready["answer"] != expected or not set(READ_TOOLS) <= called or not observer.receipt or observer.failure:
                raise DrillFailure("ready_evidence", {"answer": ready["answer"], "tool_events": observer.events,
                    "receipt_observed": bool(observer.receipt), "observer_failed": bool(observer.failure),
                    "wire_events": observer.wire_events, "source_failure": observer.source_failure,
                    "client": ready})
            if set(executions) != {b["connection_id"] for b in fixture["bindings"].values()}:
                raise DrillFailure("source_execution_evidence")
            receipt, count, start = observer.receipt, len(executions), len(observer.events)
            denied_start = len(observer.denied)
            store = fixture["store"]
            store.revoke_publication("demo", fixture["bindings"]["finance"]["connection_id"], reason="Native fixture withdrawal.")
            guidance = fixture["guidance"]
            GuidanceRegistry(store).revoke("demo", guidance["slug"], revision=guidance["revision"], reason="Native fixture withdrawal.")
            withdrawn = asyncio.run(_invoke(client, base, root, token, _prompt(fixture, "withdrawn", receipt), timeout, model))
            events = observer.events[start:]
            finance = fixture["bindings"]["finance"]
            required_denials = {("revalidate_task_context", _digest({"receipt": receipt})),
                ("plan_metric_query", _digest({"connection_id": finance["connection_id"],
                    "assertion_id": finance["assertion_id"], "request": {"group_by": ["region"]}}))}
            if (withdrawn["answer"] != {key: True if key == "refused" else None for key in expected}
                    or not required_denials <= set(observer.denied[denied_start:]) or observer.failure
                    or not any(e["tool"] == "resolve_guidance" for e in events) or len(executions) != count):
                raise DrillFailure("withdrawal_evidence")
        if hashes != {unit: hashlib.sha256(b["database"].read_bytes()).hexdigest() for unit, b in fixture["bindings"].items()}:
            raise DrillFailure("source_changed")
    if root.exists() or fixture["root"].exists():
        raise DrillFailure("fixture_cleanup")
    if config_before != _config_state():
        raise DrillFailure("user_config_changed")
    return {"client": client, "version": match[0], "requested_model": model, "ready": ready, "withdrawn": withdrawn,
            "observed_protocols": sorted(observer.protocols), "tool_events": observer.events,
            "source_execution_count": count, "source_bytes_unchanged": True, "owned_runtime_removed": True,
            "user_config_unchanged": True, "result": "passed"}


def _open_report(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise DrillFailure("unsafe_report")
        os.ftruncate(fd, 0)
        return os.fdopen(fd, "w")
    except BaseException:
        os.close(fd)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("codex", "claude", "both"), required=True)
    parser.add_argument("--codex-model")
    parser.add_argument("--claude-model")
    parser.add_argument("--timeout", type=int, default=240, choices=range(30, 601))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for model in (args.codex_model, args.claude_model):
        if model is not None and (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", model)):
            parser.error("Model identifiers must be bounded plain names")
    try:
        destination = _open_report(args.output)
    except (OSError, DrillFailure):
        print('{"result":"failed","error_code":"unsafe_report"}')
        return 1
    report = {"schema_version": "native-harness-drill-v1", "evaluated_at": datetime.now(UTC).isoformat(),
              "fixture_only": True, "customer_source_access": False, "skill_autoactivation": "not_qualified",
              "customer_oidc": "not_qualified", "clients": [], "result": "failed"}
    with destination as file:
        try:
            for client in (("codex", "claude") if args.client == "both" else (args.client,)):
                report["clients"].append(run_client(client, args.timeout, args.codex_model if client == "codex" else args.claude_model))
            report["result"] = "passed"
        except Exception as exc:  # noqa: BLE001 - fixed codes; raw driver/process text suppressed
            report["error_code"] = str(exc) if isinstance(exc, DrillFailure) else "drill_failed"
            if isinstance(exc, DrillFailure):
                report["failed_checks"] = exc.details
        json.dump(report, file, indent=2)
        file.write("\n")
    print(json.dumps({"result": report["result"], "clients": [c["client"] for c in report["clients"]],
                      "error_code": report.get("error_code")}))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
