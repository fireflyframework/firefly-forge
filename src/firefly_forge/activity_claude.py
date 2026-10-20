# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Recognize bounded Claude stream-json observations without inferring execution."""

from __future__ import annotations

import re
from typing import Any

from firefly_forge.activity_models import _preview

Data = dict[str, Any]


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ValueError("Invalid observed tool identity")
    return value


def _text_content(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if (
        isinstance(value, list)
        and len(value) <= 64
        and all(
            isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
            for part in value
        )
    ):
        return "\n".join(part["text"] for part in value)
    return None


def _block(value: Any, role: str, limit: int) -> Data | None:
    if not isinstance(value, dict):
        raise ValueError("Invalid observed content block")
    kind = value.get("type")
    if kind == "text" and role == "assistant":
        if not isinstance(value.get("text"), str):
            raise ValueError("Invalid observed text")
        return {"item_type": "agent_message", "output_preview": _preview(value["text"], limit)}
    if kind == "tool_use" and role == "assistant":
        name = value.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", name):
            raise ValueError("Invalid observed tool name")
        if not isinstance(value.get("input"), dict):
            raise ValueError("Invalid observed tool input")
        result: Data = {"item_type": "tool_use", "item_id": _identifier(value.get("id")), "tool_name": name}
        command = value["input"].get("command")
        if name == "Bash" and isinstance(command, str):
            result["command_preview"] = _preview(command, limit)
        return result
    if kind == "tool_result" and role == "user":
        if "is_error" in value and type(value["is_error"]) is not bool:
            raise ValueError("Invalid observed tool error flag")
        result = {
            "item_type": "tool_result",
            "item_id": _identifier(value.get("tool_use_id")),
            "tool_outcome": "reported_error" if value.get("is_error") is True else "reported_result",
        }
        output = _text_content(value.get("content"))
        if output is not None:
            result["output_preview"] = _preview(output, limit)
        return result
    return None


def claude_summary(event: Data) -> Data | None:
    kind = event.get("type")
    base: Data = {"provider": "claude", "preview_omitted": True}
    if kind == "result":
        error, subtype = event.get("is_error"), event.get("subtype")
        if (
            type(error) is not bool
            or not isinstance(subtype, str)
            or subtype
            not in {
                "success",
                "error_during_execution",
                "error_max_turns",
                "error_max_budget_usd",
                "error_max_structured_output_retries",
            }
            or (subtype == "success") == error
        ):
            return None
        output = event.get("result")
        if not isinstance(output, str):
            errors = event.get("errors")
            if (
                not error
                or not isinstance(errors, list)
                or len(errors) > 64
                or not all(isinstance(e, str) for e in errors)
            ):
                return None
            output = "\n".join(errors)
        return {
            **base,
            "event_type": "claude.result_error" if error else "claude.result",
            "item_type": "agent_message",
            "output_preview": _preview(output, 768),
        }
    if not isinstance(kind, str) or kind not in {"assistant", "user"}:
        return None
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role", kind) != kind:
        return None
    blocks = message.get("content")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 64:
        return None
    try:
        if len(blocks) == 1:
            block = _block(blocks[0], kind, 768)
            return {**base, "event_type": "claude." + kind, **block} if block else None
        projected = []
        for block in blocks[:4]:
            if selected := _block(block, kind, 128):
                projected.append(selected)
        if not projected:
            return None
        return {
            **base,
            "event_type": "claude." + kind,
            "item_type": "provider_message",
            "content_blocks": projected,
            "omitted_blocks": len(blocks) - len(projected),
        }
    except ValueError:
        return None
