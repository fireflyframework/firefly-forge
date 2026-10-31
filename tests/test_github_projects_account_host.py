# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read-only account proof precedes the visual private-board confirmation."""

from types import SimpleNamespace

import pytest

from firefly_forge import github_projects_models as contracts
from firefly_forge.github_projects import GitHubProjects
from firefly_forge.github_projects_graphql import CREATION_OWNER, GitHubProjectsGraphQL
from firefly_forge.project_access import AccessDenied


@pytest.mark.parametrize("change", [None, "repository", "private_scope", "viewer"])
async def test_account_proof_checks_current_viewer_repository_and_private_scope(change):
    remote = GitHubProjectsGraphQL(object())
    method = getattr(remote, "account", None)
    assert callable(method), "The wizard requires an independently read current account proof"
    calls = []

    async def execute(binding, actor, query, variables):
        calls.append((query, variables))
        return {
            "viewer": {"id": "U_1", "login": "octocat" if change != "viewer" else "invalid/login"},
            "repository": {
                "id": "R_1",
                "databaseId": 77 if change != "repository" else 88,
                "nameWithOwner": "team/product",
                "isPrivate": True,
            },
        }

    remote.execute = execute
    binding = {"provider": "github-projects" if change == "private_scope" else "github-projects-private"}
    repository = {"provider": "github", "url": "https://github.com/team/product", "remote_repository_id": "77"}
    if change:
        with pytest.raises(ValueError):
            await method(binding, object(), repository)
    else:
        result = await method(binding, object(), repository)
        assert result["viewer"] == {"id": "U_1", "login": "octocat"}
        assert result["repository"] == {"id": "R_1", "name_with_owner": "team/product", "private": True}
    assert calls == [(CREATION_OWNER, {"repoOwner": "team", "repoName": "product"})]


@pytest.mark.parametrize("revoke", [False, True])
async def test_account_proof_rechecks_native_and_oauth_authority_after_remote_await(revoke):
    method = getattr(GitHubProjects, "account", None)
    assert callable(method), "Account proof must revalidate native and credential custody"
    manager = GitHubProjects.__new__(GitHubProjects)
    checks = []
    revoked = False

    async def authority(*args, **kwargs):
        checks.append("native")
        assert kwargs == {"configure": True}
        if revoked:
            raise AccessDenied("Revoked during account read")

    async def bind(*args, **kwargs):
        return {"provider": "github-projects-private"}

    async def admission(*args, **kwargs):
        checks.append("oauth")

    async def account(*args):
        nonlocal revoked
        revoked = revoke
        return {
            "viewer": {"id": "U_1", "login": "octocat"},
            "repository": {"id": "R_1", "name_with_owner": "team/product", "private": True},
        }

    async def repository(*args):
        return {}

    async def credentials(*args):
        checks.append("redaction")

    manager.authority = authority
    manager.resources = SimpleNamespace(bind=bind, _admission=admission)
    manager.remote = SimpleNamespace(account=account)
    manager.store = SimpleNamespace(repository=repository, admit_credentials=credentials)
    body = contracts.BoardAccountInputV1(version=1, connection_name="github-board")
    if revoke:
        with pytest.raises(AccessDenied):
            await method(manager, "a" * 32, "b" * 32, body, object())
    else:
        proof = await method(manager, "a" * 32, "b" * 32, body, object())
        assert proof.project_id == "a" * 32 and proof.repository_id == "b" * 32
        assert proof.connection_name == "github-board"
        assert checks == ["native", "native", "oauth", "redaction", "native"]
