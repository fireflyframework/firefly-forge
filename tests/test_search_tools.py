# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import importlib
import json

import httpx
import pytest


def service(tmp_path, client=None):
    try:
        return importlib.import_module("firefly_forge.search_tools").SearchTools(tmp_path, client=client)
    except ImportError as exc:
        raise AssertionError("Configured server-mediated search is missing") from exc


@pytest.mark.asyncio
async def test_search_requires_explicit_private_configuration_and_redacts_get(tmp_path):
    tool = service(tmp_path)
    assert tool.configuration()["configured"] is False
    with pytest.raises(ValueError, match="configured"):
        await tool.search("firefly framework")
    public = tool.configure({"api_key": "private-search-secret"})
    assert "private-search-secret" not in json.dumps(public)
    assert public["configured"]
    assert (tmp_path / "search.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_search_calls_real_protocol_only_fixed_origin_and_returns_bounded_citations(tmp_path):
    requests = []

    def transport(request):
        requests.append(request)
        assert request.url.host == "api.search.brave.com"
        assert request.headers["X-Subscription-Token"] == "selected-secret"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Official PyFly",
                            "url": "https://github.com/fireflyframework/pyfly",
                            "description": "Python framework",
                        },
                        {
                            "title": "Unsafe",
                            "url": "https://user:password@example.com/path",
                            "description": "not admitted",
                        },
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        tool = service(tmp_path, client)
        tool.configure({"api_key": "selected-secret"})
        result = await tool.search("PyFly framework", 2)
        assert len(result["results"]) == 1
        assert result["results"][0]["citation_id"].startswith("web-")
        assert result["results"][0]["url"] == "https://github.com/fireflyframework/pyfly"
        assert "selected-secret" not in json.dumps(result)
        assert result["untrusted_content"] is True
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_search_snapshot_keeps_credential_revision_and_rejects_secret_queries(tmp_path):
    seen = []

    def transport(request):
        seen.append(request.headers["X-Subscription-Token"])
        return httpx.Response(200, json={"web": {"results": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        tool = service(tmp_path, client)
        tool.configure({"api_key": "old-private-key"})
        snapshot = tool.snapshot()
        tool.configure({"api_key": "new-private-key"})
        await tool.search("Firefly docs", snapshot=snapshot)
        assert seen == ["old-private-key"]
        with pytest.raises(ValueError, match="credential"):
            await tool.search("old-private-key", snapshot=snapshot)
        assert "old-private-key" not in json.dumps(snapshot)
