# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Provider draft capabilities and exact reference identity use controlled HTTP only."""

import hashlib
import importlib

import httpx
import pytest


def repository(provider="github"):
    return {"provider": provider, "url": "https://example.test/team/product.git", "default_branch": "main"}


def github(sha="a" * 40, **changes):
    return {
        "number": 7,
        "node_id": "PR_public_fixture",
        "html_url": "https://example.test/team/product/pull/7",
        "state": "open",
        "draft": True,
        "head": {"ref": "forge/unit", "sha": sha, "repo": {"full_name": "team/product"}},
        "base": {"ref": "main", "repo": {"full_name": "team/product"}},
        **changes,
    }


async def test_github_exact_draft_is_updated_then_same_pr_becomes_ready():
    calls = []
    ready = False

    async def respond(request):
        nonlocal ready
        calls.append((request.method, request.url.path, request.content))
        if request.url.path == "/api/graphql":
            ready = True
            return httpx.Response(
                200,
                json={
                    "data": {
                        "markPullRequestReadyForReview": {"pullRequest": {"id": "PR_public_fixture", "isDraft": False}}
                    }
                },
            )
        return httpx.Response(200, json=github(draft=not ready))

    module = importlib.import_module("firefly_forge.git_delivery_remote")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        remote = module.DraftPullRequests(client, repository(), ("private-fixture", "https://example.test/api/v3"))
        selected = await remote.read("https://example.test/team/product/pull/7", "forge/unit", "a" * 40)
        await remote.update(selected, "forge/unit", "a" * 40, "Public unit", "Verification is pending.")
        await remote.ready(selected, "forge/unit", "a" * 40)
    assert sum(method == "POST" for method, _, _ in calls) == 1
    assert all(b"private-fixture" not in body for _, _, body in calls)
    assert any(path == "/api/graphql" for _, path, _ in calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"draft": False},
        {"state": "closed"},
        {"head": {"ref": "foreign", "sha": "a" * 40, "repo": {"full_name": "team/product"}}},
        {"number": True},
    ],
)
async def test_wrong_or_nondraft_reference_refuses_before_any_write(changes):
    module = importlib.import_module("firefly_forge.git_delivery_remote")
    calls = []

    async def respond(request):
        calls.append(request.method)
        return httpx.Response(200, json=github(**changes))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        remote = module.DraftPullRequests(client, repository(), ("fixture", "https://example.test/api/v3"))
        with pytest.raises(ValueError):
            await remote.read("https://example.test/team/product/pull/7", "forge/unit", "a" * 40)
    assert calls == ["GET"]


async def test_duplicate_remote_matches_are_not_an_idempotency_receipt():
    module = importlib.import_module("firefly_forge.git_delivery_remote")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[github(), github()]))
    ) as client:
        remote = module.DraftPullRequests(client, repository(), ("fixture", "https://example.test/api/v3"))
        with pytest.raises(ValueError, match="multiple|ambiguous"):
            await remote.find("forge/unit", "a" * 40)


@pytest.mark.parametrize("provider", ["git", "bitbucket", "gitea", "azure"])
def test_unimplemented_draft_lifecycle_refuses_before_network(provider):
    module = importlib.import_module("firefly_forge.git_delivery_remote")
    assert not module.draft_capability(provider)
    with pytest.raises(ValueError, match="draft"):
        module.DraftPullRequests(None, repository(provider), ("fixture", "https://example.test/api"))


