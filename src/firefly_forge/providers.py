# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real signed-in CLI and native Firefly Agentic coding providers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from firefly_forge.configuration import (
    DEFAULTS,
    FIELDS,
    ConfigurationStore,
    normalize_provider,
    public_settings,
    validate_endpoint,
)
from firefly_forge.profiles import build_instruction
from firefly_forge.search_tools import SearchProviderError
from firefly_forge.secrets import redact_environment, runtime_environment
from firefly_forge.store import now

Emit = Callable[[str, str], Awaitable[None]]


@dataclass
class AgentResult:
    summary: str
    provider: str
    usage: dict[str, Any] = field(default_factory=dict)


class ProviderError(RuntimeError):
    pass


class ProviderLimitError(ProviderError):
    def __init__(
        self,
        message: str,
        *,
        kind: Literal["rate_limit", "quota", "authentication", "transient", "provider_paused"],
        retry_after: float | None = None,
        observation: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.observation = observation or {}


class ProviderOutcomeUnknown(ProviderError):
    """A Bridge submission has no reliable terminal result; never submit it again implicitly."""

    def __init__(self, message: str, *, observation: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.observation = observation or {}


class ProviderSubmissionCancelled(asyncio.CancelledError):
    """Local cancellation cannot establish the outcome of an attempted Bridge submission."""


@dataclass
class _BridgeInvocation:
    pending: set[object] = field(default_factory=set)


@dataclass
class _BridgeAttempt:
    invocation: _BridgeInvocation | None
    key: object = field(default_factory=object)

    def dispatch(self) -> None:
        if self.invocation is not None:
            self.invocation.pending.add(self.key)

    def complete(self) -> None:
        if self.invocation is not None:
            self.invocation.pending.discard(self.key)


# Graph subtasks inherit the object, so their mutations survive cancellation unwrapping.
_bridge_invocation: ContextVar[_BridgeInvocation | None] = ContextVar("bridge_invocation", default=None)
_bridge_attempt: ContextVar[_BridgeAttempt | None] = ContextVar("bridge_attempt", default=None)


def _bridge_failure(exc: Exception) -> ProviderError | None:
    from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

    if not isinstance(exc, ModelAPIError):
        return None
    headers = getattr(exc, "headers", None) or {}
    body = getattr(exc, "body", None)
    body = body.get("error", body) if isinstance(body, dict) else {}
    body = body if isinstance(body, dict) else {}
    observation: dict[str, Any] = {}
    job = body.get("job_id") or headers.get("x-bridge-job-id")
    if isinstance(job, str) and len(job) == 36:
        with suppress(ValueError):
            observation["job_id"] = str(uuid.UUID(job))
    try:
        retry_after = float(headers.get("retry-after", "nan"))
        if math.isfinite(retry_after) and 0 <= retry_after <= 86400:
            observation["retry_after"] = retry_after
    except (ValueError, TypeError):
        pass
    if isinstance(exc, ModelHTTPError):
        code = body.get("code")
        if exc.status_code == 429 and code == "quota_exhausted":
            return ProviderLimitError(
                "Bridge quota is exhausted; inspect the retained job before an explicit resume",
                kind="quota",
                retry_after=observation.get("retry_after"),
                observation=observation,
            )
        if exc.status_code == 503 and code == "execution_paused":
            return ProviderLimitError(
                "Bridge execution is paused; inspect the retained native session before an explicit resume",
                kind="provider_paused",
                retry_after=observation.get("retry_after"),
                observation=observation,
            )
        if exc.status_code < 500:
            return None
    return ProviderOutcomeUnknown(
        "Bridge execution outcome is unknown; reconcile the retained job before continuing",
        observation=observation,
    )


def classify_provider_failure(exc: Exception) -> tuple[str, float | None] | None:
    if isinstance(exc, ProviderOutcomeUnknown):
        return "provider_outcome_unknown", exc.observation.get("retry_after")
    if isinstance(exc, ProviderLimitError):
        return exc.kind, exc.retry_after
    if isinstance(exc, SearchProviderError) and exc.kind in {"rate_limit", "quota", "authentication"}:
        return exc.kind, None
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    body = str(getattr(exc, "body", "")).lower()
    if status == 429:
        if any(code in body for code in ("insufficient_quota", "insufficient_credits", "credit_balance")):
            return "quota", None
        delay = 60.0
        try:
            headers = response.headers if response is not None else getattr(exc, "headers", {})
            delay = float(headers.get("retry-after", "60"))
        except (ValueError, TypeError):
            pass
        return "rate_limit", max(1, min(delay, 86400))
    if status in {401, 403}:
        return "authentication", None
    if status in {500, 502, 503, 504}:
        return "transient", 5.0
    if isinstance(exc, ProviderError) and "timed out" in str(exc).lower():
        return "transient", 5.0
    return None


def _raise_cli_failure(message: str) -> None:
    lower = message.lower()
    if any(
        code in lower
        for code in ("usage_limit_reached", "insufficient_quota", "insufficient_credits", "credit balance")
    ):
        raise ProviderLimitError(message, kind="quota")
    if any(code in lower for code in ("rate_limit_exceeded", "too many requests", "http 429")):
        raise ProviderLimitError(message, kind="rate_limit", retry_after=60)
    if any(code in lower for code in ("not logged in", "authentication_error", "invalid_api_key", "unauthorized")):
        raise ProviderLimitError(message, kind="authentication")
    raise ProviderError(message)


class CodingProvider(Protocol):
    async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Emit) -> AgentResult: ...


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _redact(text: str) -> str:
    return redact_environment(text)


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
        env=env if env is not None else runtime_environment(),
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


def _executable(name: str, settings: dict[str, str] | None = None) -> str:
    configured = (settings if settings is not None else os.environ).get(f"FORGE_{name.upper()}_EXECUTABLE")
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


class ConfiguredProvider:
    def __init__(self, settings: dict[str, str] | None = None) -> None:
        self.settings = settings if settings is not None else dict(os.environ)
        self.runtime_context: Any = None

    def configure_runtime(self, **kwargs: Any) -> None:
        from firefly_forge.workspace_tools import ToolRuntimeContext

        if kwargs.get("allowed_tools") is not None:
            kwargs["allowed_tools"] = frozenset(kwargs["allowed_tools"])
        self.runtime_context = ToolRuntimeContext(**kwargs)

    async def image_input_capability(self, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
        return {"status": "unverified"}

    async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Emit) -> AgentResult:
        raise NotImplementedError


class CodexProvider(ConfiguredProvider):
    async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Emit) -> AgentResult:
        prompt = build_instruction(brief, backend, frontend)
        command = [
            _executable("codex", self.settings),
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
        model = self.settings.get("FORGE_CODEX_MODEL")
        if model:
            command[-1:-1] = ["--model", model]
        await emit("provider", "Using the official Codex CLI with its existing login")
        result = await run_process(
            command, workspace, emit, timeout=float(self.settings.get("FORGE_PROVIDER_TIMEOUT", "1800")), stdin=prompt
        )
        if result.returncode:
            _raise_cli_failure(f"Codex exited with code {result.returncode}: {result.stderr[-2000:]}")
        summary, usage = "", {}
        for event in _events(result.stdout):
            if event.get("type") == "turn.failed":
                _raise_cli_failure(f"Codex generation failed: {_redact(str(event.get('error', 'unknown error')))}")
            if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                summary = event["item"].get("text", "")
            if event.get("type") == "turn.completed":
                usage = event.get("usage", {})
        if not summary:
            raise ProviderError("Codex completed without a final implementation report")
        return AgentResult(summary, "codex", usage)


class ClaudeProvider(ConfiguredProvider):
    async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Emit) -> AgentResult:
        command = [
            _executable("claude", self.settings),
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
        model = self.settings.get("FORGE_CLAUDE_MODEL")
        if model:
            command.extend(["--model", model])
        await emit("provider", "Using the official Claude Code CLI with its existing login")
        child_env = runtime_environment()
        child_env.pop("CLAUDECODE", None)
        result = await run_process(
            command,
            workspace,
            emit,
            timeout=float(self.settings.get("FORGE_PROVIDER_TIMEOUT", "1800")),
            stdin=build_instruction(brief, backend, frontend),
            env=child_env,
        )
        if result.returncode:
            _raise_cli_failure(f"Claude Code exited with code {result.returncode}: {result.stderr[-2000:]}")
        for event in reversed(_events(result.stdout)):
            if event.get("type") == "result":
                if event.get("is_error") or event.get("subtype", "").startswith("error"):
                    error = event.get("result", event.get("errors", "unknown error"))
                    _raise_cli_failure(f"Claude Code generation failed: {_redact(str(error))}")
                return AgentResult(event.get("result", ""), "claude", event.get("usage", {}))
        raise ProviderError("Claude Code completed without a final implementation report")


class APIProvider(ConfiguredProvider):
    def model(self) -> Any:
        model: Any = None
        base_url = self.settings.get("FORGE_AGENTIC_BASE_URL")
        if base_url:
            base_url = validate_endpoint(base_url)
        key = self.settings.get("FORGE_AGENTIC_API_KEY")
        model_name = self.settings.get("FORGE_AGENTIC_MODEL", "openai-responses:gpt-6-luna")
        if not key:
            keys = ["OPENAI_API_KEY"]
            if model_name.startswith("anthropic:"):
                keys = ["ANTHROPIC_API_KEY"]
            elif model_name.startswith("google"):
                keys = ["GOOGLE_API_KEY", "GEMINI_API_KEY"]
            key = next((self.settings[k] for k in keys if self.settings.get(k)), "")
        if not key:
            raise ProviderLimitError("Configure the selected model API key before execution", kind="authentication")
        supported_families = {"openai", "openai-chat", "openai-responses", "anthropic", "google", "google-gla"}
        if not base_url and ":" in model_name and model_name.split(":", 1)[0] not in supported_families:
            raise ProviderError(
                "Unsupported native model family; use OpenAI, Anthropic, Google GLA or an explicit compatible endpoint"
            )
        if base_url or key:
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            if not key:
                raise ProviderError("Set FORGE_AGENTIC_API_KEY for the configured API endpoint")
            name = model_name.split(":")[-1]
            if model_name.startswith("anthropic:") and not base_url:
                from pydantic_ai.models.anthropic import AnthropicModel
                from pydantic_ai.providers.anthropic import AnthropicProvider

                model = AnthropicModel(name, provider=AnthropicProvider(api_key=key))
            elif model_name.startswith("google") and not base_url:
                from pydantic_ai.models.google import GoogleModel
                from pydantic_ai.providers.google import GoogleProvider

                model = GoogleModel(name, provider=GoogleProvider(api_key=key))
            elif model_name.startswith("openai-responses:") and not base_url:
                from pydantic_ai.models.openai import OpenAIResponsesModel

                model = OpenAIResponsesModel(name, provider=OpenAIProvider(api_key=key))
            else:
                model = OpenAIChatModel(name, provider=OpenAIProvider(base_url=base_url, api_key=key))
        return model

    async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Emit) -> AgentResult:
        from firefly_forge.agentic_runtime import execute_agentic

        summary, usage = await execute_agentic(
            workspace,
            brief,
            backend,
            frontend,
            emit,
            model=self.model(),
            settings=self.settings,
            runtime_context=self.runtime_context,
        )
        return AgentResult(summary, "agentic", usage)


