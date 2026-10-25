# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual controller/owner queue and real Git; memory locks do not claim PostgreSQL proof."""

import copy
from uuid import UUID

import pytest
from test_bridge_inflight_cancel import RUN
from test_git_delivery_local import git

from firefly_forge.git_delivery_controllers import GitDeliveryController
from firefly_forge.git_delivery_models import GitDeliveryInputV1
from firefly_forge.git_delivery_requests import GitDeliveryRequests
from firefly_forge.journal import Journal
from firefly_forge.project_access import CURRENT_PRINCIPAL

pytest_plugins = ["test_git_partial_source_frontier_host"]


async def queue(env):
    controller = GitDeliveryController(env.partial.d, GitDeliveryRequests(env.partial.d, env.requests))
    candidate = await controller.partial_source_candidate(RUN)
    body = GitDeliveryInputV1.model_validate(
        dict(
            version=1,
            request_id="a" * 32,
            expected_head=candidate["expected_head"],
            expected_source_digest=candidate["source_digest"],
            explicit_partial_source={
                "version": 1,
                "candidate_digest": candidate["digest"],
                **{
                    key: candidate[key]
                    for key in [
                        "policy_digest",
                        "expected_run_digest",
                        "journal_digest",
                        "source_digest",
                        "frontier_record_digest",
                    ]
                },
                "acknowledge_partial_unverified": True,
                "acknowledge_preserve_failed_graph": True,
                "acknowledge_no_execution_grant": True,
            },
        )
    )
    receipt = await controller.partial_source_request(RUN, body)
    assert receipt["status"] == "queued" and git(env.workspace, "rev-parse", "HEAD") == candidate["expected_head"]
    return controller, body, receipt


