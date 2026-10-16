# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host action/feedback controls; native authority and transactions are separate."""

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from firefly_forge.collaboration import Collaboration, CollaborationSettings, RepositoryCommand
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm_actor_authority import SCMActorAuthority

REPO, RUN, RECEIPT, PROJECT = (value * 32 for value in "1234")


@pytest.fixture
async def delegated_feedback(tmp_path, monkeypatch):
    """Replace unavailable DB/authority and HTTP boundaries, not feedback logic."""
    sent = []
    state = {"fail_delivery": False, "deny": False, "fail_action": False}
    run = dict(
        id=RUN,
        repository_id=REPO,
        project_id=PROJECT,
        component_id="api",
        status="paused",
        worker_request=dict(id="question-1", status="pending", question="Which result is expected?"),
    )
    repo = dict(
        id=REPO,
        name="team/product",
        url="https://github.com/team/product.git",
        connection_provider="github",
        remote_repository_id="42",
    )
    row = SimpleNamespace(status="received", provider="github", data={})
    settings = SimpleNamespace(
        data={
            **CollaborationSettings(
                enabled=True, trusted_actors=["7"], channel_commands=["respond", "cancel"]
            ).model_dump(),
            "_scm_authority_v1": {"version": 1, "heads": {"retained-head": {}}, "requests": {}},
        }
    )

    async def noop(*args, **kwargs):
        return None

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace(execute=noop)

    async def find_receipt(key, **kwargs):
        assert key.hex == RECEIPT
        return row

    async def find_repository(key, **kwargs):
        assert key.hex == REPO
        return SimpleNamespace(data=repo)

    async def find_run(key, **kwargs):
        assert key.hex == RUN
        return SimpleNamespace(data=run)

    async def find_settings(key):
        assert key.hex == REPO
        return settings

    async def save(value):
        assert value is row

    async def repository(key):
        assert key == REPO
        return copy.deepcopy(repo)

    async def get_run(key):
        assert key == RUN
        return copy.deepcopy(run)

    async def cancel(key):
        assert key == RUN
        return {"id": "5" * 32, "status": "queued"}

    async def resolve(self, delivery):
        if state["deny"]:
            raise AccessDenied("The retained mapping is revoked")
        return Principal(
            UUID("6" * 32),
            "owned-host",
            "operator",
            authentication_method="scm_mapping",
            scm_delegation=dict(project_id=PROJECT, repository_id=REPO, component_id="api", run_id=RUN),
        )

    async def authorize(self, actor, project, permission):
        assert project == PROJECT and permission in {"read", "execute"}
        assert CURRENT_PRINCIPAL.get().id.hex == actor["user_id"]
        return True

    async def identity(self, key, **kwargs):
        assert key.hex == "6" * 32
        return SimpleNamespace(enabled=True)

    async def answer(self, run_id, request_id, response):
        assert run_id == RUN and request_id == "question-1" and response.id == RECEIPT
        if state["fail_action"]:
            raise RuntimeError("Action delivery outcome is unknown")
        run["worker_request"]["status"] = "answered"

    monkeypatch.setattr(SCMActorAuthority, "resolve", resolve)
    monkeypatch.setattr(ProjectAccess, "authorize_actor", authorize)
    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    monkeypatch.setattr(Journal, "worker_response", answer)
    store = SimpleNamespace(
        schema="owned_host_feedback",
        _unit=unit,
        repository=repository,
        get_run=get_run,
        admit_credentials=noop,
        reject_credentials=lambda _: None,
        repository_records=SimpleNamespace(find_by_id=find_repository),
        run_records=SimpleNamespace(find_by_id=find_run),
        execution_requests=SimpleNamespace(request_cancel=cancel),
        configuration=SimpleNamespace(directory=tmp_path, _read=lambda _: {}),
    )

    def transport(request):
        assert CURRENT_PRINCIPAL.get() is None, "Close delegation context before outbound I/O"
        assert request.method == "POST" and request.url == "https://api.github.com/repos/team/product/issues/3/comments"
        assert row.data["status"] == "processed" and row.data["action_status"] == "completed"
        assert row.data["feedback_status"] == "writing", "Persist intent before provider I/O"
        sent.append(json.loads(request.content))
        if state["fail_delivery"]:
            raise httpx.ReadTimeout("Response lost after receiving the comment", request=request)
        return httpx.Response(
            201, json={"id": 91, "html_url": "https://github.com/team/product/issues/3#issuecomment-91"}
        )

    async def pending():
        return [row]

    async def authentication(*args, **kwargs):
        return "host-transport-fixture-only", "https://api.github.com"

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        scm = SimpleNamespace(
            issue_authentication=authentication, _request=client.request, redaction_settings=lambda: {}
        )
        service = Collaboration(
            store,
            scm,
            settings_records=SimpleNamespace(find_for_repository=find_settings),
            inbox=SimpleNamespace(find_by_id=find_receipt, find_pending=pending, save=save),
        )
        yield service, row, run, sent, state


