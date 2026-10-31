# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded GitHub GraphQL through native OAuth resource custody."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from typing import Any, cast
from urllib.parse import urlsplit

import httpx2

from firefly_forge.github_projects_models import (
    BoardCreateInputV1,
    BoardDiscoveryInputV1,
    BoardDiscoveryV1,
    BoardIdentity,
)
from firefly_forge.journal import digest

ENDPOINT = "https://api.github.com/graphql"
HTTP_TIMEOUT = 15
PROVIDERS = frozenset({"github-projects", "github-projects-private"})
FIELDS = """id title url closed public viewerCanUpdate fields(first:100,after:$after) {
 pageInfo { hasNextPage endCursor } nodes { __typename
 ... on ProjectV2SingleSelectField { id name options { id name } } }
}"""
DISCOVERY = """query ForgeBoard($owner:String!,$number:Int!,$repoOwner:String!,$repoName:String!,$after:String) {
 viewer { id login } repository(owner:$repoOwner,name:$repoName) { id databaseId nameWithOwner isPrivate }
 owner:OWNER(login:$owner) { projectV2(number:$number) { FIELDS } }
}"""
ITEMS = """query ForgeBoardItems($board:ID!,$after:String) { node(id:$board) { ... on ProjectV2 {
 items(first:100,after:$after) { pageInfo { hasNextPage endCursor } nodes { id
 content { __typename ... on Issue { id number repository { id } } ... on PullRequest { id number repository { id } } }
 fieldValues(first:100) { pageInfo { hasNextPage endCursor } nodes {
 ... on ProjectV2ItemFieldSingleSelectValue { optionId field { ... on ProjectV2SingleSelectField { id } } }
 } }
 } } } } }"""
ITEM_FIELDS = """query ForgeBoardItemFields($item:ID!,$after:String) { node(id:$item) {
 ... on ProjectV2Item { fieldValues(first:100,after:$after) { pageInfo { hasNextPage endCursor }
 nodes { ... on ProjectV2ItemFieldSingleSelectValue { optionId field { ... on ProjectV2SingleSelectField { id } } } }
 } } } }"""
