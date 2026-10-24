# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit new recovery requests confirm desired state without replaying old tickets."""

import copy
import json
from contextlib import suppress

import pytest
from test_git_delivery_lifecycle import remote_case as remote_case
from test_git_delivery_local import delivery_case as delivery_case
from test_git_delivery_local import git
from test_git_delivery_operational import operational
from test_git_delivery_remote_uow_host import strict_remote as strict_remote
from test_git_failed_unit_adoption import failed_delivery, request_from

from firefly_forge import scm
from firefly_forge.git_delivery_models import GitDeliveryInputV1
from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess

RUN, OLD, NEW = (c * 32 for c in "1ab")
ACTOR = {"user_id": "4" * 32}


def test_historical_input_digest_and_new_consent_are_strict():
    old = {"version": 1, "request_id": OLD, "expected_head": "d" * 40, "expected_source_tree": "e" * 40}
    assert GitDeliveryInputV1.model_validate(old).model_dump(mode="json") == old
    reviewed = {
        **old,
        "request_id": NEW,
        "explicit_reconciliation": {
            "version": 1,
            "original_request_id": OLD,
            "checkpoint_digest": "a" * 64,
            "remote_intent_digest": "b" * 64,
            "observation_digest": "c" * 64,
            "valid_for_seconds": 300,
            "acknowledge_original_outcome_unknown": True,
            "acknowledge_retained_body_only": True,
            "acknowledge_no_execution_grant": True,
        },
    }
    assert GitDeliveryInputV1.model_validate(reviewed).model_dump(mode="json") == reviewed
    for field in [
        "acknowledge_original_outcome_unknown",
        "acknowledge_retained_body_only",
        "acknowledge_no_execution_grant",
    ]:
        for invalid in [False, 1, "true"]:
            value = copy.deepcopy(reviewed)
            value["explicit_reconciliation"][field] = invalid
            with pytest.raises(ValueError):
                GitDeliveryInputV1.model_validate(value)
    for change in [{"request_id": OLD}, {"expected_source_tree": None}, {"expected_source_digest": "f" * 64}]:
        with pytest.raises(ValueError):
            GitDeliveryInputV1.model_validate({**reviewed, **change})


async def interrupted(c, monkeypatch):
    push = scm.push

    async def lost(*args, **kwargs):
        raise RuntimeError("Controlled pre-push interruption")

    monkeypatch.setattr(scm, "push", lost)
    (c.root / "product.py").write_text("value = 2\n")
    with pytest.raises(RuntimeError, match="pre-push"):
        await c.service.checkpoint(RUN, request_id=OLD, expected_head=c.base, actor=ACTOR)
    monkeypatch.setattr(scm, "push", push)
    return copy.deepcopy(c.state["entries"])


async def recovery_input(c, *, actor=ACTOR):
    observed = await c.service.observations(RUN, OLD, actor=actor)
    return GitDeliveryInputV1.model_validate(
        {
            "version": 1,
            "request_id": NEW,
            "expected_head": observed["observed_head"],
            "expected_source_tree": observed["observed_tree"],
            "explicit_reconciliation": {
                **observed["reconciliation_candidate"],
                "valid_for_seconds": 300,
                "acknowledge_original_outcome_unknown": True,
                "acknowledge_retained_body_only": True,
                "acknowledge_no_execution_grant": True,
            },
        }
    )


async def test_new_explicit_recovery_pushes_retained_commit_and_body_without_rewriting_original(
    remote_case, monkeypatch
):
    c = remote_case
    original = await interrupted(c, monkeypatch)
    body = await recovery_input(c)

    async def no_redraft(*args, **kwargs):
        raise AssertionError("Reconciliation must use the original admitted AI body")

    c.service.engine.communication_writer.write = no_redraft
    from firefly_forge.git_remote_reconciliation import GitRemoteReconciliations

    result = await GitRemoteReconciliations(c.service).complete(RUN, body, ACTOR, expires_at_ms=10**15)
    assert result["remote_status"] == "draft" and result["revision"] == body.expected_head
    assert c.state["entries"][: len(original)] == original
    assert git(c.remote, "rev-parse", "refs/heads/forge/public-unit") == body.expected_head
    assert git(c.root, "rev-parse", "HEAD") == body.expected_head
    assert len(c.service._records(c.state["entries"])) == 1
    remote_intent = next(e for e in original if e["kind"] == "git_delivery_remote_intent")
    assert c.remote_state["draft"]["body"] == remote_intent["payload"]["body"]
    assert sum(call.startswith("POST") for call in c.calls) == 1
    assert (
        c.service._remote_state(c.state["entries"])["reconciliation"]["original_intent_digest"]
        == remote_intent["digest"]
    )
    assert (await c.service.view(RUN, actor=ACTOR))["items"][0]["remote_status"] == "draft"


