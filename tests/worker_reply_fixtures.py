# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticate local test operators through the real persisted bootstrap login."""

from contextlib import asynccontextmanager

from starlette.requests import Request

from firefly_forge.identity import IdentityConfiguration, IdentityService
from firefly_forge.project_access import CURRENT_PRINCIPAL


@asynccontextmanager
async def authenticated_operator(store):
    service = IdentityService(
        store,
        config=IdentityConfiguration(
            mode="local_bootstrap",
            bootstrap_token="owned-worker-reply-fixture-token",
        ),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "scheme": "http",
            "server": ("127.0.0.1", 80),
            "client": ("127.0.0.1", 52001),
            "headers": [],
        }
    )
    login = await service.login_bootstrap("owned-worker-reply-fixture-token", request)
    request.scope["headers"] = [(b"cookie", ("forge_session=" + login.cookie).encode())]
    principal = await service.authenticate(request)
    assert principal is not None and principal.id == login.principal.id
    context = CURRENT_PRINCIPAL.set(principal)
    try:
        yield principal
    finally:
        CURRENT_PRINCIPAL.reset(context)
        await service.client.aclose()
