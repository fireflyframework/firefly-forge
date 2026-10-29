# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Selected new model work keeps transport bounds without a hidden response deadline."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Self

import httpx2

MODEL_CLEANUP_TIMEOUT_SECONDS = 10
logger = logging.getLogger(__name__)


class ModelOperationCleanupUnavailable(RuntimeError):
    pass


def _observe_unconfirmed_cleanup(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("Late owned model cleanup failed after its control deadline")


async def bounded_model_cleanup(operation: Callable[[], Awaitable[None]], detail: str) -> None:
    async def execute() -> None:
        await operation()

    task = asyncio.create_task(execute())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MODEL_CLEANUP_TIMEOUT_SECONDS
    cancelled = False
    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            task.cancel()
            task.add_done_callback(_observe_unconfirmed_cleanup)
            raise ModelOperationCleanupUnavailable(detail)
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            cancelled = True
    try:
        task.result()
    except (Exception, asyncio.CancelledError) as exc:
        raise ModelOperationCleanupUnavailable(detail) from exc
    if cancelled:
        raise asyncio.CancelledError


class ModelOperationClient(httpx2.AsyncClient):
    construction_refused = False
    construction_claimed = False
    construction_cleanup: asyncio.Task[None] | None = None

    async def send(self, request: httpx2.Request, **kwargs: Any) -> httpx2.Response:
        if self.construction_refused:
            raise ValueError("A refused model construction cannot dispatch")
        # Some SDKs send a scalar timeout on every request, overriding client defaults.
        # The owning invocation enforces the original chosen work deadline instead.
        request.extensions["timeout"] = {"connect": 10, "read": None, "write": 30, "pool": 10}
        return await super().send(request, **kwargs)


class OwnedModelProvider:
    """Own one public SDK client across nested public model contexts."""

    client: Any

    def bind_operation_client(self, client: ModelOperationClient) -> None:
        self.operation_client = client
        self.operation_lock = asyncio.Lock()
        self.operation_scopes = 0
        self.operation_closed = False

    async def __aenter__(self) -> Self:
        async with self.operation_lock:
            if self.operation_closed or self.operation_client.is_closed:
                raise ValueError("Create a fresh model for another owned invocation")
            self.operation_scopes += 1
        return self

    async def _close_operation(self) -> None:
        await close_owned_model_client(self.client, self.operation_client)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        async with self.operation_lock:
            if self.operation_scopes <= 0:
                raise ValueError("Owned model context was not entered")
            self.operation_scopes -= 1
            if self.operation_scopes:
                return False
            self.operation_closed = True
            self.operation_client.construction_refused = True
            cleanup = asyncio.create_task(self._close_operation())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError()
        return False


def owned_model_provider(provider_type: type[Any], http_client: ModelOperationClient, **options: Any) -> Any:
    # Construct an application-owned subtype through the provider's public constructor;
    # no SDK instance methods, caches or private ownership fields are replaced.
    owned_type = type("Owned" + provider_type.__name__, (OwnedModelProvider, provider_type), {})
    provider = owned_type(**options)
    provider.bind_operation_client(http_client)
    return provider


async def close_owned_model_client(client: Any, http_client: ModelOperationClient) -> None:
    http_client.construction_refused = True
    unavailable = False
    cancelled = False
    try:
        await bounded_model_cleanup(
            client.aio.aclose if hasattr(client, "aio") else client.close,
            "Owned model vendor cleanup was not confirmed",
        )
    except ModelOperationCleanupUnavailable:
        unavailable = True
    except asyncio.CancelledError:
        cancelled = True
    finally:
        try:
            await close_operation_http(http_client)
        except ModelOperationCleanupUnavailable:
            unavailable = True
        except asyncio.CancelledError:
            cancelled = True
    if unavailable:
        raise ModelOperationCleanupUnavailable("Owned model cleanup was not confirmed")
    if cancelled:
        raise asyncio.CancelledError


async def close_operation_http(client: ModelOperationClient) -> None:
    client.construction_refused = True
    await bounded_model_cleanup(client.aclose, "Owned model HTTP cleanup was not confirmed")


@contextmanager
def model_client_construction(client: ModelOperationClient | None) -> Iterator[None]:
    if client is not None:
        if client.construction_claimed or client.construction_refused or client.is_closed:
            raise ValueError("This operation HTTP client is already owned or unavailable")
        client.construction_claimed = True
    try:
        yield
    except BaseException:
        if client is not None:
            # Construction cannot issue an async model POST. Refuse any later use and
            # release the unstarted transport without a global client/task registry.
            client.construction_refused = True
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(close_operation_http(client))
            else:
                client.construction_cleanup = loop.create_task(close_operation_http(client))

                def observed(task: asyncio.Task[None]) -> None:
                    if task.cancelled() or task.exception() is not None:
                        logger.error("Undispatched model construction cleanup was not confirmed")

                client.construction_cleanup.add_done_callback(observed)
        raise
