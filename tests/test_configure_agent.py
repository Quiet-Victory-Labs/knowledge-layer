"""Onboarding preserves other servers and never embeds a credential."""
import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("configure_agent", Path(__file__).parents[1] / "scripts/configure_agent.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize("client", ["codex", "claude", "cursor"])
def test_configuration_is_additive_and_idempotent(tmp_path, client):
    target = tmp_path / module.PATHS[client]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('[mcp_servers.existing]\nurl="https://existing.example/mcp"\n' if client == "codex" else
                      json.dumps({"mcpServers": {"existing": {"url": "https://existing.example/mcp"}}}))
    result = module.configure(client=client, project=tmp_path, url="http://127.0.0.1:4173/agent/mcp/")
    content = result.read_text()
    config = tomllib.loads(content) if client == "codex" else json.loads(content)
    servers = config["mcp_servers" if client == "codex" else "mcpServers"]
    assert servers["existing"]["url"] == "https://existing.example/mcp"
    assert servers["knowledge_layer"]["url"].endswith("/agent/mcp/")
    assert "KNOWLEDGE_AGENT_TOKEN" in content
    backups = list(target.parent.glob(target.name + ".backup-*"))
    assert len(backups) == 1
    module.configure(client=client, project=tmp_path, url="http://127.0.0.1:4173/agent/mcp/")
    assert list(target.parent.glob(target.name + ".backup-*")) == backups
    with pytest.raises(ValueError, match="already exists"):
        module.configure(client=client, project=tmp_path, url="https://new.example/agent/mcp/")
    module.configure(client=client, project=tmp_path, url="https://new.example/agent/mcp/", replace=True)
    assert "https://new.example/agent/mcp/" in target.read_text()
    assert "https://existing.example/mcp" in target.read_text()


@pytest.mark.parametrize("url", ["http://remote.example/mcp", "https://secret@example.com/mcp", "https://example.com/mcp?token=x"])
def test_unsafe_endpoint_refuses_without_creating_file(tmp_path, url):
    with pytest.raises(ValueError):
        module.configure(client="codex", project=tmp_path, url=url)
    assert list(tmp_path.iterdir()) == []


def test_symlink_parent_refused(tmp_path):
    other = tmp_path / "actual"
    other.mkdir()
    (tmp_path / ".cursor").symlink_to(other)
    with pytest.raises(ValueError, match="symlinks"):
        module.configure(client="cursor", project=tmp_path, url="https://example.com/mcp")
