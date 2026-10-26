# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Complete JSON admission documents with bounded text windows and no recursive walk."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from typing import Any

TEXT_CHARS = 8192


def text_chunks(value: str) -> Iterator[str]:
    for start in range(0, len(value), TEXT_CHARS):
        chunk = value[start : start + TEXT_CHARS]
        try:
            chunk.encode("utf-8")
        except UnicodeError:
            raise ValueError("Credential admission requires valid Unicode text") from None
        yield chunk


def _scalar(value: Any) -> str:
    if value is None or type(value) is bool:
        return json.dumps(value)
    if type(value) is int:
        if value.bit_length() > 4096:
            raise ValueError("Credential admission exceeds its numeric bound")
        return str(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Credential admission requires finite JSON scalars")
        return json.dumps(value, allow_nan=False)
    raise ValueError("Credential admission requires JSON data")


def raw_documents(value: Any, *, strict: bool = True) -> Iterator[str | None]:
    """None ends one scalar document; sibling values must not be concatenated."""
    ancestors: set[int] = set()
    stack = [iter((value,))]
    containers: list[int] = []
    steps = 0
    while stack:
        steps += 1
        if steps % 256 == 0:
            yield ""
        try:
            item = next(stack[-1])
        except StopIteration:
            stack.pop()
            if containers:
                ancestors.remove(containers.pop())
            continue
        if isinstance(item, (dict, list, tuple)):
            if id(item) in ancestors:
                raise ValueError("Credential admission exceeds its structural bound: cyclic data")
            ancestors.add(id(item))
            containers.append(id(item))
            children = (part for pair in item.items() for part in pair) if isinstance(item, dict) else iter(item)
            stack.append(iter(children))
        elif isinstance(item, str):
            yield from text_chunks(item)
            yield None
        elif strict:
            yield _scalar(item)
            yield None


def canonical_chunks(value: Any) -> Iterator[str]:
    """Match json.dumps(..., ensure_ascii=False, separators=(',', ':')) without joining."""
    ancestors: set[int] = set()
    stack: list[tuple[str, Any]] = [("value", value)]
    while stack:
        action, item = stack.pop()
        if action == "text":
            yield item
        elif action == "string":
            yield '"'
            for chunk in text_chunks(item):
                yield json.dumps(chunk, ensure_ascii=False)[1:-1]
            yield '"'
        elif action == "end":
            ancestors.remove(id(item))
            yield "}" if isinstance(item, dict) else "]"
        elif action == "items":
            iterator, first, mapping = item
            try:
                child = next(iterator)
            except StopIteration:
                continue
            stack.append(("items", (iterator, False, mapping)))
            if mapping:
                key, element = child
                stack.extend(
                    [("value", element), ("text", ":"), ("string", key if isinstance(key, str) else _scalar(key))]
                )
            else:
                stack.append(("value", child))
            if not first:
                stack.append(("text", ","))
        elif isinstance(item, str):
            stack.append(("string", item))
        elif isinstance(item, (dict, list, tuple)):
            if id(item) in ancestors:
                raise ValueError("Credential admission exceeds its structural bound: cyclic data")
            ancestors.add(id(item))
            yield "{" if isinstance(item, dict) else "["
            stack.append(("end", item))
            stack.append(
                ("items", (iter(item.items() if isinstance(item, dict) else item), True, isinstance(item, dict)))
            )
        else:
            yield _scalar(item)


def admission_documents(value: Any) -> Iterator[str | None]:
    yield from raw_documents(value)
    if isinstance(value, (dict, list, tuple)):
        yield from canonical_chunks(value)
        yield None