@pytest.fixture
def queued_remote(remote_case):
    fixture = operational.__wrapped__(lambda: remote_case)
    try:
        yield next(fixture)
    finally:
        with suppress(StopIteration):
            next(fixture)


async def test_owner_queue_retains_unknown_ticket_and_completes_new_recovery_once(queued_remote, monkeypatch):
    c = queued_remote
    actor = ProjectAccess.actor_snapshot(CURRENT_PRINCIPAL.get())
    (c.root / "product.py").write_text("value = 2\n")
    _, _, tree = await c.service.engine._snapshot(c.root, c.base)
    original = GitDeliveryInputV1(version=1, request_id=OLD, expected_head=c.base, expected_source_tree=tree)
    await c.controller.request(RUN, original)
    push = scm.push

    async def lost(*args, **kwargs):
        raise RuntimeError("Controlled pre-push interruption")

    monkeypatch.setattr(scm, "push", lost)
    await c.requests.git_deliveries.execute(OLD)
    monkeypatch.setattr(scm, "push", push)
    retained = copy.deepcopy(await c.controller.ticket(RUN, OLD))
    assert retained["status"] == "needs_reconciliation"
    before = copy.deepcopy(c.state["entries"])
    recovery = await recovery_input(c, actor=actor)
    admitted = await c.controller.request(RUN, recovery)
    assert admitted["status"] == "queued" and c.calls == []
    await c.requests.git_deliveries.execute(NEW)
    confirmed = await c.controller.ticket(RUN, NEW)
    assert confirmed["status"] == "completed", confirmed
    assert await c.controller.ticket(RUN, OLD) == retained
    assert c.state["entries"][: len(before)] == before
    assert confirmed["result"]["revision"] == recovery.expected_head
    assert confirmed["result"]["source_tree"] == tree
    calls = list(c.calls)
    await c.requests.git_deliveries.execute(NEW)
    assert c.calls == calls


async def test_recovery_external_io_remains_outside_writer_units(strict_remote, monkeypatch):
    from firefly_forge.git_remote_reconciliation import GitRemoteReconciliations

    c = strict_remote
    await interrupted(c, monkeypatch)
    body = await recovery_input(c)
    result = await GitRemoteReconciliations(c.service).complete(RUN, body, ACTOR, expires_at_ms=10**15)
    assert result["remote_status"] == "draft" and c.observed


@pytest.mark.parametrize("drift", ["permission", "lease", "source", "expired", "head", "binding"])
async def test_recovery_refuses_drift_before_any_new_remote_effect(remote_case, monkeypatch, drift):
    from firefly_forge.git_remote_reconciliation import GitRemoteReconciliations

    c = remote_case
    await interrupted(c, monkeypatch)
    body = await recovery_input(c)
    if drift == "permission":
        c.state["allowed"] = False
    elif drift == "lease":
        c.state["owned"] = False
    elif drift == "source":
        (c.root / "product.py").write_text("value = 99\n")
    elif drift == "head":
        git(c.root, "commit", "--allow-empty", "-qm", "Another local revision")
    elif drift == "binding":
        c.state["run"]["branch"] = "forge/another-unit"
    with pytest.raises((ValueError, PermissionError, RuntimeError)):
        await GitRemoteReconciliations(c.service).complete(
            RUN, body, ACTOR, expires_at_ms=0 if drift == "expired" else 10**15
        )
    assert c.calls == [] and c.remote_state["draft"] is None
    assert not any(e["kind"] == "git_delivery_remote_receipt" for e in c.state["entries"])


