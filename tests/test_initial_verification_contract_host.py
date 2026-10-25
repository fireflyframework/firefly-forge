# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Observe real provider prompt boundaries without CLI, model, network or database calls."""

import json

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from firefly_forge import providers
from firefly_forge.engine import Engine


@pytest.mark.parametrize("agent", ["codex", "claude", "bridge", "api"])
@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("command_case", ["frozen", "detect-later", "credential-refused"])
async def test_initial_and_resumed_providers_receive_frozen_verification_contract(
    monkeypatch, tmp_path, agent, resumed, command_case
):
    from test_bridge_inflight_cancel import RUN, MemoryStore

    store = MemoryStore(tmp_path / "memory")
    commands = [
        {"argv": ["uv", "run", "pytest", "tests/check with space.py"], "timeout": 137},
        {"argv": ["npm", "run", "typecheck"], "timeout": 241},
    ]
    if command_case == "detect-later":
        commands = []
    elif command_case == "credential-refused":
        commands[0]["argv"].append("fictional-private-command-token")
    store.run.data.update(agent=agent, commands=commands, resume_existing=resumed)
    captured = []
    provider = providers.get_provider(agent, {})

    async def stop_cli(argv, cwd, emit, **kwargs):
        captured.append(kwargs["stdin"])
        assert "forge_coding" not in " ".join(argv)
        raise providers.ProviderError("Controlled prompt capture; no CLI launched")

    def stop_model(messages, info):
        user_parts = [
            part.content
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        ]
        assert user_parts
        captured.append("\n".join([info.instructions or "", *user_parts]))
        assert "run_configured_check" not in {tool.name for tool in info.function_tools}
        raise providers.ProviderError("Controlled prompt capture; no model contacted")

    monkeypatch.setattr(providers, "run_process", stop_cli)
    monkeypatch.setattr(providers, "_executable", lambda name, settings: name)
    if agent in {"bridge", "api"}:
        monkeypatch.setattr(provider, "model", lambda: FunctionModel(stop_model))
    engine = Engine(store, tmp_path / "workspaces", lambda _: provider)
    engine.credential_registry = lambda: {"FORGE_BRIDGE_TOKEN": "fictional-private-command-token"}
    workspace = engine.workspaces / RUN
    workspace.mkdir()
    (workspace / "main.py").write_text("VALUE = 1\n")

    async def inspect(*args):
        return {"base_revision": "b" * 40, "head": "b" * 40, "source_tree": "c" * 40}

    async def clone(*args, **kwargs):
        return "b" * 40

    async def checked(argv, *args, **kwargs):
        assert argv == ["git", "ls-files", "-z"]
        return ""

    monkeypatch.setattr(engine, "_inspect_workspace", inspect)
    monkeypatch.setattr("firefly_forge.engine.clone", clone)
    monkeypatch.setattr("firefly_forge.engine.checked", checked)
    await engine.execute_run(RUN)
    if command_case == "credential-refused":
        assert captured == []
        assert "configured credential" in store.run.data["error"]
        return
    assert len(captured) == 1, store.run.data
    prompt = captured[0]
    start = prompt.index("FROZEN VERIFICATION CONTRACT (read-only):")
    contract = prompt[start:].split("END FROZEN VERIFICATION CONTRACT", 1)[0]
    assert json.loads(contract.splitlines()[1]) == commands
    assert "20" in contract and "independent final verification" in contract
    assert "does not grant tools, commands, credentials, network access or execution permission" in contract
    assert "run_configured_check" not in contract
    assert "fictional-private-command-token" not in prompt
    assert store.run.data["commands"] == commands
    assert provider.coding_checks is None
    assert store.run.data["repair_budget"] == 0
