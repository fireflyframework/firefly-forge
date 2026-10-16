# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Invocation-local native callbacks; this module performs no persistence or inference."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

Data = dict[str, Any]


@dataclass(frozen=True)
class BridgeCompletionScope:
    prepare: Callable[[bytes], Awaitable[None]]
    admit: Callable[[Data, bytes], Awaitable[Data]]
    guard: Callable[[Data, bytes], Awaitable[None]]
    response: Callable[[Data, bytes], Awaitable[None]] | None = None


_current: ContextVar[BridgeCompletionScope | None] = ContextVar("bridge_completion_scope", default=None)


def current_completion_scope() -> BridgeCompletionScope | None:
    return _current.get()


@contextmanager
def bridge_completion_scope(scope: BridgeCompletionScope | None) -> Iterator[None]:
    token = _current.set(scope)
    try:
        yield
    finally:
        _current.reset(token)