async def test_gitlab_uses_draft_title_and_validates_current_source_and_target():
    module = importlib.import_module("firefly_forge.git_delivery_remote")
    calls = []

    async def respond(request):
        calls.append(request)
        return httpx.Response(
            201,
            json={
                "iid": 7,
                "web_url": "https://example.test/team/product/-/merge_requests/7",
                "state": "opened",
                "draft": True,
                "source_branch": "forge/unit",
                "target_branch": "main",
                "source_project_id": 23,
                "target_project_id": 23,
                "sha": "a" * 40,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        remote = module.DraftPullRequests(client, repository("gitlab"), ("fixture", "https://example.test/api/v4"))
        result = await remote.create("forge/unit", "a" * 40, "Public unit", "Verification is pending.")
    assert result["url"].endswith("/7") and b'"Draft: Public unit"' in calls[0].content


async def test_requested_pr_identity_cannot_be_substituted_by_valid_other_pr_metadata():
    module = importlib.import_module("firefly_forge.git_delivery_remote")
    calls = []

    async def respond(request):
        calls.append(request.method)
        return httpx.Response(200, json=github(number=8, html_url="https://example.test/team/product/pull/8"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        remote = module.DraftPullRequests(client, repository(), ("fixture", "https://example.test/api/v3"))
        with pytest.raises(ValueError, match="identity|number|reference"):
            await remote.ready(
                {
                    "url": "https://example.test/team/product/pull/7",
                    "number": 7,
                    "node_id": "PR_original",
                    "body_sha256": None,
                },
                "forge/unit",
                "a" * 40,
            )
    assert calls == ["GET"]


async def test_retained_node_identity_cannot_change_before_ready():
    module = importlib.import_module("firefly_forge.git_delivery_remote")
    calls = []

    async def respond(request):
        calls.append(request.method)
        return httpx.Response(200, json=github(node_id="PR_other"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        remote = module.DraftPullRequests(client, repository(), ("fixture", "https://example.test/api/v3"))
        with pytest.raises(ValueError, match="identity|reference"):
            await remote.ready(
                {
                    "url": "https://example.test/team/product/pull/7",
                    "number": 7,
                    "node_id": "PR_original",
                    "body_sha256": None,
                },
                "forge/unit",
                "a" * 40,
            )
    assert calls == ["GET"]


@pytest.mark.parametrize("provider,drift", [("github", "number"), ("github", "node"), ("gitlab", "number")])
async def test_update_response_cannot_substitute_another_retained_reference(provider, drift):
    module = importlib.import_module("firefly_forge.git_delivery_remote")
    calls = []

    def record(number=7, node_id="PR_public_fixture"):
        if provider == "github":
            return github(
                number=number,
                node_id=node_id,
                html_url=f"https://example.test/team/product/pull/{number}",
                body="Verification remains pending.",
            )
        return {
            "iid": number,
            "web_url": f"https://example.test/team/product/-/merge_requests/{number}",
            "state": "opened",
            "draft": True,
            "source_branch": "forge/unit",
            "target_branch": "main",
            "source_project_id": 23,
            "target_project_id": 23,
            "sha": "a" * 40,
            "description": "Verification remains pending.",
        }

    async def respond(request):
        calls.append(request.method)
        changed = request.method != "GET"
        return httpx.Response(
            200,
            json=record(
                8 if changed and drift == "number" else 7,
                "PR_substituted" if changed and drift == "node" else "PR_public_fixture",
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        remote = module.DraftPullRequests(client, repository(provider), ("fixture", "https://example.test/api/v3"))
        url = "https://example.test/team/product/" + ("pull/7" if provider == "github" else "-/merge_requests/7")
        selected = await remote.read(url, "forge/unit", "a" * 40)
        with pytest.raises(ValueError, match="identity"):
            await remote.update(selected, "forge/unit", "a" * 40, "Public unit", "Verification remains pending.")
    assert calls == ["GET", "GET", "PATCH" if provider == "github" else "PUT"]


@pytest.mark.parametrize("drift", ["body", "identity"])
async def test_gitlab_second_ready_read_refuses_changed_body_or_identity_before_PUT(drift):
    module = importlib.import_module("firefly_forge.git_delivery_remote")
    calls = []

    def record(*, draft=True, changed=False):
        number = 8 if changed and drift == "identity" else 7
        return {
            "iid": number,
            "web_url": f"https://example.test/team/product/-/merge_requests/{number}",
            "state": "opened",
            "draft": draft,
            "source_branch": "forge/unit",
            "target_branch": "main",
            "source_project_id": 23,
            "target_project_id": 23,
            "sha": "a" * 40,
            "description": "Unreviewed replacement" if changed and drift == "body" else "Approved body",
            "title": "Draft: Public unit",
        }

    async def respond(request):
        calls.append(request.method)
        return httpx.Response(200, json=record(draft=len(calls) <= 2, changed=len(calls) > 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        remote = module.DraftPullRequests(client, repository("gitlab"), ("fixture", "https://example.test/api/v4"))
        reference = {
            "url": "https://example.test/team/product/-/merge_requests/7",
            "number": 7,
            "node_id": None,
            "revision": "a" * 40,
            "body_sha256": hashlib.sha256(b"Approved body").hexdigest(),
        }
        with pytest.raises(ValueError, match="identity|content"):
            await remote.ready(reference, "forge/unit", "a" * 40)
    assert calls == ["GET", "GET"]


@pytest.mark.parametrize(
    "provider,boundary",
    [("gitlab", "put_identity"), ("gitlab", "put_body"), ("github", "final_identity"), ("gitlab", "final_identity")],
)
async def test_ready_post_write_substitution_is_unconfirmed_not_a_success_receipt(provider, boundary):
    module = importlib.import_module("firefly_forge.git_delivery_remote")
    calls = []
    promoted = False

    def gitlab(*, changed=False):
        number = 8 if changed and boundary in {"put_identity", "final_identity"} else 7
        return {
            "iid": number,
            "web_url": f"https://example.test/team/product/-/merge_requests/{number}",
            "state": "opened",
            "draft": not promoted,
            "source_branch": "forge/unit",
            "target_branch": "main",
            "source_project_id": 23,
            "target_project_id": 23,
            "sha": "a" * 40,
            "description": "Unreviewed replacement" if changed and boundary == "put_body" else "Approved body",
            "title": "Draft: Public unit",
        }

    async def respond(request):
        nonlocal promoted
        calls.append(request.method)
        if provider == "github":
            if request.method == "POST":
                promoted = True
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "markPullRequestReadyForReview": {
                                "pullRequest": {"id": "PR_public_fixture", "isDraft": False}
                            }
                        }
                    },
                )
            return httpx.Response(
                200,
                json=github(
                    draft=not promoted, body="Approved body", node_id="PR_foreign" if promoted else "PR_public_fixture"
                ),
            )
        if request.method == "PUT":
            promoted = True
        changed = request.method == "PUT" if boundary.startswith("put_") else request.method == "GET" and promoted
        return httpx.Response(200, json=gitlab(changed=changed))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        remote = module.DraftPullRequests(
            client,
            repository(provider),
            ("fixture", "https://example.test/api/v3" if provider == "github" else "https://example.test/api/v4"),
        )
        reference = {
            "url": "https://example.test/team/product/" + ("pull/7" if provider == "github" else "-/merge_requests/7"),
            "number": 7,
            "node_id": "PR_public_fixture" if provider == "github" else None,
            "revision": "a" * 40,
            "body_sha256": hashlib.sha256(b"Approved body").hexdigest(),
        }
        with pytest.raises(ValueError, match="identity|content|reference"):
            await remote.ready(reference, "forge/unit", "a" * 40)
    assert calls == (
        ["GET", "GET", "PUT"]
        if boundary.startswith("put_")
        else ["GET", "POST", "GET"]
        if provider == "github"
        else ["GET", "GET", "PUT", "GET"]
    )


async def test_gitlab_same_reference_and_body_can_become_ready():
    module = importlib.import_module("firefly_forge.git_delivery_remote")
    calls = []
    promoted = False

    async def respond(request):
        nonlocal promoted
        calls.append(request.method)
        if request.method == "PUT":
            promoted = True
        return httpx.Response(
            200,
            json={
                "iid": 7,
                "web_url": "https://example.test/team/product/-/merge_requests/7",
                "state": "opened",
                "draft": not promoted,
                "source_branch": "forge/unit",
                "target_branch": "main",
                "source_project_id": 23,
                "target_project_id": 23,
                "sha": "a" * 40,
                "description": "Approved body",
                "title": "Draft: Public unit",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        remote = module.DraftPullRequests(client, repository("gitlab"), ("fixture", "https://example.test/api/v4"))
        reference = {
            "url": "https://example.test/team/product/-/merge_requests/7",
            "number": 7,
            "node_id": None,
            "revision": "a" * 40,
            "body_sha256": hashlib.sha256(b"Approved body").hexdigest(),
        }
        assert await remote.ready(reference, "forge/unit", "a" * 40) == reference
    assert calls == ["GET", "GET", "PUT", "GET"]
