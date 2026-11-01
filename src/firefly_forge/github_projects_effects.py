# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read-before-write decisions detect drift but cannot supply remote compare-and-swap."""

from typing import Any, Literal


def managed_value(item: dict[str, Any], field_id: str) -> str | None:
    values = [
        value.get("optionId")
        for value in item["fieldValues"]["nodes"]
        if (value.get("field") or {}).get("id") == field_id
    ]
    if len(values) > 1 or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("Managed-field observation is ambiguous")
    return values[0] if values else None


def decide_update(
    current: str | None, last_confirmed: str | None, desired: str
) -> Literal["conflict", "update", "unchanged"]:
    if last_confirmed is not None and current != last_confirmed:
        return "conflict"
    if last_confirmed is None and current is not None and current != desired:
        return "conflict"
    return "unchanged" if current == desired else "update"