class BridgeProvider(ConfiguredProvider):
    async def image_input_capability(self, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
        if client is None:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as http:
                return await self.image_input_capability(client=http)
        token = self.settings.get("FORGE_BRIDGE_TOKEN")
        endpoint = self.settings.get("FORGE_BRIDGE_URL")
        if not token or not endpoint:
            return {"status": "unverified"}
        try:
            endpoint = validate_endpoint(endpoint)
            if not endpoint.endswith("/v1"):
                endpoint += "/v1"
            async with asyncio.timeout(5):
                async with client.stream(
                    "GET", endpoint + "/models", headers={"Authorization": "Bearer " + token}
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 100_000:
                            return {"status": "unverified"}
            payload = json.loads(body)
            model = self.settings.get("FORGE_BRIDGE_MODEL", "bridge/default")
            for item in payload.get("data", []):
                if not isinstance(item, dict) or item.get("id") != model:
                    continue
                capability = item.get("image_input")
                if not isinstance(capability, dict):
                    break
                revision = capability.get("config_revision")
                if (
                    capability.get("status") == "verified"
                    and isinstance(revision, str)
                    and 1 <= len(revision) <= 128
                    and capability.get("max_images", 0) >= 5
                    and capability.get("max_decoded_bytes", 0) >= 6_000_000
                ):
                    return {"status": "verified", "provider": "bridge", "model": model, "config_revision": revision}
                break
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, TimeoutError):
            pass
        return {"status": "unverified"}

    def model(self, *, image_capability: dict[str, Any] | None = None) -> Any:
        from openai import AsyncOpenAI
        from pydantic_ai.models.openai import OpenAIChatModel, OpenAIStreamedResponse
        from pydantic_ai.profiles.openai import OpenAIModelProfile
        from pydantic_ai.providers.openai import OpenAIProvider

        base_url = self.settings.get("FORGE_BRIDGE_URL")
        token = self.settings.get("FORGE_BRIDGE_TOKEN")
        if not base_url or not token:
            raise ProviderError("Configure FORGE_BRIDGE_URL and FORGE_BRIDGE_TOKEN before selecting the bridge")
        endpoint = validate_endpoint(base_url)
        if not endpoint.endswith("/v1"):
            endpoint += "/v1"
        name = self.settings.get("FORGE_BRIDGE_MODEL", "bridge/default")
        capability = (
            image_capability
            if image_capability is not None
            else self.runtime_context.image_capability
            if self.runtime_context
            else {}
        )
        revision = capability.get("config_revision")
        headers = {}
        if (
            capability.get("status") == "verified"
            and capability.get("model") == name
            and isinstance(revision, str)
            and len(revision) == 64
            and all(character in "0123456789abcdef" for character in revision)
        ):
            headers["X-Bridge-Image-Revision"] = revision

        class BridgeClient(AsyncOpenAI):
            async def _send_request(self, request: Any, **kwargs: Any) -> Any:
                completion = request.method == "POST" and request.url.path.rstrip("/").endswith("/chat/completions")
                if completion and (attempt := _bridge_attempt.get()) is not None:
                    attempt.dispatch()
                try:
                    return await super()._send_request(request, **kwargs)
                except asyncio.CancelledError as exc:
                    if completion:
                        raise ProviderSubmissionCancelled(
                            "Bridge submission cancelled before its outcome was known"
                        ) from exc
                    raise

        class BridgeStreamedResponse(OpenAIStreamedResponse):
            bridge_complete = False
            bridge_attempt: _BridgeAttempt | None = None

            async def _get_event_iterator(self) -> AsyncIterator[Any]:
                try:
                    async for event in super()._get_event_iterator():
                        yield event
                    self.bridge_complete = True
                    if self.bridge_attempt is not None:
                        self.bridge_attempt.complete()
                except asyncio.CancelledError as exc:
                    raise ProviderSubmissionCancelled("Bridge stream cancelled before completion") from exc
                except Exception as exc:
                    raise (
                        _bridge_failure(exc)
                        or ProviderOutcomeUnknown(
                            "Bridge returned an unusable stream; reconcile the retained job before continuing"
                        )
                    ) from exc

        class BridgeChatModel(OpenAIChatModel):
            @property
            def _streamed_response_cls(self) -> type[OpenAIStreamedResponse]:
                return BridgeStreamedResponse

            def _process_response(self, *args: Any, **kwargs: Any) -> Any:
                try:
                    return super()._process_response(*args, **kwargs)
                except Exception as exc:
                    raise ProviderOutcomeUnknown(
                        "Bridge returned an unusable response; reconcile the retained job before continuing"
                    ) from exc

            async def _process_streamed_response(self, *args: Any, **kwargs: Any) -> Any:
                try:
                    response = await super()._process_streamed_response(*args, **kwargs)
                    assert isinstance(response, BridgeStreamedResponse)
                    response.bridge_attempt = _bridge_attempt.get()
                    return response
                except asyncio.CancelledError as exc:
                    raise ProviderSubmissionCancelled("Bridge stream cancelled before completion") from exc
                except Exception as exc:
                    raise (
                        _bridge_failure(exc)
                        or ProviderOutcomeUnknown(
                            "Bridge returned an unusable stream; reconcile the retained job before continuing"
                        )
                    ) from exc

            async def request(self, *args: Any, **kwargs: Any) -> Any:
                attempt = _BridgeAttempt(_bridge_invocation.get())
                token = _bridge_attempt.set(attempt)
                try:
                    response = await super().request(*args, **kwargs)
                    attempt.complete()
                    return response
                except Exception as exc:
                    if failure := _bridge_failure(exc):
                        if isinstance(failure, ProviderLimitError):
                            attempt.complete()
                        raise failure from exc
                    raise
                finally:
                    _bridge_attempt.reset(token)

            @asynccontextmanager
            async def request_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
                stream = None
                attempt = _BridgeAttempt(_bridge_invocation.get())
                token = _bridge_attempt.set(attempt)
                try:
                    async with super().request_stream(*args, **kwargs) as stream:
                        yield stream
                except ProviderSubmissionCancelled:
                    raise
                except asyncio.CancelledError as exc:
                    if stream is not None and not getattr(stream, "bridge_complete", False):
                        raise ProviderSubmissionCancelled("Bridge stream cancelled before completion") from exc
                    raise
                except Exception as exc:
                    if failure := _bridge_failure(exc):
                        if isinstance(failure, ProviderLimitError):
                            attempt.complete()
                        raise failure from exc
                    raise
                finally:
                    _bridge_attempt.reset(token)

        return BridgeChatModel(
            name,
            provider=OpenAIProvider(
                openai_client=BridgeClient(base_url=endpoint, api_key=token, default_headers=headers, max_retries=0)
            ),
            # Subscription CLIs cannot implement an OpenAI output-token cap.
            profile=OpenAIModelProfile(openai_unsupported_model_settings=["max_tokens"]),
        )

    async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Emit) -> AgentResult:
        from firefly_forge.agentic_runtime import execute_agentic

        invocation = _BridgeInvocation()
        token = _bridge_invocation.set(invocation)
        try:
            await emit("provider", "Connecting Firefly Agentic to the configured OpenAI-compatible bridge")
            summary, usage = await execute_agentic(
                workspace,
                brief,
                backend,
                frontend,
                emit,
                model=self.model(),
                allow_output_token_cap=False,
                settings=self.settings,
                runtime_context=self.runtime_context,
            )
            return AgentResult(summary, "bridge", usage)
        except asyncio.CancelledError as exc:
            if invocation.pending:
                raise ProviderSubmissionCancelled("Bridge submission cancelled before its outcome was known") from exc
            raise
        finally:
            _bridge_invocation.reset(token)