async def test_unchanged_exact_remote_state_is_confirmed_without_push_or_duplicate_pr(remote_case, monkeypatch):
    from firefly_forge.git_remote_reconciliation import GitRemoteReconciliations

    c = remote_case
    await interrupted(c, monkeypatch)
    body = await recovery_input(c)
    original = next(e["payload"] for e in c.state["entries"] if e["kind"] == "git_delivery_remote_intent")
    git(c.root, "push", str(c.remote), body.expected_head + ":refs/heads/forge/public-unit")
    c.remote_state["draft"] = {"title": original["title"], "body": original["body"], "draft": True}
    body = await recovery_input(c)
    result = await GitRemoteReconciliations(c.service).complete(RUN, body, ACTOR, expires_at_ms=10**15)
    assert result["remote_status"] == "draft"
    assert "push" not in c.calls and not any(call.startswith(("POST", "PATCH")) for call in c.calls)


@pytest.mark.parametrize("conflict", ["branch", "title", "body", "duplicate"])
async def test_foreign_remote_state_is_never_overwritten(remote_case, monkeypatch, conflict):
    from firefly_forge.git_remote_reconciliation import GitRemoteReconciliations

    c = remote_case
    await interrupted(c, monkeypatch)
    body = await recovery_input(c)
    original = next(e["payload"] for e in c.state["entries"] if e["kind"] == "git_delivery_remote_intent")
    git(
        c.root,
        "push",
        str(c.remote),
        (c.base if conflict == "branch" else body.expected_head) + ":refs/heads/forge/public-unit",
    )
    if conflict != "branch":
        c.remote_state["draft"] = {"title": original["title"], "body": original["body"], "draft": True}
        if conflict in {"title", "body"}:
            c.remote_state["draft"][conflict] = "Foreign content"
        else:
            client = c.service.http_client

            async def duplicate(response):
                await response.aread()
                value = response.json()
                if isinstance(value, list) and value:
                    response._content = json.dumps(value * 2).encode()

            c.service.http_client = lambda **kwargs: client(event_hooks={"response": [duplicate]}, **kwargs)
    with pytest.raises(ValueError):
        await GitRemoteReconciliations(c.service).complete(RUN, body, ACTOR, expires_at_ms=10**15)
    assert "push" not in c.calls and not any(call.startswith(("POST", "PATCH")) for call in c.calls)


async def test_failed_recovery_is_held_and_requires_new_reviewed_request(queued_remote, monkeypatch):
    c = queued_remote
    actor = ProjectAccess.actor_snapshot(CURRENT_PRINCIPAL.get())
    (c.root / "product.py").write_text("value = 2\n")
    _, _, tree = await c.service.engine._snapshot(c.root, c.base)
    await c.controller.request(
        RUN, GitDeliveryInputV1(version=1, request_id=OLD, expected_head=c.base, expected_source_tree=tree)
    )
    push = scm.push

    async def lost(*args, **kwargs):
        raise RuntimeError("Controlled transport loss")

    monkeypatch.setattr(scm, "push", lost)
    await c.requests.git_deliveries.execute(OLD)
    body = await recovery_input(c, actor=actor)
    await c.controller.request(RUN, body)
    await c.requests.git_deliveries.execute(NEW)
    held = copy.deepcopy(await c.controller.ticket(RUN, NEW))
    assert held["status"] == "needs_reconciliation"
    assert (await c.controller.request(RUN, body)) == held
    monkeypatch.setattr(scm, "push", push)
    fresh = (await recovery_input(c, actor=actor)).model_copy(update={"request_id": "c" * 32})
    await c.controller.request(RUN, fresh)
    await c.requests.git_deliveries.execute(fresh.request_id)
    assert (await c.controller.ticket(RUN, fresh.request_id))["status"] == "completed"
    assert await c.controller.ticket(RUN, NEW) == held
    assert sum(call.startswith("POST") for call in c.calls) == 1
    assert c.service._remote_state(c.state["entries"])["remote_status"] == "draft"


