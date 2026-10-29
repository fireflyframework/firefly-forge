# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Selected new model work keeps transport bounds without a hidden response deadline."""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, Self

import httpx2


class ModelOperationClient(httpx2.AsyncClient):
    async def send(self, request: httpx2.Request, **kwargs: Any) -> httpx2.Response:
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
        try:
            if hasattr(self.client, "aio"):
                await self.client.aio.aclose()
                self.client.close()
            else:
                await self.client.close()
        finally:
            await self.operation_client.aclose()

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