@lru_cache(maxsize=8)
def _login_ready(executable: str, name: str, _cache_bucket: int) -> bool:
    command = [executable, "login", "status"] if name == "codex" else [executable, "auth", "status"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=5, check=False, env=runtime_environment()
        )
        if name == "claude":
            return result.returncode == 0 and json.loads(result.stdout).get("loggedIn") is True
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False


def _environment_settings(name: str) -> dict[str, str]:
    result = {env: os.environ.get(env, DEFAULTS.get(env, "")) for env in FIELDS[name].values()}
    if name == "agentic" and not result["FORGE_AGENTIC_API_KEY"]:
        aliases = ["OPENAI_API_KEY"]
        if result["FORGE_AGENTIC_MODEL"].startswith("anthropic:"):
            aliases = ["ANTHROPIC_API_KEY"]
        elif result["FORGE_AGENTIC_MODEL"].startswith("google"):
            aliases = ["GOOGLE_API_KEY", "GEMINI_API_KEY"]
        result["FORGE_AGENTIC_API_KEY"] = next((os.environ[k] for k in aliases if os.environ.get(k)), "")
    return result


_connection_checks: dict[str, dict[str, Any]] = {}


def _fingerprint(name: str, settings: dict[str, str]) -> str:
    return hashlib.sha256((name + json.dumps(settings, sort_keys=True)).encode()).hexdigest()


