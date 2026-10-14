# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import httpx
import pytest

from firefly_forge.search_tools import SearchTools


@pytest.mark.asyncio
async def test_search_dispatch_observation_follows_private_admission_and_precedes_http(tmp_path):
    calls = []

    def response(request):
        assert calls == ["dispatch"]
        calls.append("http")
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        search = SearchTools(tmp_path, client=client)
        with pytest.raises(ValueError):
            await search.search("pyfly source", on_dispatch=lambda: calls.append("dispatch"))
        assert calls == []
        search.configure({"provider": "tavily", "api_key": "fixture-dispatch-search-key"})
        await search.search("pyfly source", on_dispatch=lambda: calls.append("dispatch"))
        assert calls == ["dispatch", "http"]