async def test_new_remote_consent_does_not_extend_expired_failed_adoption(queued_remote, monkeypatch):
    from firefly_forge.git_failed_unit_adoption import effective_policy

    c = failed_delivery.__wrapped__(queued_remote, monkeypatch)
    c.project_policy["policy"]["remote"] = "draft_pr"
    candidate = await c.controller.adoption_candidate(RUN)
    original = request_from(candidate)
    push = scm.push

    async def lost(*args, **kwargs):
        raise RuntimeError("Controlled pre-push interruption")

    monkeypatch.setattr(scm, "push", lost)
    await c.controller.request(RUN, original)
    await c.requests.git_deliveries.execute(OLD)
    assert (await c.controller.ticket(RUN, OLD))["status"] == "needs_reconciliation"
    adoption = copy.deepcopy(c.state["run"]["git_failed_unit_adoption"])
    event = next(e for e in c.state["entries"] if e["kind"] == "git_failed_unit_adoption")
    monkeypatch.setattr(
        "firefly_forge.git_failed_unit_adoption.clock_ms", lambda: event["payload"]["expires_at_ms"] + 1
    )
    actor = ProjectAccess.actor_snapshot(CURRENT_PRINCIPAL.get())
    with pytest.raises(Exception, match="finite consent"):
        await effective_policy(c.service, c.state["run"], actor=actor, writing=True)
    body = await recovery_input(c, actor=actor)
    monkeypatch.setattr(scm, "push", push)
    await c.controller.request(RUN, body)
    await c.requests.git_deliveries.execute(NEW)
    assert (await c.controller.ticket(RUN, NEW))["status"] == "completed"
    assert c.state["run"]["git_failed_unit_adoption"] == adoption
    assert c.state["run"]["status"] == "failed" and c.state["run"]["git_delivery_policy"] is None
    assert c.task["status"] == "running" and c.task["attempt"] == 6


async def test_sdk_cli_and_mcp_submit_exact_typed_recovery_once(remote_case, monkeypatch, tmp_path):
    import httpx
    from click.testing import CliRunner
    from test_original_recovery_sdk import connection

    from firefly_forge import cli
    from firefly_forge.mcp_server import create_server

    c = remote_case
    await interrupted(c, monkeypatch)
    body = (await recovery_input(c)).model_dump(mode="json")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(202, json={"status": "queued"})

    with connection(respond) as client:
        assert client.request_git_delivery(RUN, body)["status"] == "queued"
    monkeypatch.setattr(cli, "client", lambda: connection(respond))
    document = tmp_path / "reviewed-recovery.json"
    document.write_text(json.dumps(body))
    result = CliRunner().invoke(cli.main, ["git-deliveries", "request", RUN, str(document)])
    assert result.exit_code == 0, result.output
    response = await create_server().call_tool("request_git_delivery", {"run_id": RUN, "request": body})
    assert response
    assert len(calls) == 3
    assert all(
        request.method == "POST"
        and request.url.path == f"/api/v1/runs/{RUN}/git-deliveries"
        and json.loads(request.content) == body
        for request in calls
    )


async def test_finite_consent_is_rechecked_after_synchronous_source_hash(remote_case, monkeypatch):
    from firefly_forge import verification_models
    from firefly_forge.git_remote_reconciliation import GitRemoteReconciliations

    c = remote_case
    await interrupted(c, monkeypatch)
    body = await recovery_input(c)
    service = GitRemoteReconciliations(c.service)
    prepared = await service.prepare(RUN, body, ACTOR)
    clock = {"now": 0}
    source = verification_models.verification_source

    def slow_hash(*args, **kwargs):
        value = source(*args, **kwargs)
        clock["now"] = 2000
        return value

    monkeypatch.setattr("firefly_forge.git_failed_unit_adoption.clock_ms", lambda: clock["now"])
    monkeypatch.setattr(verification_models, "verification_source", slow_hash)
    with pytest.raises(ValueError, match="finite consent"):
        await service.guard(prepared, ACTOR, 1000)
    assert c.calls == []


@pytest.mark.parametrize("drift", ["authority", "lease", "source"])
async def test_post_effect_drift_keeps_recovery_intent_unknown_without_confirmation(strict_remote, monkeypatch, drift):
    from firefly_forge.git_remote_reconciliation import GitRemoteReconciliations

    c = strict_remote
    await interrupted(c, monkeypatch)
    body = await recovery_input(c)

    async def changed(response):
        if response.request.method == "POST":
            if drift == "authority":
                c.state["allowed"] = False
            elif drift == "lease":
                c.state["owned"] = False
            else:
                (c.root / "product.py").write_text("value = 99\n")

    c.after_response = changed
    with pytest.raises((ValueError, RuntimeError, PermissionError)):
        await GitRemoteReconciliations(c.service).complete(RUN, body, ACTOR, expires_at_ms=10**15)
    assert c.remote_state["draft"] is not None
    assert sum(e["kind"] == "git_delivery_remote_intent" for e in c.state["entries"]) == 2
    assert not any(e["kind"] == "git_delivery_remote_receipt" for e in c.state["entries"])