def provider_catalog(configuration: ConfigurationStore | None = None) -> list[dict[str, Any]]:
    items = []
    titles = {
        "codex": "Codex · signed-in CLI",
        "claude": "Claude Code · signed-in CLI",
        "bridge": "Agent bridge",
        "agentic": "Firefly Agentic · API",
    }
    setup = {
        "codex": "https://developers.openai.com/codex/cli/",
        "claude": "https://code.claude.com/docs/en/setup",
        "agentic": "https://github.com/fireflyframework/fireflyframework-agentic",
        "bridge": "https://github.com/fireflyframework/firefly-forge/blob/main/docs/bridge.md",
    }
    for name in FIELDS:
        settings = configuration.resolve(name) if configuration else _environment_settings(name)
        config = public_settings(name, settings)
        cli = name in {"codex", "claude"}
        model = str(config["model"])
        endpoint = str(config.get("endpoint", ""))
        ready = (
            bool(config.get("api_key_configured"))
            if name == "agentic"
            else bool(endpoint and config.get("token_configured"))
        )
        detail = (
            "Configuration is present; run a connection test and then a real generation to verify access"
            if ready
            else "Configure the required connection fields"
        )
        state = "configured" if ready else "missing"
        configured = ready
        checked_at = None
        if cli:
            executable = shutil.which(str(config.get("executable")) or name)
            ready = bool(executable and _login_ready(executable, name, int(time.monotonic() // 30)))
            configured = bool(executable)
            state = "authenticated" if ready else "missing"
            checked_at = now()
            detail = (
                "Official CLI is signed in; model execution still requires a real run"
                if ready
                else f"Install {name} and sign in using its official login"
            )
        check = _connection_checks.get(_fingerprint(name, settings))
        if check:
            state, checked_at, detail = check["state"], check["checked_at"], check["detail"]
            ready = state in {"authenticated", "connected", "configured"}
        if config.get("endpoint_invalid"):
            ready, state, detail = False, "failed", "Endpoint configuration is invalid; use a credential-free HTTPS URL"
        fields = []
        for key in FIELDS[name]:
            placeholder = {
                "model": "CLI default" if cli else DEFAULTS.get(FIELDS[name][key], ""),
                "endpoint": "https://api.example.com/v1",
                "executable": name,
                "timeout": "1800",
                "max_tokens": "16384",
            }.get(key, "Saved securely; leave blank to retain")
            fields.append(
                {
                    "key": key,
                    "label": key.replace("_", " ").title(),
                    "secret": key in {"api_key", "token"},
                    "required": key
                    in (
                        {"endpoint", "token"}
                        if name == "bridge"
                        else {"api_key", "model"}
                        if name == "agentic"
                        else set()
                    ),
                    "placeholder": placeholder,
                }
            )
        items.append(
            {
                "id": name,
                "name": titles[name],
                "ready": ready,
                "configured": configured,
                "detail": detail,
                "model": model or "Official CLI default",
                "endpoint": endpoint,
                "auth_mode": "official_cli_login"
                if cli
                else "endpoint_bearer"
                if name == "bridge"
                else "provider_api_key",
                "capabilities": ["source_edits", "repair_from_checks", "structured_results", "isolated_workspace"]
                + (["native_cli_tools"] if cli else ["native_firefly_agentic", "confined_file_tools"]),
                "tools": ["Read", "Write", "Edit", "Glob", "Grep", "Bash (permission bounded)"]
                if name == "claude"
                else ["Official Codex workspace tools"]
                if name == "codex"
                else ["list_files", "read_file", "write_file"],
                "setup_url": setup[name],
                "fields": fields,
                "config": config,
                "readiness": {"state": state, "checked_at": checked_at, "detail": detail},
            }
        )
    return items


async def test_provider_connection(name: str, configuration: ConfigurationStore, client: Any = None) -> dict[str, Any]:
    import httpx

    name = normalize_provider(name)
    settings = configuration.resolve(name)
    state, detail = "missing", "Configure the required connection fields first"
    if name in {"codex", "claude"}:
        try:
            executable = _executable(name, settings)
            # Explicit connection tests always bypass the short catalog cache.
            authenticated = await asyncio.to_thread(_login_ready.__wrapped__, executable, name, 0)
            state = "authenticated" if authenticated else "failed"
            detail = (
                "Official CLI login verified; generation and chosen model require a real run"
                if authenticated
                else "Official CLI login is unavailable; sign in and test again"
            )
        except ProviderError:
            state, detail = "failed", "Official CLI was not found; install it or set its executable path"
    else:
        endpoint = settings.get("FORGE_BRIDGE_URL" if name == "bridge" else "FORGE_AGENTIC_BASE_URL", "")
        key = settings.get("FORGE_BRIDGE_TOKEN" if name == "bridge" else "FORGE_AGENTIC_API_KEY", "")
        if key and not endpoint and name == "agentic":
            state, detail = (
                "configured",
                (
                    "Provider key is configured. Hosted model and tool access are verified by a rea"
                    "l run, not this configuration check"
                ),
            )
        elif endpoint and key:
            endpoint = validate_endpoint(endpoint)
            if name == "bridge" and not endpoint.endswith("/v1"):
                endpoint += "/v1"
            owned = client is None
            if owned:
                client = httpx.AsyncClient(follow_redirects=False, timeout=15)
            try:
                response = await client.get(
                    endpoint + "/models", headers={"Authorization": f"Bearer {key}"}, timeout=15
                )
                if response.status_code != 200:
                    state, detail = (
                        "failed",
                        f"Connection test rejected (HTTP {response.status_code}); no response body retained",
                    )
                else:
                    payload = response.json()
                    models = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
                    if not isinstance(models, list):
                        raise ValueError("Invalid model list")
                    selected = settings.get(
                        "FORGE_BRIDGE_MODEL" if name == "bridge" else "FORGE_AGENTIC_MODEL", ""
                    ).split(":")[-1]
                    ids = {item.get("id", item.get("name")) if isinstance(item, dict) else item for item in models}
                    if selected and selected not in ids:
                        state, detail = (
                            "failed",
                            "Endpoint authenticated, but the selected model is absent from its model list",
                        )
                    else:
                        state, detail = (
                            "connected",
                            (
                                "Authenticated model listing succeeded; source-editing tool execution still req"
                                "uires a real run"
                            ),
                        )
            except (httpx.HTTPError, ValueError, TypeError):
                state, detail = (
                    "failed",
                    "Connection test could not validate the endpoint; check its address, credentials and model listing",
                )
            finally:
                if owned:
                    await client.aclose()
    _connection_checks[_fingerprint(name, settings)] = {"state": state, "checked_at": now(), "detail": detail}
    return next(item for item in await asyncio.to_thread(provider_catalog, configuration) if item["id"] == name)


def get_provider(name: str, settings: dict[str, str] | None = None) -> CodingProvider:
    providers: dict[str, type[ConfiguredProvider]] = {
        "codex": CodexProvider,
        "claude": ClaudeProvider,
        "bridge": BridgeProvider,
        "agentic": APIProvider,
        "api": APIProvider,
    }
    if name not in providers:
        raise ValueError(f"Unknown provider: {name}")
    return providers[name](settings)
