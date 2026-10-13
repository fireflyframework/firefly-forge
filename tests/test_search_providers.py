# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json

import httpx
import pytest

from firefly_forge.search_tools import ENDPOINT, SearchTools


@pytest.mark.asyncio
async def test_tavily_explicit_configuration_uses_bounded_post_and_scoped_citations(tmp_path):
    seen = []

    def transport(request):
        seen.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://api.tavily.com/search"
        assert request.headers["Authorization"] == "Bearer tavily-private-key"
        assert "X-Subscription-Token" not in request.headers
        assert json.loads(request.content) == {
            "query": "pyfly transactions",
            "max_results": 3,
            "search_depth": "basic",
            "topic": "general",
            "auto_parameters": False,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_usage": True,
            "include_domains": ["fireflyframework.org"],
            "include_domains_mode": "restrict",
        }
        assert request.extensions["timeout"]["read"] == 15
        return httpx.Response(
            200,
            json={
                "request_id": "request-42",
                "usage": {"credits": 1},
                "results": [
                    {"title": "Transactions", "url": "https://docs.fireflyframework.org/tx", "content": "Commit."},
                    {"title": "False match", "url": "https://evil-fireflyframework.org/tx", "content": "Reject."},
                    {"title": "Secret", "url": "https://fireflyframework.org/tx", "content": "tavily-private-key"},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        tool = SearchTools(tmp_path, client=client)
        configured = tool.configure(
            {"provider": "tavily", "api_key": "tavily-private-key", "include_domains": ["FireflyFramework.org"]}
        )
        assert configured["include_domains"] == ["fireflyframework.org"]
        result = await tool.search("pyfly transactions", 3)
        assert len(seen) == 1
        assert result["provider"] == "tavily"
        assert result["request_id"] == "request-42" and result["usage"] == {"credits": 1}
        assert len(result["results"]) == 1
        citation = result["results"][0]
        assert citation["snippet"] == "Commit."
        assert tool.citation(citation["citation_id"]) == citation
        assert "tavily-private-key" not in json.dumps(result)


@pytest.mark.asyncio
async def test_legacy_frozen_brave_revision_survives_provider_switch_without_shape_changes(tmp_path):
    seen = []

    def transport(request):
        seen.append(request)
        assert request.method == "GET" and str(request.url).startswith(ENDPOINT)
        assert request.headers["X-Subscription-Token"] == "old-brave-key"
        return httpx.Response(200, json={"web": {"results": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        tool = SearchTools(tmp_path, client=client)
        legacy = tool.configure({"api_key": "old-brave-key"})
        assert set(legacy) == {"provider", "endpoint", "revision", "configured", "max_results", "live_search_verified"}
        raw_before = (tmp_path / "search-revisions" / (legacy["revision"] + ".json")).read_bytes()
        tool.configure({"provider": "tavily", "api_key": "new-tavily-key", "include_domains": []})
        assert (await tool.search("framework docs", snapshot=legacy))["provider"] == "brave"
        assert len(seen) == 1
        assert (tmp_path / "search-revisions" / (legacy["revision"] + ".json")).read_bytes() == raw_before


def test_switching_provider_never_reuses_old_vendor_key(tmp_path):
    tool = SearchTools(tmp_path)
    before = tool.configure({"api_key": "old-brave-key"})
    for values in ({"provider": "tavily"}, {"provider": "tavily", "clear_secret": True}):
        with pytest.raises(ValueError, match="new.*key"):
            tool.configure(values)
        assert tool.configuration() == before
    with pytest.raises(ValueError, match="different.*key"):
        tool.configure({"provider": "tavily", "api_key": "old-brave-key"})
    assert tool.configuration() == before
    tool.configure({"api_key": "rotated-brave-key"})
    with pytest.raises(ValueError, match="different.*key"):
        tool.configure({"provider": "tavily", "api_key": "old-brave-key"})


@pytest.mark.parametrize(
    "domains",
    [
        ["https://github.com"],
        ["*.github.com"],
        ["localhost"],
        ["127.0.0.1"],
        ["private.local"],
        ["a" * 254],
        ["github.com"] * 11,
    ],
)
def test_domain_configuration_rejects_unbounded_or_nonpublic_hosts(tmp_path, domains):
    tool = SearchTools(tmp_path)
    with pytest.raises(ValueError, match="domain"):
        tool.configure({"provider": "tavily", "api_key": "private-key", "include_domains": domains})
    assert tool.configuration()["configured"] is False


@pytest.mark.asyncio
async def test_tampered_domains_or_provider_cannot_change_frozen_search(tmp_path):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        tool = SearchTools(tmp_path, client=client)
        frozen = tool.configure({"provider": "tavily", "api_key": "private-key", "include_domains": ["github.com"]})
        for mutation in ({"provider": "brave"}, {"include_domains": []}):
            with pytest.raises(ValueError, match="snapshot"):
                await tool.search("framework docs", snapshot={**frozen, **mutation})
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,kind", [(429, "rate_limit"), (432, "quota"), (433, "quota"), (401, "authentication")])
async def test_provider_errors_are_typed_sanitized_and_never_retried(tmp_path, status, kind):
    from firefly_forge.search_tools import SearchProviderError

    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(status, text="Provider reflected private-tavily-key in its error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        tool = SearchTools(tmp_path, client=client)
        tool.configure({"provider": "tavily", "api_key": "private-tavily-key"})
        with pytest.raises(SearchProviderError) as raised:
            await tool.search("framework docs")
        assert raised.value.provider == "tavily"
        assert raised.value.kind == kind and raised.value.status == status
        assert "private-tavily-key" not in str(raised.value)
        assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        [],
        {"results": "wrong"},
        {"results": [{}]},
        {"results": [{"url": "https://github.com", "content": {"bad": True}}]},
    ],
)
async def test_malformed_response_is_not_reported_as_success(tmp_path, body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        tool = SearchTools(tmp_path, client=client)
        tool.configure({"provider": "tavily", "api_key": "private-key"})
        with pytest.raises(ValueError, match="validated|response"):
            await tool.search("framework docs")
        assert list(tool.cache.glob("*.json")) == []


@pytest.mark.asyncio
async def test_connection_test_reports_selected_provider_and_nonempty_evidence(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []}))
    ) as client:
        tool = SearchTools(tmp_path, client=client)
        tool.configure({"provider": "tavily", "api_key": "private-key"})
        with pytest.raises(ValueError, match="citation"):
            await tool.test()
        assert tool.configuration()["live_search_verified"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["redirect", "oversized", "timeout", "partial-invalid", "metadata-secret"])
async def test_unvalidated_or_unknown_provider_outcomes_never_admit_partial_citations(tmp_path, case):
    calls = []

    def transport(request):
        calls.append(request)
        if case == "redirect":
            return httpx.Response(302, headers={"Location": "https://unapproved.example/search"})
        if case == "oversized":
            return httpx.Response(200, content=b"x" * 1_000_001)
        if case == "timeout":
            raise httpx.ReadTimeout("Timeout reflected private-key", request=request)
        results = [{"url": "https://github.com/fireflyframework", "content": "Documentation."}]
        if case == "partial-invalid":
            results.append({"url": "https://github.com/bad", "content": {"bad": True}})
        return httpx.Response(
            200, json={"results": results, **({"request_id": "private-key"} if case == "metadata-secret" else {})}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        tool = SearchTools(tmp_path, client=client)
        tool.configure({"provider": "tavily", "api_key": "private-key"})
        with pytest.raises(ValueError) as raised:
            await tool.search("framework docs")
        assert "private-key" not in str(raised.value)
        assert len(calls) == 1 and list(tool.cache.glob("*.json")) == []


@pytest.mark.asyncio
async def test_brave_domain_restrictions_filter_citations_without_changing_provider_protocol(tmp_path):
    def transport(request):
        assert request.method == "GET" and request.url.host == "api.search.brave.com"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"url": "https://github.com/fireflyframework", "description": "Allowed"},
                        {"url": "https://github.com.evil.example/fireflyframework", "description": "Rejected"},
                        {"url": "http://internal.local/", "description": "Rejected"},
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        tool = SearchTools(tmp_path, client=client)
        tool.configure({"api_key": "brave-key", "include_domains": ["github.com"]})
        response = await tool.search("framework docs", 3)
        assert [citation["snippet"] for citation in response["results"]] == ["Allowed"]
