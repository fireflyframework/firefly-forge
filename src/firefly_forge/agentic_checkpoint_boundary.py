# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Opt-in model-boundary mechanics; no durable store, dispatch, or restore authority."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import json
import math
import zlib
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from importlib.metadata import version
from pathlib import PurePosixPath
from typing import Any, Literal, cast

from fireflyframework_agentic.model_utils import deserialize_model_messages, serialize_model_messages
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, field_validator
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
MAX_RAW_PAYLOAD = 8 * 1024 * 1024
COMPRESSION_THRESHOLD = 64 * 1024
MAX_COUNTER = 2**53 - 1
USAGE = TypeAdapter(RunUsage)
LIMITS = TypeAdapter(UsageLimits)


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _compatibility() -> dict[str, str]:
    return {name: version(name) for name in ("fireflyframework-agentic", "pydantic-ai-slim")}


def _native_round_trip(adapter: TypeAdapter[Any], value: dict[str, Any]) -> None:
    try:
        restored = adapter.validate_json(_encoded(value))
    except ValidationError:
        raise ValueError("Checkpoint usage or limits contain unsupported fields") from None
    if _encoded(adapter.dump_python(restored, mode="json")) != _encoded(value):
        raise ValueError("Checkpoint usage or limits contain unsupported fields")


def _counter(value: Any, label: str) -> None:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise ValueError("Checkpoint " + label + " must be a bounded nonnegative integer")


def _cost(value: Any, label: str) -> None:
    if value is None:
        return
    if type(value) is not str or not 1 <= len(value) <= 128:
        raise ValueError("Checkpoint " + label + " must retain native decimal encoding")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError("Checkpoint " + label + " is invalid") from None
    if not amount.is_finite() or not 0 <= amount <= MAX_COUNTER:
        raise ValueError("Checkpoint " + label + " must be finite, bounded and nonnegative")


def _budgets(usage: dict[str, Any], limits: dict[str, Any]) -> None:
    if set(usage) != {field.name for field in dataclasses.fields(RunUsage)}:
        raise ValueError("Checkpoint usage has missing or unknown counters")
    for name, value in usage.items():
        if name == "cost":
            _cost(value, "usage cost")
        elif name == "audio_seconds":
            if type(value) not in {int, float} or not 0 <= value <= MAX_COUNTER or not math.isfinite(value):
                raise ValueError("Checkpoint usage duration must be finite, bounded and nonnegative")
        elif name == "details":
            if type(value) is not dict or len(value) > 128:
                raise ValueError("Checkpoint usage details exceed their bound")
            for key, count in value.items():
                if type(key) is not str or not 1 <= len(key) <= 128:
                    raise ValueError("Checkpoint usage detail name exceeds its bound")
                _counter(count, "usage detail")
        else:
            _counter(value, "usage " + name)
    if set(limits) != {field.name for field in dataclasses.fields(UsageLimits)}:
        raise ValueError("Checkpoint limits have missing or unknown fields")
    for name, value in limits.items():
        if name == "cost_limit":
            _cost(value, "limits cost")
        elif name == "count_tokens_before_request":
            if type(value) is not bool:
                raise ValueError("Checkpoint limits token-count selection must be boolean")
        elif value is not None:
            _counter(value, "limits " + name)


def _history(messages: list[ModelMessage]) -> None:
    if not messages or type(messages[-1]) is not ModelRequest:
        raise ValueError("Checkpoint needs a pending model request")
    pending: dict[str, str] = {}
    seen: set[str] = set()
    for message in messages:
        if type(message) not in {ModelRequest, ModelResponse}:
            raise ValueError("Unsupported checkpoint message")
        if message.state != "complete":
            raise ValueError("Checkpoint history has an incomplete message lifecycle")
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


class _CompressedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[2]
    codec: Literal["zlib-base64"]
    raw_bytes: int
    raw_sha256: str
    data: str

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Checkpoint envelope version must be an integer")
        return value


class _SharedHistoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[3]
    codec: Literal["zlib-base64-shared-history"]
    raw_bytes: int
    raw_sha256: str
    packed_bytes: int
    data: str

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Checkpoint envelope version must be an integer")
        return value


def _checkpoint_payload(raw: bytes) -> bytes:
    if not 1 <= len(raw) <= MAX_RAW_PAYLOAD:
        raise ValueError("Checkpoint raw record exceeds 8 MiB")
    if len(raw) <= COMPRESSION_THRESHOLD:
        return raw
    record = _record(raw)
    value = record.model_dump(mode="json")
    if record.request_history != record.history or _encoded(value) != raw:
        raise ValueError("Checkpoint shared history requires the exact canonical record")
    value["request_history"] = "history"
    packed = _encoded(value)
    return _encoded(
        {
            "version": 3,
            "codec": "zlib-base64-shared-history",
            "raw_bytes": len(raw),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "packed_bytes": len(packed),
            "data": base64.b64encode(zlib.compress(packed, level=9)).decode("ascii"),
        }
    )