async def test_partial_source_api_queues_once_and_worker_returns_exact_retained_receipt(partial_service):
    env = partial_service
    token = CURRENT_PRINCIPAL.set(env.principal)
    try:
        before = copy.deepcopy(await env.store.get_run(RUN))
        controller, body, admitted = await queue(env)
        await controller.tickets.execute(body.request_id)
        final = await controller.ticket(RUN, body.request_id)
        assert final["status"] == "completed", final.get("result")
        assert final["result"]["verification_status"] == "partial_unverified"
        assert await controller.partial_source_request(RUN, body) == final
        assert len(env.requests.records.rows) == 1
        count = len(env.journal.records.rows)
        head = git(env.workspace, "rev-parse", "HEAD")
        await controller.tickets.execute(body.request_id)
        assert git(env.workspace, "rev-parse", "HEAD") == head
        assert len(env.journal.records.rows) == count
        current = await env.store.get_run(RUN)
        for key in [
            "status",
            "active_task",
            "implementation_pending",
            "bridge_completion_hold",
            "implementation_rounds",
            "attempt",
        ]:
            assert current.get(key) == before.get(key)
        changed = body.model_copy(update={"expected_head": "f" * 40})
        with pytest.raises(ValueError, match="another input"):
            await controller.partial_source_request(RUN, changed)
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_partial_source_late_revocation_preserves_uncommitted_source(partial_service):
    env = partial_service
    token = CURRENT_PRINCIPAL.set(env.principal)
    try:
        controller, body, _ = await queue(env)
        head = git(env.workspace, "rev-parse", "HEAD")
        env.partial_state["allowed"] = False
        await controller.tickets.execute(body.request_id)
        row = await env.requests.records.find_by_id(UUID(body.request_id))
        assert row.status == "failed"
        assert git(env.workspace, "rev-parse", "HEAD") == head
        assert not any(r.kind == "git_delivery_intent" for r in env.journal.records.rows)
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_crash_after_real_git_commit_keeps_unknown_ticket_and_never_replays(partial_service, monkeypatch):
    env = partial_service
    token = CURRENT_PRINCIPAL.set(env.principal)
    append = Journal.git_delivery_append

    async def refuse(journal, run_id, kind, key, payload):
        if kind == "git_delivery_receipt":
            raise RuntimeError("Controlled receipt storage failure after actual Git commit")
        return await append(journal, run_id, kind, key, payload)

    try:
        controller, body, _ = await queue(env)
        head = git(env.workspace, "rev-parse", "HEAD")
        monkeypatch.setattr(Journal, "git_delivery_append", refuse)
        await controller.tickets.execute(body.request_id)
        final = await controller.ticket(RUN, body.request_id)
        assert final["status"] == "needs_reconciliation"
        after = git(env.workspace, "rev-parse", "HEAD")
        assert after != head and git(env.workspace, "rev-parse", "HEAD^") == head
        assert await controller.partial_source_request(RUN, body) == final
        await controller.tickets.execute(body.request_id)
        assert git(env.workspace, "rev-parse", "HEAD") == after
        assert sum(r.kind == "git_delivery_intent" for r in env.journal.records.rows) == 1
        assert not any(r.kind == "git_delivery_receipt" for r in env.journal.records.rows)
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_partial_owner_uses_native_custody_rows_not_aggregate_wal_projection(partial_service, monkeypatch):
    env = partial_service
    token = CURRENT_PRINCIPAL.set(env.principal)
    try:
        controller, body, _ = await queue(env)

        async def aggregate_refusal(*args, **kwargs):
            raise AssertionError("Partial Git custody must not use aggregate protected-content projection")

        monkeypatch.setattr(env.journal, "entries", aggregate_refusal)
        await controller.tickets.execute(body.request_id)
        receipt = await controller.ticket(RUN, body.request_id)
        assert receipt["status"] == "completed", receipt.get("result")
        assert receipt["result"]["verification_status"] == "partial_unverified"
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_owned_activity_projection_remains_valid_for_partial_publication(partial_service):
    env = partial_service
    token = CURRENT_PRINCIPAL.set(env.principal)
    try:
        controller, body, _ = await queue(env)
        await controller.tickets.execute(body.request_id)
        receipt = await controller.ticket(RUN, body.request_id)
        assert receipt["status"] == "completed", receipt.get("result")
        current = await env.store.get_run(RUN)
        assert current["git_delivery_summary"]["revision"] == receipt["result"]["revision"]
        await env.partial.guard(current, current["execution_actor"])
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("projection", ["summary", "draft"])
async def test_partial_source_rejects_forged_delivery_projection(partial_service, projection):
    env = partial_service
    token = CURRENT_PRINCIPAL.set(env.principal)
    try:
        controller, body, _ = await queue(env)
        await controller.tickets.execute(body.request_id)
        receipt = await controller.ticket(RUN, body.request_id)
        assert receipt["status"] == "completed"
        if projection == "summary":
            env.store.run.data["git_delivery_summary"]["revision"] = "f" * 40
        else:
            env.store.run.data["git_delivery_draft"] = {"url": "https://example.test/forged"}
        with pytest.raises(ValueError, match="projection"):
            await env.partial.guard(await env.store.get_run(RUN), env.store.run.data["execution_actor"])
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("partial_service", ["draft"], indirect=True)
async def test_partial_source_publishes_only_its_exact_unverified_draft(partial_service, tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    import httpx

    from firefly_forge import scm

    env = partial_service
    remote = env.partial_remote
    branch = env.store.run.data["branch"]
    state = {"draft": None}
    calls = []
    repository = {
        "id": env.store.run.data["repository_id"],
        "url": "https://example.test/team/product.git",
        "provider": "github",
        "connection_provider": "github",
        "default_branch": "main",
    }

    async def repo(_):
        return repository

    async def authentication(_):
        return "controlled-fixture", "https://example.test/api/v3"

    async def environment(_):
        return {}

    async def wording(run, native, safe, **kwargs):
        assert kwargs["git_delivery"].implementation_disposition == "failed"
        return {
            "body": "### Retained source checkpoint\n\nVerification remains pending.",
            "communication": {"mode": "deterministic", "reason": "unconfigured"},
        }

    async def revision(repository, selected, environment):
        value = git(remote, "ls-remote", str(remote), "refs/heads/" + selected)
        return value.split("\t")[0] if value else None

    real_push = scm.push

    async def push(workspace, selected, **kwargs):
        calls.append("push")
        await real_push(
            workspace,
            selected,
            repository_url=str(remote),
            revision=kwargs["revision"],
            expected_remote_revision=kwargs["expected_remote_revision"],
        )

    def record():
        draft = state["draft"]
        return {
            "number": 7,
            "node_id": "PR_fixture",
            "html_url": "https://example.test/team/product/pull/7",
            "state": "open",
            "draft": True,
            "body": draft["body"],
            "title": draft["title"],
            "head": {
                "ref": branch,
                "sha": git(remote, "rev-parse", "refs/heads/" + branch),
                "repo": {"full_name": "team/product"},
            },
            "base": {"ref": "main", "repo": {"full_name": "team/product"}},
        }

    async def provider(request):
        calls.append(request.method)
        if request.method == "GET" and request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[] if state["draft"] is None else [record()])
        assert request.method == "POST"
        state["draft"] = json.loads(request.content)
        return httpx.Response(201, json=record())

    deliveries = env.partial.d
    publication_errors = []
    publish = deliveries._publish_checkpoint

    async def inspect_publication(*args, **kwargs):
        try:
            return await publish(*args, **kwargs)
        except Exception as exc:
            publication_errors.append(repr(exc))
            raise

    deliveries._publish_checkpoint = inspect_publication
    monkeypatch.setattr(scm, "push", push)
    env.store.repository = repo
    deliveries._remote_revision = revision
    deliveries.http_client = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(provider), **kwargs)
    env.engine.connections = SimpleNamespace(
        authentication=authentication, git_environment=environment, assert_snapshot=lambda *args: None
    )
    env.engine.communication_writer = SimpleNamespace(write=wording, assert_current=lambda evidence: None)
    token = CURRENT_PRINCIPAL.set(env.principal)
    try:
        before = copy.deepcopy(await env.store.get_run(RUN))
        controller, body, _ = await queue(env)
        await controller.tickets.execute(body.request_id)
        receipt = await controller.ticket(RUN, body.request_id)
        assert receipt["status"] == "completed", (receipt.get("result"), publication_errors)
        result = receipt["result"]
        assert result["remote_status"] == "draft" and result["verification_status"] == "partial_unverified"
        assert calls.count("push") == calls.count("POST") == 1
        assert state["draft"]["title"].startswith("[UNVERIFIED]") and state["draft"]["draft"] is True
        current = await env.store.get_run(RUN)
        await env.partial.guard(current, current["execution_actor"])
        assert current["git_delivery_summary"]["remote_status"] == "draft"
        assert current["git_delivery_draft"]["url"] == result["pr_url"]
        for key in ("status", "implementation_pending", "bridge_completion_hold", "attempt", "implementation_rounds"):
            assert current.get(key) == before.get(key)
        with pytest.raises(ValueError, match="no final-ready"):
            await deliveries.checkpoint(
                RUN,
                request_id=body.request_id,
                expected_head=result["revision"],
                actor=current["execution_actor"],
                final=True,
            )
    finally:
        CURRENT_PRINCIPAL.reset(token)
