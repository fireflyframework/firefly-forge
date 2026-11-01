# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A durable intent precedes create; absence of a receipt never grants a retry."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest

from firefly_forge import github_projects, github_projects_models
from firefly_forge.github_projects_graphql import GitHubProjectsGraphQL
from firefly_forge.project_access import AccessDenied


def creation_fixture():
    method = getattr(github_projects.GitHubProjects, "create", None)
    assert callable(method), "Private-board creation must use native durable admission"
    contract = getattr(github_projects_models, "BoardCreateInputV1", None)
    assert contract is not None
    manager = github_projects.GitHubProjects.__new__(github_projects.GitHubProjects)
    actor = SimpleNamespace(id=UUID(int=3))
    request = contract.model_validate(
        {
            "version": 1,
            "request_id": "1" * 32,
            "connection_name": "github-board",
            "owner": "octocat",
            "title": "Delivery",
            "acknowledge_private_creation": True,
        }
    )
    order, entities = [], {}
    revoked = False

    async def authority(*args, **kwargs):
        if revoked:
            raise AccessDenied("revoked")

    async def get(entity, key):
        return entities.get((entity.__name__, key))

    async def scalar(statement):
        scope = statement.compile().params.get("scope_digest_1")
        for (name, key), entity in entities.items():
            if (
                name == "ForgeGitHubProjectsCreationEntity"
                and entity.scope_digest == scope
                and ("ForgeGitHubProjectsCreationReceiptEntity", key) not in entities
            ):
                return entity
        return None

    def add(entity):
        entities[(type(entity).__name__, entity.id)] = entity
        order.append(type(entity).__name__)

    @asynccontextmanager
    async def locked(*args):
        yield SimpleNamespace(get=get, scalar=scalar, add=add)
        order.append("commit")

    async def credentials(value):
        pass

    async def repository(key):
        return {"id": key, "provider": "github", "url": "https://github.com/team/product", "remote_repository_id": "77"}

    async def bind(*args, **kwargs):
        return {"provider": "github-projects-private", "grant_family": "a" * 32}

    async def admission(*args, **kwargs):
        pass

    async def owner(*args, **kwargs):
        return {"viewer": {"id": "U_1", "login": "octocat"}, "owner_id": "U_1", "repository_id": "R_1"}

    async def create(*args, **kwargs):
        assert order[-1] == "commit", "The real provider dispatch must follow committed intent"
        order.append("remote_create")
        return {
            "id": "PVT_1",
            "number": 2,
            "title": "Delivery",
            "url": "https://github.com/users/octocat/projects/2",
            "owner_kind": "user",
            "owner": "octocat",
            "viewer_can_update": True,
            "closed": False,
            "private": True,
        }

    def revoke():
        nonlocal revoked
        revoked = True

    manager.authority, manager.locked = authority, locked
    manager.access = SimpleNamespace(actor_snapshot=lambda value: {"user_id": value.id.hex})
    manager.store = SimpleNamespace(admit_credentials=credentials, repository=repository)
    manager.resources = SimpleNamespace(bind=bind, _admission=admission)
    manager.remote = SimpleNamespace(create_owner=owner, create_private=create)
    return manager, actor, request, order, entities, revoke


async def test_create_commits_intent_before_dispatch_and_replay_only_reads_receipt():
    manager, actor, body, order, _, _ = creation_fixture()
    first = await manager.create("4" * 32, "5" * 32, body, actor)
    assert first.status == "completed" and first.board.private is True
    second = await manager.create("4" * 32, "5" * 32, body, actor)
    assert second == first
    assert order.count("remote_create") == 1


async def test_lost_create_response_is_durably_unknown_and_same_id_cannot_retry():
    manager, actor, body, order, _, _ = creation_fixture()

    async def lost(*args, **kwargs):
        order.append("remote_create")
        raise TimeoutError("synthetic lost response")

    manager.remote.create_private = lost
    with pytest.raises(TimeoutError):
        await manager.create("4" * 32, "5" * 32, body, actor)
    readback = await manager.create("4" * 32, "5" * 32, body, actor)
    assert readback.status == "unknown" and readback.board is None
    assert order.count("remote_create") == 1


