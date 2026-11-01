# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""GitLab Notes identities come from the admitted thread, never response links."""

import copy
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from test_scm_delegated_feedback import RECEIPT, received
from test_scm_delegated_feedback import delegated_feedback as delegated_feedback

from firefly_forge.collaboration import Collaboration

REPOSITORY = dict(
    connection_provider="gitlab",
    remote_repository_id="42",
    name="team/nested/product",
    url="https://gitlab.selfmanaged.example:8443/team/nested/product.git",
)
API = "https://gitlab.selfmanaged.example:8443/api/v4"


def note(kind="issue", **changes):
    return dict(
        id=91,
        body="Confirmed action",
        project_id=42,
        noteable_type="Issue" if kind == "issue" else "MergeRequest",
        noteable_id=999,
        noteable_iid=3,
        author={"id": 123, "username": "service-bot"},
        **changes,
    )


async def publish(transport, *, repo=None, issue=None, api=API, **callbacks):
    async def authentication(*args, **kwargs):
        return "fixture-only-provider-token", api

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service = Collaboration.__new__(Collaboration)
        service.store = SimpleNamespace(execution_ownership_acquired=False, reject_credentials=lambda _: None)
        service.deployments = None
        service.scm = SimpleNamespace(issue_authentication=authentication, _request=client.request)
        return await service._remote_write(
            repo or copy.deepcopy(REPOSITORY),
            {"body": "Confirmed action"},
            issue or dict(kind="issue", number=3),
            **callbacks,
        )


@pytest.mark.parametrize("kind,path", [("issue", "issues"), ("merge_request", "merge_requests")])
async def test_real_notes_response_uses_registered_self_hosted_thread(kind, path):
    calls = []

    def transport(request):
        calls.append((request.method, str(request.url)))
        result = note(kind)
        result["web_url"] = "https://untrusted.example/foreign-note"
        return httpx.Response(201 if request.method == "POST" else 200, json=result)

    remote = await publish(transport, issue=dict(kind=kind, number=3))
    assert calls == [("POST", API + f"/projects/42/{path}/3/notes"), ("GET", API + f"/projects/42/{path}/3/notes/91")]
    assert remote == dict(
        id=91,
        number=91,
        url=f"https://gitlab.selfmanaged.example:8443/team/nested/product/-/{path}/3#note_91",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", True),
        ("id", 0),
        ("id", "091"),
        ("id", 1.5),
        ("project_id", 43),
        ("project_id", True),
        ("noteable_iid", 4),
        ("noteable_iid", "03"),
        ("noteable_type", "MergeRequest"),
        ("noteable_id", False),
    ],
)
async def test_wrong_or_noncanonical_post_identity_cannot_confirm_note(field, value):
    calls = []

    def transport(request):
        calls.append(request.method)
        result = note()
        result[field] = value
        return httpx.Response(201, json=result)

    with pytest.raises(ValueError):
        await publish(transport)
    assert calls == ["POST"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://foreign.example/team/nested/product.git"),
        ("url", "https://gitlab.selfmanaged.example:8443/other/product.git"),
        ("url", "https://gitlab.selfmanaged.example:8443/team/nested/product.git?x=1"),
        ("remote_repository_id", "042"),
        ("remote_repository_id", True),
    ],
)
async def test_registered_repository_must_match_api_and_canonical_project(field, value):
    calls = []
    repo = {**REPOSITORY, field: value}
    with pytest.raises(ValueError):
        await publish(lambda request: calls.append(request), repo=repo)
    assert calls == []


@pytest.mark.parametrize("failure", ["timeout", "wrong_id", "wrong_thread", "wrong_type", "wrong_noteable", "deferred"])
async def test_uncertain_readback_retains_known_post_identity_without_reposting(failure):
    from firefly_forge.scm_provider_backoff import ProviderRequestDeferred

    calls = []

    def transport(request):
        calls.append(request.method)
        result = note()
        if request.method == "GET":
            if failure == "timeout":
                raise httpx.ReadTimeout("Readback unavailable", request=request)
            if failure == "deferred":
                raise ProviderRequestDeferred("Readback deferred", retry_at=10)
            field, value = {
                "wrong_id": ("id", 92),
                "wrong_thread": ("noteable_iid", 4),
                "wrong_type": ("noteable_type", "MergeRequest"),
                "wrong_noteable": ("noteable_id", 1000),
            }[failure]
            result[field] = value
        return httpx.Response(201 if request.method == "POST" else 200, json=result)

    with pytest.raises(ValueError) as raised:
        await publish(transport)
    assert raised.value.remote == dict(
        id=91,
        number=91,
        url="https://gitlab.selfmanaged.example:8443/team/nested/product/-/issues/3#note_91",
    )
    assert calls == ["POST", "GET"]


@pytest.mark.parametrize(
    "api",
    [
        "https://gitlab.selfmanaged.example:8443/gitlab/api/v4",
        "https://gitlab.selfmanaged.example:8443/api/v4?foreign=1",
        "https://user:secret@gitlab.selfmanaged.example:8443/api/v4",
    ],
)
async def test_unconfigured_relative_roots_and_credential_urls_are_refused_before_post(api):
    calls = []
    with pytest.raises(ValueError):
        await publish(lambda request: calls.append(request), api=api)
    assert calls == []


@pytest.mark.parametrize("failure", ["timeout", "deferred", "cancelled"])
async def test_receipt_retains_confirmed_note_before_uncertain_readback(delegated_feedback, monkeypatch, failure):
    import asyncio

    from firefly_forge.scm_provider_backoff import ProviderRequestDeferred

    service, row, _, _, _ = delegated_feedback
    calls = []

    def transport(request):
        calls.append(request.method)
        if request.method == "GET":
            assert row.data["feedback_status"] == "writing"
            assert row.data["feedback_remote"]["id"] == 91
            assert row.data["feedback_remote"]["url"].endswith("/issues/3#note_91")
            if failure == "cancelled":
                raise asyncio.CancelledError()
            if failure == "deferred":
                raise ProviderRequestDeferred("Readback deferred", retry_at=10)
            raise httpx.ReadTimeout("Readback unavailable", request=request)
        return httpx.Response(201, json=note())

    async def remote(repo, payload, issue, **callbacks):
        return await publish(transport, **callbacks)

    monkeypatch.setattr(service, "_remote_write", remote)
    received(row, "status")
    row.provider = row.data["provider"] = "gitlab"
    repository = await service.store.repository_records.find_by_id(UUID(row.data["repository_id"]))
    repository.data.update(REPOSITORY)
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await service.process(RECEIPT)
        await service.sync_feedback()
    else:
        await service.process(RECEIPT)
    assert row.data["feedback_status"] == "needs_reconciliation"
    assert row.data["feedback_remote"]["id"] == 91
    await service.process(RECEIPT)
    await service.sync_feedback()
    assert calls == ["POST", "GET"]
    assert row.data.get("feedback_deferred") is None
