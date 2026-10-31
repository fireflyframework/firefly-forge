# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import copy

import httpx2
import pytest

from firefly_forge import github_projects_graphql as module
from firefly_forge.github_projects_graphql import GitHubProjectsGraphQL
from firefly_forge.github_projects_models import BoardDiscoveryInputV1


class Resources:
    def transport(self, binding, **kwargs):
        assert binding == {"provider": "github-projects-private"}
        assert "actor" in kwargs
        return kwargs["inner"]


def payload():
    return {
        "data": {
            "viewer": {"id": "U_123", "login": "octocat"},
            "repository": {"id": "R_123", "databaseId": 77, "nameWithOwner": "team/product", "isPrivate": True},
            "owner": {
                "projectV2": {
                    "id": "PVT_123",
                    "title": "Delivery",
                    "url": "https://github.com/users/octocat/projects/2",
                    "viewerCanUpdate": True,
                    "closed": False,
                    "public": False,
                    "fields": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "__typename": "ProjectV2SingleSelectField",
                                "id": "PVTSSF_123",
                                "name": "Forge state",
                                "options": [{"id": "todo", "name": "Todo"}],
                            }
                        ],
                    },
                }
            },
        }
    }


async def discover(body, *, status=200, headers=None):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(status, json=body, headers=headers)

    client = GitHubProjectsGraphQL(Resources(), transport=httpx2.MockTransport(respond))
    result = await client.discover(
        {"provider": "github-projects-private"},
        object(),
        BoardDiscoveryInputV1(version=1, connection_name="github-board", owner_kind="user", owner="octocat", number=2),
        {"url": "https://github.com/team/product", "provider": "github", "remote_repository_id": "77"},
    )
    return result, calls


@pytest.mark.asyncio
async def test_discovery_uses_fixed_query_and_exact_repository_identity():
    result, calls = await discover(payload())
    assert result.board.id == "PVT_123"
    assert result.fields[0].options[0].id == "todo"
    assert len(calls) == 1 and str(calls[0].url) == "https://api.github.com/graphql"
    assert "Authorization" not in calls[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "partial",
        "pagination",
        "repository",
        "board_url",
        "closed",
        "option",
        "redirect",
        "public",
        "missing_visibility",
        "foreign_owner",
    ],
)
async def test_incomplete_foreign_or_unavailable_discovery_refuses(case):
    value = copy.deepcopy(payload())
    board = value["data"]["owner"]["projectV2"]
    if case == "partial":
        value["errors"] = [{"message": "provider secret must not escape"}]
    elif case == "pagination":
        board["fields"]["pageInfo"]["hasNextPage"] = True
    elif case == "repository":
        value["data"]["repository"]["databaseId"] = 78
    elif case == "board_url":
        board["url"] = "https://github.com/users/another/projects/2"
    elif case == "closed":
        board["closed"] = True
    elif case == "option":
        board["fields"]["nodes"][0]["options"][0]["id"] = ""
    elif case == "public":
        board["public"] = True
    elif case == "missing_visibility":
        board.pop("public")
    elif case == "foreign_owner":
        value["data"]["viewer"]["login"] = "another-user"
    with pytest.raises(ValueError) as error:
        await discover(value, status=302 if case == "redirect" else 200, headers={"location": "https://example.com"})
    assert "provider secret" not in str(error.value)