def _checkpoint_record(payload: bytes) -> bytes:
    if not 1 <= len(payload) <= MAX_PAYLOAD:
        raise ValueError("Checkpoint size differs")
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError("Checkpoint JSON encoding differs") from None
    if type(value) is not dict or value.get("version") not in (2, 3):
        return payload
    envelope: _CompressedRecord | _SharedHistoryRecord
    try:
        envelope = (
            _SharedHistoryRecord.model_validate_json(payload)
            if value["version"] == 3
            else _CompressedRecord.model_validate_json(payload)
        )
    except ValidationError:
        raise ValueError("Checkpoint compression envelope schema differs") from None
    if not 1 <= envelope.raw_bytes <= MAX_RAW_PAYLOAD:
        raise ValueError("Checkpoint raw record exceeds 8 MiB")
    packed_bytes = envelope.packed_bytes if isinstance(envelope, _SharedHistoryRecord) else envelope.raw_bytes
    if not 1 <= packed_bytes <= MAX_RAW_PAYLOAD:
        raise ValueError("Checkpoint packed record exceeds 8 MiB")
    try:
        compressed = base64.b64decode(envelope.data, validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, packed_bytes + 1)
    except (binascii.Error, ValueError, zlib.error):
        raise ValueError("Checkpoint compression encoding differs") from None
    if (
        len(raw) != packed_bytes
        or not inflater.eof
        or inflater.unused_data
        or inflater.unconsumed_tail
        or base64.b64encode(compressed).decode("ascii") != envelope.data
        or _encoded(envelope.model_dump(mode="json")) != payload
    ):
        raise ValueError("Checkpoint compression size, digest or canonical encoding differs")
    if isinstance(envelope, _SharedHistoryRecord):
        try:
            value = json.loads(raw)
            if (
                type(value) is not dict
                or set(value) != set(_Record.model_fields)
                or type(value.get("history")) is not list
                or value.get("request_history") != "history"
                or _encoded(value) != raw
            ):
                raise ValueError("Checkpoint shared history representation differs")
            # Check expanded size before allocating the full mirrored record.
            history_bytes = len(_encoded(value["history"]))
            if len(raw) - len(b'"history"') + history_bytes != envelope.raw_bytes:
                raise ValueError("Checkpoint shared history expanded size differs")
            value["request_history"] = value["history"]
            raw = _encoded(value)
        except (UnicodeDecodeError, RecursionError):
            raise ValueError("Checkpoint shared history encoding differs") from None
        except ValueError:
            raise ValueError("Checkpoint shared history representation or size differs") from None
    if len(raw) != envelope.raw_bytes or hashlib.sha256(raw).hexdigest() != envelope.raw_sha256:
        raise ValueError("Checkpoint raw size or digest differs")
    return raw


def _record(raw: bytes) -> _Record:
    try:
        return _Record.model_validate_json(raw)
    except ValidationError:
        raise ValueError("Checkpoint record schema differs") from None


@dataclasses.dataclass(frozen=True)
class BoundaryCheckpoint:
    payload: bytes
    digest: str

    def __post_init__(self) -> None:
        if type(self.payload) is not bytes:
            raise ValueError("Checkpoint payload must be immutable bytes")

    @classmethod
    def decode(cls, payload: bytes, digest: str) -> BoundaryCheckpoint:
        if type(payload) is not bytes:
            raise ValueError("Checkpoint payload must be immutable bytes")
        if not 1 <= len(payload) <= MAX_PAYLOAD:
            raise ValueError(f"Checkpoint encoded size differs (bytes={len(payload)}, limit={MAX_PAYLOAD})")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("Checkpoint encoded digest differs")
        raw = _checkpoint_record(payload)
        record = _record(raw)
        if record.compatibility != _compatibility() or _encoded(record.model_dump(mode="json")) != raw:
            raise ValueError("Checkpoint runtime or canonical encoding differs")
        try:
            messages = deserialize_model_messages(record.history)
        except ValidationError:
            raise ValueError("Checkpoint native history schema differs") from None
        _history(messages)
        if record.request_history != record.history:
            raise ValueError("Request-only history cannot restore the agent graph")
        _native_round_trip(USAGE, record.usage)
        _native_round_trip(LIMITS, record.limits)
        _budgets(record.usage, record.limits)
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
        return _record(_checkpoint_record(self.payload))

    @property
    def canonical_payload(self) -> bytes:
        BoundaryCheckpoint.decode(self.payload, self.digest)
        return _checkpoint_record(self.payload)

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
        if type(wire) is not bytes:
            raise ValueError("Checkpoint wire must be immutable bytes")
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
        for field in ("_telemetry_mcp_url_ids", "_telemetry_mcp_url_toolset_ids", "_telemetry_mcp_url_tool_names"):
            if field not in contract:
                continue
            value = contract[field]
            if value is None and field != "_telemetry_mcp_url_toolset_ids":
                continue
            if type(value) is not set or any(type(item) is not str for item in value):
                raise ValueError("Checkpoint telemetry provenance must retain native string sets")
            contract[field] = sorted(value)
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
        retained = self.restore is not None and not invocation.restored
        if retained:
            assert self.restore is not None
            expected = self.restore.record.model_dump(mode="json")
            actual = record.model_dump(mode="json")
            changed = [field for field in expected if _encoded(actual[field]) != _encoded(expected[field])]
            if changed:
                raise ValueError("Restored model state differs from the checkpoint: " + ", ".join(changed))
        payload = (
            self.restore.payload
            if retained and self.restore is not None
            else _checkpoint_payload(_encoded(record.model_dump(mode="json")))
        )
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
        if type(response) is not bytes:
            raise ValueError("Original completion must be immutable bytes")
        if not 1 <= len(response) <= 262144:
            raise ValueError("Original completion exceeds its response bound")
        self._response = response
        self.consume = consume
        self.claimed = False

    @property
    def response(self) -> bytes:
        return self._response

    async def expose(self, wire: bytes) -> bytes:
        if type(wire) is not bytes:
            raise ValueError("Restored request wire must be immutable bytes")
        if self.claimed:
            raise ValueError("Original completion consumption was already attempted")
        if (
            len(wire) != self.checkpoint.record.wire_bytes
            or hashlib.sha256(wire).hexdigest() != self.checkpoint.wire_sha256
        ):
            raise ValueError("Restored request wire differs from the original")
        self.claimed = True
        response = self.response
        await self.consume(self.checkpoint.digest, hashlib.sha256(response).hexdigest())
        return response
