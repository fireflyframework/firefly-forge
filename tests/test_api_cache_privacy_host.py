# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The outer auth filter keeps explicit privacy while forbidding API caching."""

from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import Response

from firefly_forge.api import OperatorAuth
from firefly_forge.identity import AUTH_PREFIX


@pytest.mark.parametrize(
    "existing,expected",
    [
        ("private, no-store", "private, no-store"),
        ("Private, max-age=3600", "private, no-store"),
        ("public, max-age=3600", "no-store"),
        ("no-store", "no-store"),
        (None, "no-store"),
    ],
)
async def test_outer_auth_filter_retains_private_directive_without_permitting_api_caching(existing, expected):
    auth = OperatorAuth("public-cache-policy-fixture", SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    request = Request({"type": "http", "method": "GET", "path": AUTH_PREFIX + "/configuration", "headers": []})

    async def next_response(_request):
        return Response(headers={"Cache-Control": existing} if existing else {})

    response = await auth.do_filter(request, next_response)
    assert response.headers["cache-control"] == expected
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_outer_auth_filter_keeps_document_cache_revalidation():
    auth = OperatorAuth("public-cache-policy-fixture", SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    async def next_response(_request):
        return Response(headers={"Cache-Control": "private"})

    response = await auth.do_filter(request, next_response)
    assert response.headers["cache-control"] == "no-cache"