def received(row, action):
    row.status = "received"
    options = {"run_id": RUN}
    if action == "respond":
        options.update(request_id="question-1", answer="Empty is expected")
    command = RepositoryCommand.parse(f"/forge {action} " + json.dumps(options)).model_dump(exclude_none=True)
    row.data = dict(
        id=RECEIPT,
        status="received",
        repository_id=REPO,
        provider="github",
        actor="7",
        remote_id="42",
        issue=dict(kind="issue", number=3),
        feedback_status="not_requested",
        command=command,
    )


@pytest.mark.parametrize("action", ["respond", "status", "cancel"])
async def test_completed_delegated_action_posts_retained_result_once(delegated_feedback, action):
    service, row, run, sent, _ = delegated_feedback
    received(row, action)
    result = await service.process(RECEIPT)
    assert result["status"] == "processed" and result["action_status"] == "completed"
    assert result["feedback_status"] == "sent"
    assert len(sent) == 1 and f"Forge {action}:" in sent[0]["body"] and RECEIPT in sent[0]["body"]
    assert result["feedback_remote"]["id"] == 91
    if action == "respond":
        assert run["status"] == "paused" and run["worker_request"]["status"] == "answered"
    assert await service.process(RECEIPT) == result
    assert len(sent) == 1
    await service.sync_feedback()
    assert len(sent) == 1, "An unchanged run must not generate a second action-result comment"


@pytest.mark.parametrize("action", ["respond", "status", "cancel"])
async def test_lost_delegated_feedback_is_held_without_automatic_repost(delegated_feedback, action):
    service, row, _, sent, state = delegated_feedback
    received(row, action)
    state["fail_delivery"] = True
    result = await service.process(RECEIPT)
    assert result["status"] == "processed" and result["action_status"] == "completed"
    assert result["feedback_status"] == "needs_reconciliation" and len(sent) == 1
    assert await service.process(RECEIPT) == result
    await service.sync_feedback()
    assert len(sent) == 1
    reconciled = await service.reconcile(RECEIPT, "posted", "Provider comment 91 was checked explicitly")
    assert reconciled["feedback_status"] == "sent" and reconciled["reconciliation"]["operator"] is True
    assert len(sent) == 1


async def test_denied_or_unknown_delegated_action_does_not_report_completion(delegated_feedback):
    service, row, _, sent, state = delegated_feedback
    received(row, "respond")
    state["deny"] = True
    assert (await service.process(RECEIPT))["status"] == "denied"
    assert sent == []
    received(row, "respond")
    state["deny"], state["fail_action"] = False, True
    result = await service.process(RECEIPT)
    assert result["action_status"] == "needs_reconciliation"
    assert result["feedback_status"] == "not_requested" and sent == []


async def test_run_transition_reports_current_status_without_rewriting_retained_action(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "respond")
    await service.process(RECEIPT)
    original = copy.deepcopy(row.data["action_result"])
    assert original["status"] == "paused"
    run["status"] = "failed"
    await service.sync_feedback()
    assert len(sent) == 2 and "failed" in sent[-1]["body"]
    assert "paused" in sent[-1]["body"]
    assert row.data["action_result"] == original
    await service.sync_feedback()
    assert len(sent) == 2