@pytest.mark.parametrize("case", ["compressed", "oversized", "duplicate_members"])
async def test_wire_bounds_refuse_ambiguous_or_unbounded_responses(case):
    body = b'{"data":{},"data":{"foreign":true}}' if case == "duplicate_members" else b"x" * 262145
    headers = {"content-encoding": "gzip"} if case == "compressed" else {}

    def reply(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx2.Response(200, headers=headers, stream=httpx2.ByteStream(body))

    client = GitHubProjectsGraphQL(Resources(), transport=httpx2.MockTransport(reply))
    with pytest.raises(ValueError):
        await client.execute({"provider": "github-projects-private"}, object(), "query Fixed { viewer { id } }", {})


async def test_total_http_deadline_covers_a_slow_stream(monkeypatch):
    class Slow(httpx2.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.1)
            yield b'{"data":{}}'

    monkeypatch.setattr(module, "HTTP_TIMEOUT", 0.01, raising=False)
    client = GitHubProjectsGraphQL(
        Resources(), transport=httpx2.MockTransport(lambda request: httpx2.Response(200, stream=Slow()))
    )
    with pytest.raises(TimeoutError):
        await client.execute({"provider": "github-projects-private"}, object(), module.CREATION_OWNER, {})


async def test_arbitrary_graphql_is_rejected_before_transport():
    calls = []
    client = GitHubProjectsGraphQL(
        Resources(),
        transport=httpx2.MockTransport(
            lambda request: calls.append(request) or httpx2.Response(200, json={"data": {}})
        ),
    )
    with pytest.raises(ValueError, match="admitted"):
        await client.execute(
            {"provider": "github-projects-private"},
            object(),
            'mutation Arbitrary { deleteProjectV2(input:{projectId:"foreign"}) { clientMutationId } }',
            {},
        )
    assert calls == []


async def test_discovery_completes_cursor_pages_beyond100_fields():
    responses = []
    for start, end in [(0, 100), (100, 101)]:
        value = payload()
        fields = value["data"]["owner"]["projectV2"]["fields"]
        fields["nodes"] = [
            {
                "__typename": "ProjectV2SingleSelectField",
                "id": f"FIELD_{i}",
                "name": f"Field {i}",
                "options": [{"id": f"OPT_{i}", "name": "Todo"}],
            }
            for i in range(start, end)
        ]
        fields["pageInfo"] = {"hasNextPage": end == 100, "endCursor": "cursor-100" if end == 100 else None}
        responses.append(value["data"])
    calls = []
    client = GitHubProjectsGraphQL(Resources())

    async def execute(binding, actor, query, variables):
        calls.append(variables)
        return responses[len(calls) - 1]

    client.execute = execute
    result = await client.discover(
        {"provider": "github-projects-private"},
        object(),
        BoardDiscoveryInputV1(version=1, connection_name="github-board", owner_kind="user", owner="octocat", number=2),
        {"url": "https://github.com/team/product", "provider": "github", "remote_repository_id": "77"},
    )
    assert len(result.fields) == 101
    assert [call.get("after") for call in calls] == [None, "cursor-100"]


async def test_discovery_drains_provider_pages_without_a_fifty_page_entity_ceiling():
    client = GitHubProjectsGraphQL(Resources())
    calls = []

    async def execute(binding, actor, query, variables):
        index = len(calls)
        calls.append(variables)
        value = payload()["data"]
        fields = value["owner"]["projectV2"]["fields"]
        fields["nodes"][0]["id"] = f"FIELD_{index}"
        fields["pageInfo"] = {"hasNextPage": index < 51, "endCursor": f"cursor-{index}"}
        return value

    client.execute = execute
    result = await client.discover(
        {"provider": "github-projects-private"},
        object(),
        BoardDiscoveryInputV1(version=1, connection_name="github-board", owner_kind="user", owner="octocat", number=2),
        {"url": "https://github.com/team/product", "provider": "github", "remote_repository_id": "77"},
    )
    assert len(calls) == len(result.fields) == 52
    assert [call.get("after") for call in calls] == [None, *[f"cursor-{i}" for i in range(51)]]
    assert not result.truncated


async def test_item_discovery_completes_beyond100_without_inventing_absence():
    calls = []
    client = GitHubProjectsGraphQL(Resources())

    async def execute(binding, actor, query, variables):
        calls.append(variables)
        first = len(calls) == 1
        return {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": first, "endCursor": "next" if first else None},
                    "nodes": [
                        {
                            "id": f"ITEM_{i}",
                            "content": {"id": f"ISSUE_{i}", "repository": {"id": "REPO"}},
                            "fieldValues": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                        }
                        for i in (range(100) if first else range(100, 101))
                    ],
                }
            }
        }

    client.execute = execute
    result = await client.find_item(
        {"provider": "github-projects-private"}, object(), "BOARD", {"id": "ISSUE_100", "repository_id": "REPO"}
    )
    assert result["id"] == "ITEM_100"
    assert [call.get("after") for call in calls] == [None, "next"]


async def test_item_field_values_complete_before_managed_field_decision():
    calls = []
    client = GitHubProjectsGraphQL(Resources())

    async def execute(binding, actor, query, variables):
        calls.append(variables)
        if "item" in variables:
            assert variables == {"item": "ITEM", "after": "field-next"}
            return {
                "node": {
                    "fieldValues": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [{"optionId": "todo", "field": {"id": "MANAGED"}}],
                    }
                }
            }
        return {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "id": "ITEM",
                            "content": {"id": "ISSUE", "repository": {"id": "REPO"}},
                            "fieldValues": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "field-next"},
                                "nodes": [{"optionId": "other", "field": {"id": "OTHER"}}],
                            },
                        }
                    ],
                }
            }
        }

    client.execute = execute
    item = await client.find_item(
        {"provider": "github-projects-private"}, object(), "BOARD", {"id": "ISSUE", "repository_id": "REPO"}
    )
    from firefly_forge.github_projects_effects import managed_value

    assert managed_value(item, "MANAGED") == "todo"
    assert len(calls) == 2


@pytest.mark.parametrize("case", ["cursor_repeat", "identity_change"])
async def test_incomplete_page_chain_cannot_authorize_discovery(case):
    client = GitHubProjectsGraphQL(Resources())
    calls = []

    async def execute(binding, actor, query, variables):
        calls.append(variables)
        value = payload()["data"]
        board = value["owner"]["projectV2"]
        board["fields"]["pageInfo"] = {"hasNextPage": True, "endCursor": "same"}
        if len(calls) > 1 and case == "identity_change":
            board["id"] = "OTHER_BOARD"
        return value

    client.execute = execute
    with pytest.raises(ValueError):
        await client.discover(
            {"provider": "github-projects-private"},
            object(),
            BoardDiscoveryInputV1(
                version=1, connection_name="github-board", owner_kind="user", owner="octocat", number=2
            ),
            {"url": "https://github.com/team/product", "provider": "github", "remote_repository_id": "77"},
        )
    assert len(calls) == 2
