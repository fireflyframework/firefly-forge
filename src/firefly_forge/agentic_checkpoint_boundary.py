# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Opt-in model-boundary mechanics; no durable store, dispatch, or restore authority."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib.metadata import version
from pathlib import PurePosixPath
from typing import Any, Literal, cast

from fireflyframework_agentic.model_utils import deserialize_model_messages, serialize_model_messages
from pydantic import BaseModel, ConfigDict, TypeAdapter, field_validator
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage, UsageLimits

MAX_PAYLOAD = 192 * 1024
USAGE = TypeAdapter(RunUsage)
LIMITS = TypeAdapter(UsageLimits)


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _compatibility() -> dict[str, str]:
    return {name: version(name) for name in ("fireflyframework-agentic", "pydantic-ai-slim")}


def _native_round_trip(adapter: TypeAdapter[Any], value: dict[str, Any]) -> None:
    restored = adapter.validate_json(_encoded(value))
    if _encoded(adapter.dump_python(restored, mode="json")) != _encoded(value):
        raise ValueError("Checkpoint usage or limits contain unsupported fields")


def _history(messages: list[ModelMessage]) -> None:
    if not messages or type(messages[-1]) is not ModelRequest:
        raise ValueError("Checkpoint needs a pending model request")
    pending: dict[str, str] = {}
    seen: set[str] = set()
    for message in messages:
        if type(message) not in {ModelRequest, ModelResponse}:
            raise ValueError("Unsupported checkpoint message")
        if isinstance(message, ModelResponse) and message.finish_reason not in {None, "stop", "tool_call"}:
            raise ValueError("Unsupported checkpoint response state")
        for part in message.parts:
            if type(part) in {SystemPromptPart, TextPart}:
                if not isinstance(cast(SystemPromptPart | TextPart, part).content, str):
                    raise ValueError("Checkpoint requires text history")
            elif type(part) is UserPromptPart:
                if not isinstance(part.content, str):
                    raise ValueError("Checkpoint does not support vision or binary history")
            elif type(part) is ToolCallPart:
                if not part.tool_call_id or part.tool_call_id in seen:
                    raise ValueError("Checkpoint tool identities must be unique")
                pending[part.tool_call_id] = part.tool_name
                seen.add(part.tool_call_id)
            elif type(part) is ToolReturnPart:
                if pending.pop(part.tool_call_id, None) != part.tool_name:
                    raise ValueError("Checkpoint tool result does not match its call")
                _encoded(part.content)
            else:
                raise ValueError("Unsupported checkpoint history part")
    if pending:
        raise ValueError("Checkpoint has unfinished tools")


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    files_written: list[str]

    @field_validator("files_written")
    @classmethod
    def paths(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(
            not value or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts for value in values
        ):
            raise ValueError("Checkpoint file paths must be unique and relative")
        return values


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    compatibility: dict[str, str]
    history: list[Any]
    request_history: list[Any]
    usage: dict[str, Any]
    limits: dict[str, Any]
    state: _State
    contract: dict[str, Any]
    wire_sha256: str
    wire_bytes: int

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Checkpoint version must be an integer")
        return value


@dataclasses.dataclass(frozen=True)
class BoundaryCheckpoint:
    payload: bytes
    digest: str

    @classmethod
    def decode(cls, payload: bytes, digest: str) -> BoundaryCheckpoint:
        if not 1 <= len(payload) <= MAX_PAYLOAD or hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("Checkpoint size or digest differs")
        record = _Record.model_validate_json(payload)
        if record.compatibility != _compatibility() or _encoded(record.model_dump(mode="json")) != payload:
            raise ValueError("Checkpoint runtime or canonical encoding differs")
        messages = deserialize_model_messages(record.history)
        _history(messages)
        if record.request_history != record.history:
            raise ValueError("Request-only history cannot restore the agent graph")
        _native_round_trip(USAGE, record.usage)
        _native_round_trip(LIMITS, record.limits)
        if (
            len(record.wire_sha256) != 64
            or any(char not in "0123456789abcdef" for char in record.wire_sha256)
            or type(record.wire_bytes) is not int
            or not 1 <= record.wire_bytes <= 9_000_000
        ):
            raise ValueError("Checkpoint wire binding is invalid")
        return cls(payload, digest)

    @property
    def record(self) -> _Record:
        return _Record.model_validate_json(self.payload)

    @property
    def state(self) -> dict[str, Any]:
        return self.record.state.model_dump()

    @property
    def history(self) -> list[ModelMessage]:
        return deserialize_model_messages(self.record.history)

    @property
    def usage(self) -> RunUsage:
        return USAGE.validate_json(_encoded(self.record.usage))

    @property
    def wire_sha256(self) -> str:
        return self.record.wire_sha256

    def resume_kwargs(self) -> dict[str, Any]:
        BoundaryCheckpoint.decode(self.payload, self.digest)
        return {
            "prompt": None,
            "message_history": self.history,
            "usage": self.usage,
            "usage_limits": LIMITS.validate_json(_encoded(self.record.limits)),
        }


@dataclasses.dataclass
class _Invocation:
    restored: bool = False
    contract: bytes | None = None


class AgenticBoundary(AbstractCapability[Any]):
    """Capture only while a public native model hook is inside an explicit invocation."""

    def __init__(self, state: Callable[[], dict[str, Any]], *, restore: BoundaryCheckpoint | None = None) -> None:
        self.state = state
        self.restore = restore
        self._invocation: ContextVar[_Invocation | None] = ContextVar("agentic_boundary_invocation", default=None)
        self._pending: ContextVar[tuple[RunContext[Any], ModelRequestContext] | None] = ContextVar(
            "agentic_boundary_pending", default=None
        )

    @contextmanager
    def invocation(self) -> Iterator[None]:
        active = self._invocation.set(_Invocation())
        pending = self._pending.set(None)
        try:
            yield
        finally:
            self._pending.reset(pending)
            self._invocation.reset(active)

    def current(self) -> tuple[RunContext[Any], ModelRequestContext] | None:
        return self._pending.get()

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        invocation = self._invocation.get()
        if invocation is None:
            raise ValueError("Checkpoint capability needs an explicit invocation")
        if ctx.retry or any(ctx.retries.values()):
            raise ValueError("Checkpoint cannot restore retry state")
        parameters = request_context.model_request_parameters
        if (
            request_context.streaming
            or parameters.native_tools
            or parameters.allow_image_output
            or parameters.deferred_capability_ids
            or parameters.revealed_tool_names
            or any(part.dynamic for part in parameters.instruction_parts or [])
            or any(
                tool.defer_loading or tool.kind not in {"function", "output"}
                for tool in [*parameters.function_tools, *parameters.output_tools]
            )
        ):
            raise ValueError("Checkpoint does not support streaming, deferred or dynamic state")
        _history(ctx.messages)
        _history(request_context.messages)
        if self.restore is not None and not invocation.restored:
            # Native prompt=None reconstructs the pending request with a new timestamp.
            # Restore its recorded metadata through the public history objects as well.
            original = self.restore.history[-1]
            assert isinstance(original, ModelRequest)
            for messages in (ctx.messages, request_context.messages):
                pending = messages[-1]
                assert isinstance(pending, ModelRequest)
                pending.timestamp = original.timestamp
        self._pending.set((ctx, request_context))
        return request_context

    def capture(self, wire: bytes) -> BoundaryCheckpoint:
        current = self.current()
        invocation = self._invocation.get()
        if invocation is None or current is None:
            raise ValueError("No active model request to checkpoint")
        ctx, request = current
        limits = ctx.usage_limits
        if limits is None:
            raise ValueError("Checkpoint requires an explicit native usage budget")
        _history(ctx.messages)
        contract = dataclasses.asdict(request.model_request_parameters)
        for field in ("deferred_capability_ids", "revealed_tool_names"):
            contract.pop(field)
        record = _Record(
            version=1,
            compatibility=_compatibility(),
            history=serialize_model_messages(ctx.messages),
            request_history=serialize_model_messages(request.messages),
            usage=USAGE.dump_python(ctx.usage, mode="json"),
            limits=LIMITS.dump_python(limits, mode="json"),
            state=_State.model_validate(self.state()),
            contract={"parameters": contract, "settings": request.model_settings},
            wire_sha256=hashlib.sha256(wire).hexdigest(),
            wire_bytes=len(wire),
        )
        if self.restore is not None and not invocation.restored:
            expected = self.restore.record.model_dump(mode="json")
            actual = record.model_dump(mode="json")
            changed = [field for field in expected if _encoded(actual[field]) != _encoded(expected[field])]
            if changed:
                raise ValueError("Restored model state differs from the checkpoint: " + ", ".join(changed))
        payload = _encoded(record.model_dump(mode="json"))
        checkpoint = BoundaryCheckpoint.decode(payload, hashlib.sha256(payload).hexdigest())
        contract_bytes = _encoded(record.contract)
        if invocation.contract is not None and invocation.contract != contract_bytes:
            raise ValueError("Checkpoint cannot restore a dynamic model/tool contract")
        invocation.contract = contract_bytes
        invocation.restored = True
        return checkpoint


class OriginalCompletion:
    """One-use in-memory gate; its callback must supply durable fencing in a later stage."""

    def __init__(
        self,
        checkpoint: BoundaryCheckpoint,
        response: bytes,
        consume: Callable[[str, str], Awaitable[None]],
    ) -> None:
        self.checkpoint = BoundaryCheckpoint.decode(checkpoint.payload, checkpoint.digest)
        if not 1 <= len(response) <= 262144:
            raise ValueError("Original completion exceeds its response bound")
        self.response = response
        self.consume = consume
        self.claimed = False

    async def expose(self, wire: bytes) -> bytes:
        if self.claimed:
            raise ValueError("Original completion consumption was already attempted")
        if (
            len(wire) != self.checkpoint.record.wire_bytes
            or hashlib.sha256(wire).hexdigest() != self.checkpoint.wire_sha256
        ):
            raise ValueError("Restored request wire differs from the original")
        self.claimed = True
        await self.consume(self.checkpoint.digest, hashlib.sha256(self.response).hexdigest())
        return self.response
