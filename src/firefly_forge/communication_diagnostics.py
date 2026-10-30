# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Public diagnostic excerpts preserve recorded bytes after explicit privacy redaction."""

from __future__ import annotations

import re
import shlex
from typing import Any

from firefly_forge.secrets import redact_environment


def _public_excerpt(value: str, *, tail: bool) -> tuple[str, bool] | None:
    """Filter before bounding text; unstructured API paths are not proven public."""
    if re.search(
        r"(?i)(?:[\w.-]*(?:password|secret|token|api[_-]?key|credential|authorization)[\w.-]*[\"']?\s*[:=])"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----",
        value,
    ):
        return None
    value = redact_environment(value)
    value = re.sub(r"https?://\S+|www\.\S+", "[destination omitted]", value)
    value = re.sub(r"(?<!\w)(?:[A-Za-z]:\\|/)[^\s<>]+", "[local path omitted]", value)
    value = re.sub(r"\b[A-Z][A-Z0-9_]*=\S+", "[setting omitted]", value)
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]", "", value)
    if not value.strip():
        return None
    if len(value) <= 1800:
        return value, False
    if not tail:
        return value[:1800], True
    end = len(value.rstrip())
    start = max(0, end - 1800)
    if start and value[start - 1] != "\n":
        boundary = value.find("\n", start, end)
        if boundary != -1:
            start = boundary + 1
    return value[start:end], True


def check_diagnostics(run: dict[str, Any]) -> dict[str, Any]:
    from firefly_forge.communication import _reused_verification

    checks = run.get("final_verification")
    if not isinstance(checks, list):
        return {}
    result = {}
    for index, check in enumerate(checks, 1):
        if not isinstance(check, dict) or type(check.get("exit_code")) is not int:
            continue
        # A replayed or provenance-unknown result must not become a fresh check.
        attempt = check.get("attempt")
        current = run.get("attempt")
        reused = _reused_verification(check)
        provenance = (
            "unknown"
            if (
                type(attempt) is not int
                or attempt < 1
                or type(current) is not int
                or current < 1
                or attempt > current
                or reused is None
            )
            else "current"
            if attempt == current and reused is False
            else "retained"
        )
        argv = check.get("argv")
        values = {"output": check.get("output")} if check["exit_code"] != 0 else {}
        if isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv):
            values["command"] = shlex.join(argv)
        for kind, value in values.items():
            if not isinstance(value, str) or not value.strip():
                continue
            excerpt = _public_excerpt(value, tail=kind == "output")
            if excerpt is None:
                continue
            text, truncated = excerpt
            result[f"check-{index}-{kind}"] = {
                "source": "recorded_final_verification",
                "kind": kind,
                "check_index": index,
                "exit_code": check["exit_code"],
                "provenance": provenance,
                "text": text,
                "truncated": truncated,
            }
    return result


def recorded_error(run: dict[str, Any]) -> dict[str, Any] | None:
    if run.get("status") not in ("failed", "needs_reconciliation"):
        return None
    error = run.get("error")
    if not isinstance(error, str) or not (excerpt := _public_excerpt(error, tail=True)):
        return None
    text, truncated = excerpt
    return {"source": "recorded_run_error", "cause": "not_established", "text": text, "truncated": truncated}


def render_excerpt(value: dict[str, Any]) -> str:
    text = value["text"]
    longest = max((len(item) for item in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    notice = "[excerpt truncated]\n\n" if value.get("truncated") is True else ""
    return f"{notice}{fence}text\n{text}\n{fence}"
