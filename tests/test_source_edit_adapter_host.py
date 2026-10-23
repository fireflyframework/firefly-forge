# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The official source-edit invocation has no optional execution or inherited MCP surface."""

import json
import sys
import time
import tomllib

from source_edit_qualification_fixtures import register_fixture_profile

from firefly_forge.coding_check_transport import CodingCheckServer
from firefly_forge.providers import CodexProvider


async def test_source_edit_adapter_supplies_closed_execution_and_hook_policy(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    executable = tmp_path / "fixture-cli"
    executable.write_text(
        "#!" + sys.executable + "\nimport json,os,pathlib,sys\n"
        "assert os.environ.get('CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED') == '1'\n"
        "pathlib.Path('args.json').write_text(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'source changed'}}))\n"
    )
    executable.chmod(0o700)
    profile = register_fixture_profile(monkeypatch, executable)

    async def read(request):
        raise AssertionError("This fixture only inspects native adapter input")

    server = CodingCheckServer(None, source_callback=read, expires_at_ms=int(time.time() * 1000) + 30000)
    server.endpoint = "http://127.0.0.1:19433/mcp"
    provider = CodexProvider(
        {"FORGE_CODEX_EXECUTABLE": str(executable), "FORGE_CODEX_MODEL": profile.model, "FORGE_PROVIDER_TIMEOUT": "5"}
    )
    assert hasattr(provider, "configure_source_edits")
    provider.configure_source_edits(server, "Only source readers and native patch.")

    async def emit(*args):
        return None

    await provider.execute(workspace, "A public fixture", "pyfly", "none", emit)
    args = json.loads((workspace / "args.json").read_text())
    config = tomllib.loads("\n".join(args[n + 1] for n, arg in enumerate(args[:-1]) if arg == "-c"))
    assert "--sandbox" not in args and "--ignore-user-config" in args
    from firefly_forge.source_edit_filesystem_policy import build_named_permissions, source_private_roots

    assert config["default_permissions"] == "forge_source_edit"
    assert config["permissions"] == build_named_permissions(workspace, source_private_roots())
    assert "--strict-config" in args, "Source-edit startup must fail closed after its metadata qualification"
    assert config["approval_policy"] == "never"
    assert config["model_provider"] == profile.native_provider
    assert isinstance(config.get("model_catalog_json"), str)
    assert config.get("agents") == {"enabled": False}
    for name in (
        "shell_tool",
        "hooks",
        "multi_agent",
        "multi_agent_v2",
        "apps",
        "plugins",
        "remote_plugin",
        "tool_suggest",
        "image_generation",
        "request_permissions_tool",
        "skill_mcp_dependency_install",
    ):
        assert config["features"][name] is False
    assert config["notify"] == [] and config["hooks"] == {}
    assert config["web_search"] == "disabled"
    assert config["cloud"]["skills"]["enabled"] is False
    servers = config["mcp_servers"]
    assert set(servers) == {"forge_coding"}
    assert servers["forge_coding"]["enabled_tools"] == ["list_files", "read_file", "search_text"]
    assert servers["forge_coding"].get("omit_tools_from") == ["deferred", "code_mode"]
    assert set(servers["forge_coding"]["tools"]) == {"list_files", "read_file", "search_text"}
    assert server.token not in json.dumps(args)