async def test_unknown_creation_cannot_be_replayed_through_another_connection_alias():
    manager, actor, body, order, _, _ = creation_fixture()

    async def lost(*args, **kwargs):
        order.append("remote_create")
        raise TimeoutError("synthetic lost response")

    manager.remote.create_private = lost
    with pytest.raises(TimeoutError):
        await manager.create("4" * 32, "5" * 32, body, actor)
    alias = body.model_copy(update={"request_id": "8" * 32, "connection_name": "other-current-connection"})
    with pytest.raises(ValueError, match="unknown outcome"):
        await manager.create("4" * 32, "5" * 32, alias, actor)
    assert order.count("remote_create") == 1


async def test_revocation_after_remote_create_prevents_publishing_success():
    manager, actor, body, _, entities, revoke = creation_fixture()
    original = manager.remote.create_private

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        revoke()
        return result

    manager.remote.create_private = changed
    with pytest.raises(AccessDenied):
        await manager.create("4" * 32, "5" * 32, body, actor)
    assert not any("Receipt" in name for name, _ in entities)


async def test_request_identity_cannot_rebind_project_or_actor():
    manager, actor, body, order, _, _ = creation_fixture()
    await manager.create("4" * 32, "5" * 32, body, actor)
    with pytest.raises(ValueError):
        await manager.create("6" * 32, "5" * 32, body, actor)
    with pytest.raises(ValueError):
        await manager.create("4" * 32, "5" * 32, body, SimpleNamespace(id=UUID(int=9)))
    assert order.count("remote_create") == 1


@pytest.mark.parametrize("change", [None, "foreign_owner", "public", "changed_readback"])
async def test_fixed_create_graphql_requires_exact_owner_private_visibility_and_readback(change):
    _, actor, body, _, _, _ = creation_fixture()
    client = GitHubProjectsGraphQL(object())
    method = getattr(client, "create_private", None)
    assert callable(method), "Private creation needs a fixed provider operation and exact readback"
    calls = []
    board = {
        "id": "PVT_1",
        "number": 2,
        "title": "Delivery",
        "url": "https://github.com/users/octocat/projects/2",
        "public": False,
        "closed": False,
        "viewerCanUpdate": True,
        "owner": {"__typename": "User", "id": "U_1", "login": "octocat"},
    }
    if change == "foreign_owner":
        board["owner"]["id"] = "U_FOREIGN"
    elif change == "public":
        board["public"] = True

    async def execute(binding, actor, query, variables):
        calls.append((query, variables))
        if "mutation " in query:
            assert variables == {"input": {"ownerId": "U_1", "title": "Delivery", "clientMutationId": "1" * 32}}
            return {"createProjectV2": {"clientMutationId": "1" * 32, "projectV2": board}}
        observed = dict(board)
        if change == "changed_readback":
            observed["id"] = "PVT_OTHER"
        return {
            "viewer": {"id": "U_1", "login": "octocat"},
            "repository": {"id": "R_1", "databaseId": 77, "nameWithOwner": "team/product", "isPrivate": True},
            "node": observed,
        }

    client.execute = execute
    repository = {"provider": "github", "url": "https://github.com/team/product", "remote_repository_id": "77"}
    owner = {"viewer": {"id": "U_1", "login": "octocat"}, "owner_id": "U_1", "repository_id": "R_1"}
    if change is None:
        result = await method({}, actor, body, repository, owner)
        assert result["private"] is True and len(calls) == 2
    else:
        with pytest.raises(ValueError):
            await method({}, actor, body, repository, owner)


async def test_create_owner_is_the_current_oauth_viewer_not_caller_selected_node():
    _, actor, body, _, _, _ = creation_fixture()
    client = GitHubProjectsGraphQL(object())
    method = getattr(client, "create_owner", None)
    assert callable(method)

    async def execute(*args):
        return {
            "viewer": {"id": "FOREIGN", "login": "another-user"},
            "repository": {"id": "R_1", "databaseId": 77, "nameWithOwner": "team/product", "isPrivate": True},
        }

    client.execute = execute
    with pytest.raises(ValueError):
        await method(
            {},
            actor,
            body,
            {"provider": "github", "url": "https://github.com/team/product", "remote_repository_id": "77"},
        )
