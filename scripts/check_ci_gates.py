# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Keep the required aggregate check red when a parallel prerequisite fails."""

from __future__ import annotations

import json
import os

REQUIRED = frozenset({"python", "studio", "documentation", "container"})


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous prerequisite result")
        result[key] = value
    return result


def require_success(value: str) -> None:
    if len(value) > 16_384:
        raise ValueError("Invalid prerequisite results")
    try:
        results = json.loads(value, object_pairs_hook=_unique)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid prerequisite results") from exc
    if not isinstance(results, dict) or set(results) != REQUIRED:
        raise ValueError("Missing or unexpected prerequisite result")
    if any(not isinstance(item, dict) or item.get("result") != "success" for item in results.values()):
        raise ValueError("Every prerequisite must finish successfully before packaging")


if __name__ == "__main__":
    try:
        require_success(os.environ.get("FORGE_CI_NEEDS", ""))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
