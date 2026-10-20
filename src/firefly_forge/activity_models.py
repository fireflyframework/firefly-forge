# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded projections and exact sanitized provider-message content contracts."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Data = dict[str, Any]
MAX_EVENT_BYTES = 4_194_304
MAX_RUN_BYTES = 67_108_864
MAX_EVENTS = 4096
MAX_INDEX_BYTES = 4096
CHUNK_BYTES = 16384


class ActivityUnavailable(ValueError):
    """No activity content may accompany an unavailable observation."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class ActivityDescriptor(_Strict):
    version: Literal[1]
    status: Literal["available", "budget_exceeded"]
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    event_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    stage: Literal["stdout", "stderr"]
    encoding: Literal["utf-8"]
    sanitized_message_bytes: int = Field(ge=0)
    sanitized_message_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    descriptor_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported activity content version")
        return value


class ActivityContentBlock(_Strict):
    item_type: Literal["agent_message", "tool_use", "tool_result"]
    item_id: str | None = Field(default=None, max_length=128)
    tool_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    tool_outcome: Literal["reported_result", "reported_error"] | None = None
    command_preview: str | None = None
    output_preview: str | None = None

    @field_validator("command_preview", "output_preview")
    @classmethod
    def preview_bytes(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode()) > 128:
            raise ValueError("Activity block preview exceeds its byte budget")
        return value


class ActivitySummary(_Strict):
    version: Literal[1] = 1
    event_type: str = Field(max_length=64)
    item_type: str | None = Field(default=None, max_length=64)
    item_id: str | None = Field(default=None, max_length=128)
    command_preview: str | None = None
    output_preview: str | None = None
    exit_code: int | None = Field(default=None, ge=-2147483648, le=2147483647)
    provider: Literal["claude", "codex"] | None = None
    tool_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    tool_server: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    tool_status: Literal["in_progress", "completed", "failed"] | None = None
    tool_exit_code: int | None = Field(default=None, ge=-2147483648, le=2147483647)
    tool_outcome: Literal["reported_result", "reported_error"] | None = None
    content_blocks: list[ActivityContentBlock] | None = Field(default=None, min_length=1, max_length=4)
    omitted_blocks: int | None = Field(default=None, ge=0, le=64)
    preview_omitted: bool
    sanitized_message_bytes: int = Field(ge=0, le=MAX_EVENT_BYTES)
    sanitized_message_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("command_preview", "output_preview")
    @classmethod
    def preview_bytes(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode()) > 768:
            raise ValueError("Activity preview exceeds its byte budget")
        return value


class ActivityContentQuery(_Strict):
    expected_descriptor: str = Field(pattern=r"^[a-f0-9]{64}$")
    chunk: int = Field(default=0, ge=0, le=255)


def _preview(value: str, limit: int) -> str:
    result = value.encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")
    while len(canonical(result)) > limit + 2:
        result = result[: len(result) // 2]
    return result


def _unique_object(items: list[tuple[str, Any]]) -> Data:
    result: Data = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate provider event field")
        result[key] = value
    return result


def _summary(message: str, raw: bytes) -> Data | None:
    if len(raw) > MAX_EVENT_BYTES:
        return None
    try:
        event = json.loads(message, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        return None
    if not isinstance(event, dict):
        return None
    if isinstance(event.get("item"), dict) and event["item"].get("type") == "mcp_tool_call":
        from firefly_forge.activity_codex_mcp import codex_mcp_summary

        mcp_values = codex_mcp_summary(event)
        if mcp_values is None:
            return None
        mcp_values.update(sanitized_message_bytes=len(raw), sanitized_message_sha256=hashlib.sha256(raw).hexdigest())
        return ActivitySummary.model_validate(mcp_values).model_dump(exclude_none=True)
    if event.get("type") in ("assistant", "user", "result"):
        from firefly_forge.activity_claude import claude_summary

        claude_values = claude_summary(event)
        if claude_values is None:
            return None
        claude_values.update(sanitized_message_bytes=len(raw), sanitized_message_sha256=hashlib.sha256(raw).hexdigest())
        return ActivitySummary.model_validate(claude_values).model_dump(exclude_none=True)
    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type not in {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "error",
        "item.started",
        "item.updated",
        "item.completed",
    }:
        return None
    item = event.get("item")
    item = item if isinstance(item, dict) else {}
    item_type = item.get("type")
    if item_type is not None and (
        not isinstance(item_type, str)
        or item_type not in {"command_execution", "agent_message", "reasoning", "file_change"}
    ):
        return None
    values: Data = {
        "event_type": event_type,
        "item_type": item_type,
        "preview_omitted": _preview(message, 1024) != message,
        "sanitized_message_bytes": len(raw),
        "sanitized_message_sha256": hashlib.sha256(raw).hexdigest(),
    }
    if isinstance(item.get("id"), str):
        values["item_id"] = _preview(item["id"], 128)
    if item_type == "command_execution":
        if isinstance(item.get("command"), str):
            values["command_preview"] = _preview(item["command"], 768)
            values["preview_omitted"] |= values["command_preview"] != item["command"]
        if type(item.get("exit_code")) is int and -2147483648 <= item["exit_code"] <= 2147483647:
            values["exit_code"] = item["exit_code"]
        output = item.get("aggregated_output")
    else:
        output = item.get("text")
    if isinstance(output, str):
        values["output_preview"] = _preview(output, 768)
        values["preview_omitted"] |= values["output_preview"] != output
    summary = ActivitySummary.model_validate(values).model_dump(exclude_none=True)
    summary["item_type"] = item_type
    return summary


def project_activity(
    run_id: str, event_id: str, stage: str, message: str, *, retain: bool = True
) -> tuple[Data, bytes | None]:
    raw = message.encode("utf-8", errors="replace")
    descriptor: Data = {
        "version": 1,
        "status": "available" if retain and len(raw) <= MAX_EVENT_BYTES else "budget_exceeded",
        "run_id": run_id,
        "event_id": event_id,
        "stage": stage,
        "encoding": "utf-8",
        "sanitized_message_bytes": len(raw),
        "sanitized_message_sha256": hashlib.sha256(raw).hexdigest(),
    }
    descriptor["descriptor_sha256"] = hashlib.sha256(canonical(descriptor)).hexdigest()
    descriptor = ActivityDescriptor.model_validate(descriptor).model_dump()
    result: Data = {"message": _preview(message, 1024), "activity_content": descriptor}
    if summary := _summary(raw.decode(), raw):
        result["activity_summary"] = summary
    if len(canonical(result)) > MAX_INDEX_BYTES - 256:
        raise ActivityUnavailable("activity_index_limit")
    return result, raw if descriptor["status"] == "available" else None


def validate_descriptor(value: Any) -> Data:
    try:
        descriptor = ActivityDescriptor.model_validate(value).model_dump()
    except ValueError as exc:
        raise ActivityUnavailable("activity_integrity") from exc
    selected = {key: value for key, value in descriptor.items() if key != "descriptor_sha256"}
    if hashlib.sha256(canonical(selected)).hexdigest() != descriptor["descriptor_sha256"]:
        raise ActivityUnavailable("activity_integrity")
    return descriptor


def content_chunk(descriptor: Data, payload: bytes, query: ActivityContentQuery) -> Data:
    descriptor = validate_descriptor(descriptor)
    if descriptor["descriptor_sha256"] != query.expected_descriptor:
        raise ActivityUnavailable("activity_cut_changed")
    if descriptor["status"] != "available":
        raise ActivityUnavailable("activity_budget_exceeded")
    if (
        len(payload) > MAX_EVENT_BYTES
        or len(payload) != descriptor["sanitized_message_bytes"]
        or hashlib.sha256(payload).hexdigest() != descriptor["sanitized_message_sha256"]
    ):
        raise ActivityUnavailable("activity_integrity")
    offset = query.chunk * CHUNK_BYTES
    if offset >= len(payload) and (payload or query.chunk):
        raise ActivityUnavailable("activity_chunk_out_of_range")
    selected = payload[offset : offset + CHUNK_BYTES]
    return {
        "version": 1,
        "descriptor_sha256": descriptor["descriptor_sha256"],
        "chunk": query.chunk,
        "offset": offset,
        "total_bytes": len(payload),
        "sanitized_message_sha256": descriptor["sanitized_message_sha256"],
        "data_base64": base64.b64encode(selected).decode("ascii"),
        "next_chunk": query.chunk + 1 if offset + len(selected) < len(payload) else None,
    }