CONTENT = """query ForgeBoardContent($owner:String!,$name:String!,$number:Int!) {
 repository(owner:$owner,name:$name) { id databaseId nameWithOwner isPrivate CONTENT(number:$number) { id number url } }
}"""
ADD = """mutation ForgeBoardAdd($input:AddProjectV2ItemByIdInput!) {
 addProjectV2ItemById(input:$input) { item { id } clientMutationId }
}"""
UPDATE = """mutation ForgeBoardState($input:UpdateProjectV2ItemFieldValueInput!) {
 updateProjectV2ItemFieldValue(input:$input) { projectV2Item { id } clientMutationId }
}"""
CREATION_OWNER = """query ForgeBoardCreationOwner($repoOwner:String!,$repoName:String!) {
 viewer { id login } repository(owner:$repoOwner,name:$repoName) { id databaseId nameWithOwner isPrivate }
}"""
CREATED_BOARD = """id title number url closed public viewerCanUpdate owner { __typename
 ... on User { id login } }
"""
CREATE = """mutation ForgeBoardCreate($input:CreateProjectV2Input!) {
 createProjectV2(input:$input) { clientMutationId projectV2 { BOARD } }
}""".replace("BOARD", CREATED_BOARD)
CREATION_READBACK = """query ForgeBoardCreationReadback($board:ID!,$repoOwner:String!,$repoName:String!) {
 viewer { id login } repository(owner:$repoOwner,name:$repoName) { id databaseId nameWithOwner isPrivate }
 node(id:$board) { ... on ProjectV2 { BOARD } }
}""".replace("BOARD", CREATED_BOARD)
ADMITTED_DOCUMENTS = frozenset(
    {DISCOVERY.replace("OWNER", kind).replace("FIELDS", FIELDS) for kind in ("user", "organization")}
) | frozenset(
    {
        ITEMS,
        ITEM_FIELDS,
        CONTENT.replace("CONTENT", "issue"),
        CONTENT.replace("CONTENT", "pullRequest"),
        ADD,
        UPDATE,
        CREATION_OWNER,
        CREATE,
        CREATION_READBACK,
    }
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous GraphQL response")
        result[key] = value
    return result


def repository_coordinates(repository: dict[str, Any]) -> tuple[str, str]:
    url = repository.get("url", "")
    if url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment:
            raise ValueError("Projects requires the exact registered GitHub.com repository")
        path = parsed.path.removeprefix("/")
    parts = path.removesuffix(".git").split("/")
    if len(parts) != 2 or any(not part or not all(c.isalnum() or c in "-_." for c in part) for part in parts):
        raise ValueError("Projects requires canonical GitHub repository coordinates")
    if repository.get("provider") != "github" or not str(repository.get("remote_repository_id", "")).isdigit():
        raise ValueError("Verify the registered GitHub repository identity before connecting a board")
    return parts[0], parts[1]


class GitHubProjectsGraphQL:
    def __init__(
        self,
        resources: Any,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        run_id: str | None = None,
        repository_id: str | None = None,
    ) -> None:
        self.resources, self.inner = resources, transport
        self.run_id, self.repository_id = run_id, repository_id

    async def create_owner(
        self, binding: dict[str, Any], actor: Any, body: BoardCreateInputV1, repository: dict[str, Any]
    ) -> dict[str, Any]:
        owner, name = repository_coordinates(repository)
        data = await self.execute(binding, actor, CREATION_OWNER, {"repoOwner": owner, "repoName": name})
        try:
            self._repository(data["repository"], repository)
            viewer = data["viewer"]
            if viewer["login"].lower() != body.owner.lower() or not isinstance(viewer["id"], str) or not viewer["id"]:
                raise ValueError
            if data["repository"]["isPrivate"] and binding.get("provider") != "github-projects-private":
                raise ValueError
            return {"viewer": viewer, "owner_id": viewer["id"], "repository_id": data["repository"]["id"]}
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ValueError("Create the private board under the current connected GitHub user") from None

    @staticmethod
    def _created_board(raw: dict[str, Any], body: BoardCreateInputV1, owner: dict[str, Any]) -> dict[str, Any]:
        try:
            if (
                raw["owner"] != {"__typename": "User", "id": owner["owner_id"], "login": owner["viewer"]["login"]}
                or raw["public"] is not False
                or raw["closed"] is not False
                or raw["viewerCanUpdate"] is not True
                or raw["title"] != body.title
                or type(raw["number"]) is not int
                or raw["number"] < 1
                or raw["url"].lower() != f"https://github.com/users/{body.owner}/projects/{raw['number']}".lower()
            ):
                raise ValueError
            return BoardIdentity(
                id=raw["id"],
                title=raw["title"],
                url=raw["url"],
                owner_kind="user",
                owner=body.owner,
                number=raw["number"],
                viewer_can_update=True,
                closed=False,
                private=True,
            ).model_dump(mode="json")
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ValueError("Created board identity or private visibility requires reconciliation") from None

    async def create_private(
        self,
        binding: dict[str, Any],
        actor: Any,
        body: BoardCreateInputV1,
        repository: dict[str, Any],
        owner: dict[str, Any],
    ) -> dict[str, Any]:
        data = await self.execute(
            binding,
            actor,
            CREATE,
            {
                "input": {"ownerId": owner["owner_id"], "title": body.title, "clientMutationId": body.request_id},
            },
        )
        try:
            result = data["createProjectV2"]
            if result["clientMutationId"] != body.request_id:
                raise ValueError
            created = self._created_board(result["projectV2"], body, owner)
        except (KeyError, TypeError, ValueError):
            raise ValueError("Board creation outcome requires read-only reconciliation") from None
        repo_owner, name = repository_coordinates(repository)
        observed = await self.execute(
            binding,
            actor,
            CREATION_READBACK,
            {
                "board": created["id"],
                "repoOwner": repo_owner,
                "repoName": name,
            },
        )
        try:
            self._repository(observed["repository"], repository)
            if (
                observed["viewer"] != owner["viewer"]
                or observed["repository"]["id"] != owner["repository_id"]
                or self._created_board(observed["node"], body, owner) != created
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("Created board has no exact current private readback") from None
        return created

    async def execute(
        self, binding: dict[str, Any], actor: Any, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        if binding.get("provider") not in PROVIDERS:
            raise ValueError("Use an explicitly consented GitHub Projects OAuth connection")
        if query not in ADMITTED_DOCUMENTS:
            raise ValueError("Select an admitted GitHub Projects operation")
        transport = self.resources.transport(
            binding, actor=actor, inner=self.inner, run_id=self.run_id, repository_id=self.repository_id
        )
        async with (
            asyncio.timeout(HTTP_TIMEOUT),
            httpx2.AsyncClient(transport=transport, timeout=10, follow_redirects=False, trust_env=False) as client,
            client.stream(
                "POST",
                ENDPOINT,
                json={"query": query, "variables": variables},
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            ) as response,
        ):
            if response.status_code != 200:
                raise ValueError("GitHub Projects request unavailable; inspect current consent and access")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("GitHub Projects requires an uncompressed bounded response")
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                if len(content) + len(chunk) > 262144:
                    raise ValueError("GitHub Projects response exceeds its finite bound")
                content.extend(chunk)
        try:
            payload = json.loads(content, object_pairs_hook=_unique_object)
            if not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data"), dict):
                raise ValueError
            return cast(dict[str, Any], payload["data"])
        except (ValueError, TypeError):
            raise ValueError("GitHub Projects returned incomplete or invalid evidence") from None

    @staticmethod
    def _repository(remote: dict[str, Any], repository: dict[str, Any]) -> None:
        owner, name = repository_coordinates(repository)
        if (
            str(remote.get("databaseId")) != str(repository["remote_repository_id"])
            or remote.get("nameWithOwner", "").lower() != f"{owner}/{name}".lower()
            or type(remote.get("isPrivate")) is not bool
        ):
            raise ValueError("GitHub repository identity changed")

    @staticmethod
    def cursor(page: dict[str, Any], seen: set[str]) -> str | None:
        info = page["pageInfo"]
        if type(info["hasNextPage"]) is not bool or not isinstance(page["nodes"], list) or len(page["nodes"]) > 100:
            raise ValueError("Provider page shape changed")
        if not info["hasNextPage"]:
            return None
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024 or cursor in seen:
            raise ValueError("Provider cursor is missing or repeated")
        seen.add(cursor)
        return cursor

    async def discover(
        self, binding: dict[str, Any], actor: Any, request: BoardDiscoveryInputV1, repository: dict[str, Any]
    ) -> BoardDiscoveryV1:
        return await self._discover(binding, actor, request, repository)

    async def _discover(
        self, binding: dict[str, Any], actor: Any, request: BoardDiscoveryInputV1, repository: dict[str, Any]
    ) -> BoardDiscoveryV1:
        owner, name = repository_coordinates(repository)
        query = DISCOVERY.replace("OWNER", request.owner_kind).replace("FIELDS", FIELDS)
        data = None
        after = None
        seen: set[str] = set()
        identity = None
        while True:
            page = await self.execute(
                binding,
                actor,
                query,
                {
                    "owner": request.owner,
                    "number": request.number,
                    "repoOwner": owner,
                    "repoName": name,
                    "after": after,
                },
            )
            try:
                board = page["owner"]["projectV2"]
                current = {
                    "viewer": page["viewer"],
                    "repository": page["repository"],
                    "board": {key: value for key, value in board.items() if key != "fields"},
                }
                if identity is not None and current != identity:
                    raise ValueError("Board identity changed between cursor pages")
                identity = current
                fields = board["fields"]
                after = self.cursor(fields, seen)
                if data is None:
                    data = copy.deepcopy(page)
                else:
                    data["owner"]["projectV2"]["fields"]["nodes"].extend(fields["nodes"])
                if after is None:
                    data["owner"]["projectV2"]["fields"]["pageInfo"] = {"hasNextPage": False}
                    break
            except (KeyError, TypeError, AttributeError) as error:
                raise ValueError("Board field discovery is incomplete") from error
        assert data is not None
        try:
            remote, board = data["repository"], data["owner"]["projectV2"]
            self._repository(remote, repository)
            owner_path = "users" if request.owner_kind == "user" else "orgs"
            expected_url = f"https://github.com/{owner_path}/{request.owner}/projects/{request.number}"
            if (
                board["url"].lower() != expected_url.lower()
                or board["closed"] is not False
                or board["public"] is not False
                or board["viewerCanUpdate"] is not True
                or board["fields"]["pageInfo"]["hasNextPage"] is not False
                or request.owner_kind == "user"
                and data["viewer"]["login"].lower() != request.owner.lower()
            ):
                raise ValueError
            if remote["isPrivate"] and binding["provider"] != "github-projects-private":
                raise ValueError
            fields = [
                {key: field[key] for key in ("id", "name", "options")}
                for field in board["fields"]["nodes"]
                if field.get("__typename") == "ProjectV2SingleSelectField"
            ]
            packet = {
                "version": 1,
                "connection_name": request.connection_name,
                "board": {
                    "id": board["id"],
                    "title": board["title"],
                    "url": board["url"],
                    "owner_kind": request.owner_kind,
                    "owner": request.owner,
                    "number": request.number,
                    "viewer_can_update": True,
                    "closed": False,
                    "private": True,
                },
                "repository": {
                    "id": remote["id"],
                    "name_with_owner": remote["nameWithOwner"],
                    "private": remote["isPrivate"],
                },
                "viewer": data["viewer"],
                "fields": fields,
                "truncated": False,
            }
            if len({field["id"] for field in fields}) != len(fields) or any(
                len({option["id"] for option in field["options"]}) != len(field["options"]) for field in fields
            ):
                raise ValueError
            return BoardDiscoveryV1.model_validate({**packet, "discovery_digest": digest(packet)})
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ValueError(
                "Select an accessible open board and complete single-select field from the registered repository"
            ) from None

    async def item_stream(self, binding: dict[str, Any], actor: Any, board_id: str) -> AsyncIterator[dict[str, Any]]:
        after = None
        seen: set[str] = set()
        ids: set[str] = set()
        while True:
            data = await self.execute(binding, actor, ITEMS, {"board": board_id, "after": after})
            try:
                page = data["node"]["items"]
                after = self.cursor(page, seen)
                for raw in page["nodes"]:
                    item = copy.deepcopy(raw)
                    if not isinstance(item["id"], str) or item["id"] in ids:
                        raise ValueError("Board item identity repeated across cursor pages")
                    ids.add(item["id"])
                    values = item["fieldValues"]
                    field_seen: set[str] = set()
                    field_after = self.cursor(values, field_seen)
                    if field_after is not None:
                        while True:
                            fields = await self.execute(
                                binding, actor, ITEM_FIELDS, {"item": item["id"], "after": field_after}
                            )
                            current = fields["node"]["fieldValues"]
                            field_after = self.cursor(current, field_seen)
                            values["nodes"].extend(current["nodes"])
                            if field_after is None:
                                break
                    values["pageInfo"] = {"hasNextPage": False}
                    yield item
                if after is None:
                    return
            except (KeyError, TypeError) as error:
                raise ValueError("Board item discovery is incomplete; no mutation is authorized") from error

    async def find_item(
        self, binding: dict[str, Any], actor: Any, board_id: str, content: dict[str, Any]
    ) -> dict[str, Any] | None:
        found = None
        async for item in self.item_stream(binding, actor, board_id):
            target = item.get("content")
            if target is None:
                raise ValueError("Board contains redacted items; absence is not complete evidence")
            if target.get("id") == content["id"]:
                if found is not None or target.get("repository", {}).get("id") != content["repository_id"]:
                    raise ValueError("Board item identity is ambiguous or leaves the original repository")
                found = item
        return found

    async def content(
        self, binding: dict[str, Any], actor: Any, repository: dict[str, Any], kind: str, number: int
    ) -> dict[str, Any]:
        if kind not in {"issue", "pull_request"} or type(number) is not int or number < 1:
            raise ValueError("Use retained native issue or pull request identity")
        owner, name = repository_coordinates(repository)
        field = "issue" if kind == "issue" else "pullRequest"
        data = await self.execute(
            binding, actor, CONTENT.replace("CONTENT", field), {"owner": owner, "name": name, "number": number}
        )
        try:
            remote = data["repository"]
            self._repository(remote, repository)
            result = remote[field]
            if result["number"] != number or not isinstance(result["id"], str):
                raise ValueError
            return {**result, "repository_id": remote["id"], "kind": kind}
        except (KeyError, TypeError, ValueError):
            raise ValueError("Retained content is unavailable in the original repository") from None

    async def add(self, binding: dict[str, Any], actor: Any, board_id: str, content_id: str, intent_id: str) -> str:
        data = await self.execute(
            binding,
            actor,
            ADD,
            {"input": {"projectId": board_id, "contentId": content_id, "clientMutationId": intent_id}},
        )
        try:
            result = data["addProjectV2ItemById"]
            if result["clientMutationId"] != intent_id or not isinstance(result["item"]["id"], str):
                raise ValueError
            return result["item"]["id"]
        except (KeyError, TypeError, ValueError):
            raise ValueError("Add-item outcome requires read-only reconciliation") from None

    async def update(
        self,
        binding: dict[str, Any],
        actor: Any,
        board_id: str,
        item_id: str,
        field_id: str,
        option_id: str,
        intent_id: str,
    ) -> None:
        data = await self.execute(
            binding,
            actor,
            UPDATE,
            {
                "input": {
                    "projectId": board_id,
                    "itemId": item_id,
                    "fieldId": field_id,
                    "value": {"singleSelectOptionId": option_id},
                    "clientMutationId": intent_id,
                }
            },
        )
        try:
            result = data["updateProjectV2ItemFieldValue"]
            if result["clientMutationId"] != intent_id or result["projectV2Item"]["id"] != item_id:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("Field-update outcome requires read-only reconciliation") from None
