# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Display observed Codex MCP metadata without asserting native execution authority."""

from __future__ import annotations

import re
from typing import Any

from firefly_forge.activity_models import _preview

Data = dict[str, Any]


def codex_mcp_summary(event: Data) -> Data | None:
    kind, item = event.get("type"), event.get("item")
    if kind not in ("item.started", "item.updated", "item.completed") or not isinstance(item, dict):
        return None
    if item.get("type") != "mcp_tool_call":
        return None
    for key, limit in (("id", 128), ("server", 64), ("tool", 64)):
        value = item.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1," + str(limit) + "}", value):
            return None
    status = item.get("status")
    if status not in ("in_progress", "completed", "failed"):
        return None
    if (kind == "item.started" and status != "in_progress") or (kind == "item.completed" and status == "in_progress"):
        return None
    if not isinstance(item.get("arguments"), dict):
        return None
    if any(item.get(key) is not None and not isinstance(item[key], dict) for key in ("result", "error")):
        return None
    summary: Data = dict(
        provider="codex",
        event_type=kind,
        item_type="mcp_tool_call",
        item_id=item["id"],
        tool_server=item["server"],
        tool_name=item["tool"],
        tool_status=status,
        preview_omitted=True,
    )
    if kind != "item.completed":
        return summary
    result, error = item.get("result") or {}, item.get("error") or {}
    structured = result.get("structured_content")
    structured = structured if isinstance(structured, dict) else {}
    output = error.get("message") if status == "failed" else structured.get("output")
    if status == "completed":
        exit_code = structured.get("exit_code")
        if type(exit_code) is int and -2147483648 <= exit_code <= 2147483647:
            summary["tool_exit_code"] = exit_code
    if not isinstance(output, str):
        content = result.get("content")
        if (
            isinstance(content, list)
            and 1 <= len(content) <= 4
            and all(
                isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                for part in content
            )
        ):
            text = "\n".join(part["text"] for part in content)
            if not text.lstrip().startswith(("{", "[")):
                output = text
    if isinstance(output, str):
        summary["output_preview"] = _preview(output, 768)
    return summary
