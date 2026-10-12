# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import json
import os
import sys

import pytest

from firefly_forge.providers import (
    AgentResult,
    CodexProvider,
    ProviderError,
    get_provider,
    provider_catalog,
    run_process,
)


async def discard(kind, message):
    pass


@pytest.mark.asyncio
async def test_process_streaming_retains_actual_exit_and_events(tmp_path):
    events = []

    async def emit(kind, message):
        events.append((kind, message))

    result = await run_process([sys.executable, "-c", 'print("first"); print("second")'], tmp_path, emit, timeout=5)
    assert result.returncode == 0
    assert result.stdout == "first\nsecond\n"
    assert len(events) == 2


@pytest.mark.asyncio
async def test_process_timeout_kills_child_and_fails(tmp_path):
    with pytest.raises(ProviderError, match="timed out"):
        await run_process([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, discard, timeout=0.1)


@pytest.mark.asyncio
async def test_cancellation_terminates_worker_and_its_descendant(tmp_path):
    script = (
        "import subprocess,sys,time,pathlib; "
        'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
        'pathlib.Path("child.pid").write_text(str(child.pid)); time.sleep(30)'
    )
    task = asyncio.create_task(run_process([sys.executable, "-c", script], tmp_path, discard, timeout=30))
    for _ in range(100):
        if (tmp_path / "child.pid").exists():
            break
        await asyncio.sleep(0.01)
    child_pid = int((tmp_path / "child.pid").read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Provider descendant survived cancellation")


@pytest.mark.asyncio
async def test_codex_cli_receives_brief_and_parses_json_final_output(tmp_path, monkeypatch):
    executable = tmp_path / "fake-codex"
    executable.write_text(
        "#!"
        + sys.executable
        + (
            '\nimport json,sys,pathlib\npathlib.Path("args.json").write_text(json.dumps(sys.argv))\n'
            'prompt=sys.stdin.read()\npathlib.Path("prompt.txt").writ'
            'e_text(prompt)\nprint(json.dumps({"type":"item.completed","item":{"type":"agent_m'
            'essage","text":"Inventory built"}}))\nprint(json.dumps({"type":"turn.completed","'
            'usage":{"input_tokens":5,"output_tokens":2}}))\n'
        )
    )
    executable.chmod(0o755)
    monkeypatch.setenv("FORGE_CODEX_EXECUTABLE", str(executable))
    result = await CodexProvider().execute(tmp_path, "Create stock alerts", "pyfly", "none", discard)
    assert result.summary == "Inventory built"
    assert result.usage["output_tokens"] == 2
    assert "Create stock alerts" in (tmp_path / "prompt.txt").read_text()
    assert "--ignore-user-config" in json.loads((tmp_path / "args.json").read_text())


@pytest.mark.asyncio
async def test_codex_nonzero_exit_is_never_reported_as_success(tmp_path, monkeypatch):
    executable = tmp_path / "fake-codex"
    executable.write_text("#!" + sys.executable + '\nimport sys\nprint("login failed",file=sys.stderr)\nsys.exit(7)\n')
    executable.chmod(0o755)
    monkeypatch.setenv("FORGE_CODEX_EXECUTABLE", str(executable))
    with pytest.raises(ProviderError, match="7"):
        await CodexProvider().execute(tmp_path, "Create service", "pyfly", "none", discard)


def test_provider_catalog_never_exposes_environment_credentials(monkeypatch):
    monkeypatch.setenv("FORGE_BRIDGE_TOKEN", "very-secret-token")
    monkeypatch.setenv("OPENAI_API_KEY", "very-secret-key")
    assert "very-secret" not in json.dumps(provider_catalog())


def test_provider_readiness_refreshes_after_official_login(tmp_path, monkeypatch):
    ready = tmp_path / "signed-in"
    executable = tmp_path / "fake-codex-login"
    executable.write_text(
        "#!"
        + sys.executable
        + "\nimport pathlib,sys\n"
        + f"sys.exit(0 if pathlib.Path({str(ready)!r}).exists() else 1)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("FORGE_CODEX_EXECUTABLE", str(executable))
    monkeypatch.setenv("FORGE_CLAUDE_EXECUTABLE", "/does-not-exist")
    monkeypatch.setattr("time.monotonic", lambda: 1.0)
    assert not next(p for p in provider_catalog() if p["id"] == "codex")["ready"]
    ready.write_text("signed in through official CLI")
    monkeypatch.setattr("time.monotonic", lambda: 31.0)
    assert next(p for p in provider_catalog() if p["id"] == "codex")["ready"]


def test_unknown_provider_is_actionable():
    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("unknown")


def test_agent_results_do_not_share_usage_state():
    first, second = AgentResult("one", "codex"), AgentResult("two", "codex")
    first.usage["count"] = 1
    assert second.usage == {}


@pytest.mark.asyncio
async def test_bridge_native_tool_flow_omits_unimplemented_openai_token_cap(tmp_path, monkeypatch):
    import httpx
    from openai import AsyncOpenAI
    from pydantic_ai.providers.openai import OpenAIProvider as OriginalProvider

    from firefly_forge.providers import BridgeProvider

    requests = []

    async def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "max_completion_tokens" not in body
        if len(requests) == 1:
            name, arguments = (
                "write_file",
                {"path": "src/service.py", "content": "def alert(qty):\n    return qty < 5\n"},
            )
        else:
            name = next(
                t["function"]["name"]
                for t in body["tools"]
                if t["function"]["name"] not in {"read_file", "write_file", "list_files"}
            )
            arguments = {"summary": "Generated service"}
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "bridge/default",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"call-{len(requests)}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = AsyncOpenAI(base_url="http://test/v1", api_key="test", http_client=http)
        monkeypatch.setattr(
            "pydantic_ai.providers.openai.OpenAIProvider", lambda **kwargs: OriginalProvider(openai_client=client)
        )
        monkeypatch.setenv("FORGE_BRIDGE_URL", "http://test")
        monkeypatch.setenv("FORGE_BRIDGE_TOKEN", "test")
        result = await BridgeProvider().execute(tmp_path, "Create alert service", "pyfly", "none", discard)
    assert result.summary == "Generated service"
    assert result.usage["requests"] == 2
    assert (tmp_path / "src/service.py").is_file()
