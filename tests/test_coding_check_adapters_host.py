# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Both native adapter command contracts use a local Python fixture, never a model."""

import json
import sys
import time
import tomllib
from pathlib import Path

import pytest

from firefly_forge.providers import ClaudeProvider, CodexProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["codex", "claude"])
async def test_coding_cli_adapter_binds_only_scoped_mcp_without_token_arguments_or_global_config(agent, tmp_path):
    from firefly_forge.coding_check_transport import CodingCheckServer

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "public-fixture-cli"
    executable.write_text(
        "#!" + sys.executable + "\n"
        "import json,os,pathlib,stat,sys\n"
        "args=sys.argv[1:];token=os.environ['FORGE_CODING_RUNTIME_TOKEN']\n"
        "metadata={'argv':args,'has_token':bool(token),'stdin':sys.stdin.read()}\n"
        "if '--mcp-config' in args:\n"
        " p=pathlib.Path(args[args.index('--mcp-config')+1]);value=json.loads(p.read_text())\n"
        " metadata.update(config_path=str(p),config_mode=stat.S_IMODE(p.stat().st_mode),"
        " servers=list(value['mcpServers']),"
        "authorized=value['mcpServers']['forge_coding']['headers']['Authorization']=='Bearer '+token)\n"
        "pathlib.Path('adapter-metadata.json').write_text(json.dumps(metadata))\n"
        "if '--print' in args: print(json.dumps({'type':'result','result':token}))\n"
        "else: print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':token}}))\n"
    )
    executable.chmod(0o755)

    async def unused(request):
        raise AssertionError("No protocol request is made by this argv fixture")

    server = CodingCheckServer(unused, expires_at_ms=int(time.time() * 1000) + 60000)
    server.endpoint = "http://127.0.0.1:19432/mcp"
    settings = {"FORGE_" + agent.upper() + "_EXECUTABLE": str(executable), "FORGE_PROVIDER_TIMEOUT": "5"}
    provider = (CodexProvider if agent == "codex" else ClaudeProvider)(settings)
    provider.configure_coding_checks(server, "Only the explicitly granted configured indices may run.")
    events = []

    async def emit(kind, value):
        events.append((kind, value))

    result = await provider.execute(workspace, "Public fixture", "pyfly", "none", emit)
    metadata = json.loads((workspace / "adapter-metadata.json").read_text())
    assert metadata["has_token"] is True
    assert "explicitly granted configured indices" in metadata["stdin"]
    assert server.token not in json.dumps(metadata) + json.dumps(events) + result.summary
    assert provider.settings == settings
    args = metadata["argv"]
    if agent == "codex":
        assert args[args.index("--sandbox") + 1] == "workspace-write"
        assert "--ignore-user-config" in args
        configuration = next(value for value in args if value.startswith("mcp_servers="))
        assert 'bearer_token_env_var="FORGE_CODING_RUNTIME_TOKEN"' in configuration
        assert 'enabled_tools=["run_configured_check"]' in configuration
        assert "network_access" not in configuration
        parsed = tomllib.loads(configuration)
        assert set(parsed) == {"mcp_servers"}
        assert set(parsed["mcp_servers"]) == {"forge_coding"}
        scoped = parsed["mcp_servers"]["forge_coding"]
        assert scoped["enabled_tools"] == ["run_configured_check"]
        assert scoped.get("tools") == {"run_configured_check": {"approval_mode": "approve"}}
        assert "default_tools_approval_mode" not in scoped
        approval = next(value for value in args if value.startswith("approval_policy="))
        assert tomllib.loads(approval) == {"approval_policy": "never"}
    else:
        assert "--strict-mcp-config" in args
        assert "mcp__forge_coding__run_configured_check" in args[args.index("--allowedTools") + 1]
        assert metadata["servers"] == ["forge_coding"] and metadata["authorized"]
        path = Path(metadata["config_path"])
        assert metadata["config_mode"] == 0o600 and not path.is_relative_to(workspace)
        assert not path.exists()
