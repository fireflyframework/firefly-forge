# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real signed-in CLI and native Firefly Agentic coding providers."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from firefly_forge.profiles import build_instruction

Emit = Callable[[str, str], Awaitable[None]]


@dataclass
class AgentResult:
    summary: str
    provider: str
    usage: dict[str, Any] = field(default_factory=dict)


class ProviderError(RuntimeError):
    pass


class CodingProvider(Protocol):
    async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Emit) -> AgentResult: ...


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _redact(text: str) -> str:
    for name in [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "FORGE_AGENTIC_API_KEY",
        "FORGE_BRIDGE_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    ]:
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "[redacted]")
    return text


async def _stop_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        if process.returncode is None:
            process.terminate()
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=2)
    # Descendants can outlive the leader and keep its output pipes open.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if process.returncode is None:
            process.kill()
    await process.wait()


async def run_process(
    command: list[str],
    workspace: Path,
    emit: Emit,
    *,
    timeout: float,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    """Drain both streams, bound retained output, and kill the process group on cancellation."""
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=1_000_000,
        env=env,
    )
    output = {"stdout": "", "stderr": ""}
    assert process.stdout is not None and process.stderr is not None

    async def drain(stream: asyncio.StreamReader, kind: str) -> None:
        while raw := await stream.readline():
            line = _redact(raw.decode("utf-8", errors="replace"))
            output[kind] = (output[kind] + line)[-1_000_000:]
            await emit(kind, line.rstrip("\n"))

    streams = [
        asyncio.create_task(drain(process.stdout, "stdout")),
        asyncio.create_task(drain(process.stderr, "stderr")),
    ]
    try:
        async with asyncio.timeout(timeout):
            if stdin is not None:
                assert process.stdin is not None
                process.stdin.write(stdin.encode())
                await process.stdin.drain()
                process.stdin.close()
            await asyncio.gather(*streams)
            await process.wait()
    except BaseException as exc:
        await _stop_process_group(process)
        for stream in streams:
            stream.cancel()
        await asyncio.gather(*streams, return_exceptions=True)
        if isinstance(exc, TimeoutError):
            raise ProviderError(f"Coding provider timed out after {timeout:g} seconds") from exc
        raise
    assert process.returncode is not None
    return ProcessResult(process.returncode, output["stdout"], output["stderr"])


def _executable(name: str) -> str:
    configured = os.environ.get(f"FORGE_{name.upper()}_EXECUTABLE")
    result = shutil.which(configured or name)
    if not result:
        raise ProviderError(f"{name.title()} CLI is unavailable. Install the official CLI and sign in first.")
    return result


def _events(stdout: str) -> list[dict[str, Any]]:
    output = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            output.append(item)
    return output


class CodexProvider:
    async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Emit) -> AgentResult:
        prompt = build_instruction(brief, backend, frontend)
        command = [
            _executable("codex"),
            "exec",
            "--json",
            "--ignore-user-config",
            "--sandbox",
            "workspace-write",
            "-c",
            'approval_policy="never"',
            "-C",
            str(workspace.resolve()),
            "-",
        ]
        model = os.environ.get("FORGE_CODEX_MODEL")
        if model:
            command[-1:-1] = ["--model", model]
        await emit("provider", "Using the official Codex CLI with its existing login")
        result = await run_process(
            command, workspace, emit, timeout=float(os.environ.get("FORGE_PROVIDER_TIMEOUT", "1800")), stdin=prompt
        )
        if result.returncode:
            raise ProviderError(f"Codex exited with code {result.returncode}: {result.stderr[-2000:]}")
        summary, usage = "", {}
        for event in _events(result.stdout):
            if event.get("type") == "turn.failed":
                raise ProviderError(f"Codex generation failed: {_redact(str(event.get('error', 'unknown error')))}")
            if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                summary = event["item"].get("text", "")
            if event.get("type") == "turn.completed":
                usage = event.get("usage", {})
        if not summary:
            raise ProviderError("Codex completed without a final implementation report")
        return AgentResult(summary, "codex", usage)


class ClaudeProvider:
    async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Emit) -> AgentResult:
        command = [
            _executable("claude"),
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
            "--permission-prompts",
            "none",
            "--allowedTools",
            (
                "Read,Write,Edit,Glob,Grep,Bash(uv *),Bash(npm *),Bash(pnpm *),Bash(mvn *),Bash(c"
                "omposer *),Bash(php *),Bash(python *),Bash(git status*),Bash(git diff*)"
            ),
            "--disallowedTools",
            "Bash(git commit*),Bash(git push*),Bash(git reset*),Bash(rm *)",
        ]
        model = os.environ.get("FORGE_CLAUDE_MODEL")
        if model:
            command.extend(["--model", model])
        await emit("provider", "Using the official Claude Code CLI with its existing login")
        child_env = dict(os.environ)
        child_env.pop("CLAUDECODE", None)
        result = await run_process(
            command,
            workspace,
            emit,
            timeout=float(os.environ.get("FORGE_PROVIDER_TIMEOUT", "1800")),
            stdin=build_instruction(brief, backend, frontend),
            env=child_env,
        )
        if result.returncode:
            raise ProviderError(f"Claude Code exited with code {result.returncode}: {result.stderr[-2000:]}")
        for event in reversed(_events(result.stdout)):
            if event.get("type") == "result":
                if event.get("is_error") or event.get("subtype", "").startswith("error"):
                    error = event.get("result", event.get("errors", "unknown error"))
                    raise ProviderError(f"Claude Code generation failed: {_redact(str(error))}")
                return AgentResult(event.get("result", ""), "claude", event.get("usage", {}))
        raise ProviderError("Claude Code completed without a final implementation report")


