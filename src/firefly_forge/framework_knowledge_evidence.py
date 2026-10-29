# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Complete version-scoped citations inside a configurable bounded prompt window."""

from __future__ import annotations

import json
import os
from typing import Any

from firefly_forge.prompts import prompt_text


def evidence_context(
    official: list[dict[str, Any]],
    project: list[dict[str, Any]],
    *,
    budget: int | None = None,
    excerpt: int | None = None,
) -> str:
    budget = budget if budget is not None else int(os.environ.get("FORGE_FRAMEWORK_CONTEXT_BYTES", "40000"))
    excerpt = excerpt if excerpt is not None else int(os.environ.get("FORGE_FRAMEWORK_EXCERPT_BYTES", "2000"))
    if type(budget) is not int or type(excerpt) is not int or budget <= 0 or excerpt <= 0:
        raise ValueError("Configure positive finite official evidence response windows")
    scopes = {(hit.get("corpus_ref"), hit.get("framework_version"), hit.get("commit")) for hit in official}
    if len(scopes) != 1 or any(value is None for value in next(iter(scopes), ())):
        raise ValueError("Official evidence must retain one exact framework corpus scope")
    if not {"document", "example", "api"} <= {hit.get("kind") for hit in official}:
        raise ValueError("Exact framework evidence requires documentation, example, and API citations")

    def bounded(hit: dict[str, Any]) -> dict[str, Any]:
        content = hit.get("content")
        if not isinstance(content, str):
            raise ValueError("Official evidence requires text excerpts")
        return {**hit, "content": content.encode()[:excerpt].decode("utf8", errors="ignore")}

    required = [bounded(hit) for hit in official]
    optional = [bounded(hit) for hit in project]
    while True:
        context = prompt_text("tools/knowledge-evidence", hits=json.dumps(required + optional, ensure_ascii=False))
        if len(context.encode()) <= budget:
            return context
        if not optional:
            raise ValueError(
                "Increase the official evidence context window budget to retain every required citation family"
            )
        optional.pop()