class APIProvider:
    async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Emit) -> AgentResult:
        from firefly_forge.agentic_runtime import execute_agentic

        model = None
        base_url = os.environ.get("FORGE_AGENTIC_BASE_URL")
        if base_url:
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            key = os.environ.get("FORGE_AGENTIC_API_KEY") or os.environ.get("OPENAI_API_KEY")
            if not key:
                raise ProviderError("Set FORGE_AGENTIC_API_KEY for the configured API endpoint")
            model = OpenAIChatModel(
                os.environ.get("FORGE_AGENTIC_MODEL", "gpt-6-luna").split(":")[-1],
                provider=OpenAIProvider(base_url=base_url, api_key=key),
            )
        summary, usage = await execute_agentic(workspace, brief, backend, frontend, emit, model=model)
        return AgentResult(summary, "agentic", usage)


class BridgeProvider:
    async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Emit) -> AgentResult:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.profiles.openai import OpenAIModelProfile
        from pydantic_ai.providers.openai import OpenAIProvider

        from firefly_forge.agentic_runtime import execute_agentic

        base_url = os.environ.get("FORGE_BRIDGE_URL")
        token = os.environ.get("FORGE_BRIDGE_TOKEN")
        if not base_url or not token:
            raise ProviderError("Configure FORGE_BRIDGE_URL and FORGE_BRIDGE_TOKEN before selecting the bridge")
        endpoint = base_url.rstrip("/")
        if not endpoint.endswith("/v1"):
            endpoint += "/v1"
        model = OpenAIChatModel(
            os.environ.get("FORGE_BRIDGE_MODEL", "bridge/default"),
            provider=OpenAIProvider(base_url=endpoint, api_key=token),
            # Subscription CLIs cannot implement an OpenAI output-token cap.
            profile=OpenAIModelProfile(openai_unsupported_model_settings=["max_tokens"]),
        )
        await emit("provider", "Connecting Firefly Agentic to the configured OpenAI-compatible bridge")
        summary, usage = await execute_agentic(
            workspace, brief, backend, frontend, emit, model=model, allow_output_token_cap=False
        )
        return AgentResult(summary, "bridge", usage)


@lru_cache(maxsize=8)
def _login_ready(executable: str, name: str, _cache_bucket: int) -> bool:
    command = [executable, "login", "status"] if name == "codex" else [executable, "auth", "status"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        if name == "claude":
            return result.returncode == 0 and json.loads(result.stdout).get("loggedIn") is True
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False


def provider_catalog() -> list[dict[str, Any]]:
    items = []
    for name, title in [("codex", "Codex · signed-in CLI"), ("claude", "Claude Code · signed-in CLI")]:
        executable = shutil.which(os.environ.get(f"FORGE_{name.upper()}_EXECUTABLE") or name)
        ready = bool(executable and _login_ready(executable, name, int(time.monotonic() // 30)))
        items.append(
            {
                "id": name,
                "name": title,
                "ready": ready,
                "detail": "Official CLI is signed in"
                if ready
                else f"Install {name} and sign in with its official login command",
            }
        )
    bridge_ready = bool(os.environ.get("FORGE_BRIDGE_URL") and os.environ.get("FORGE_BRIDGE_TOKEN"))
    items.append(
        {
            "id": "bridge",
            "name": "Agent bridge",
            "ready": bridge_ready,
            "detail": "Endpoint and token configured; connectivity is checked when running"
            if bridge_ready
            else "Set FORGE_BRIDGE_URL and FORGE_BRIDGE_TOKEN",
        }
    )
    model = os.environ.get("FORGE_AGENTIC_MODEL", "openai-responses:gpt-6-luna")
    keys = ["FORGE_AGENTIC_API_KEY", "OPENAI_API_KEY"]
    if model.startswith("anthropic:"):
        keys = ["ANTHROPIC_API_KEY"]
    elif model.startswith("google"):
        keys = ["GOOGLE_API_KEY", "GEMINI_API_KEY"]
    ready = any(os.environ.get(key) for key in keys)
    items.append(
        {
            "id": "agentic",
            "name": "Firefly Agentic · API",
            "ready": ready,
            "detail": "API environment configured; model access is checked when running"
            if ready
            else "Configure a provider API key and FORGE_AGENTIC_MODEL",
        }
    )
    return items


def get_provider(name: str) -> CodingProvider:
    providers: dict[str, type[CodingProvider]] = {
        "codex": CodexProvider,
        "claude": ClaudeProvider,
        "bridge": BridgeProvider,
        "agentic": APIProvider,
        "api": APIProvider,
    }
    if name not in providers:
        raise ValueError(f"Unknown provider: {name}")
    return providers[name]()
