# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual DI/PG/current SCM grants and local Git network transports."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from scm_polling_native_fixtures import native_poll as native_poll  # noqa: F401
from worker_reply_fixtures import authenticated_operator

from firefly_forge import scm
from firefly_forge.checkpoint_test_sources import CheckpointTestSources
from firefly_forge.engine import Engine
from firefly_forge.git_deliveries import GitDeliveries
from firefly_forge.git_delivery_models import GitDeliveryPolicyInputV1
from firefly_forge.git_delivery_policy import GitDeliveryPolicies
from firefly_forge.journal import Journal
from firefly_forge.models import RunInput
from firefly_forge.providers import AgentResult
from firefly_forge.run_creation_requests import RunCreationRequests
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.store import Store


@pytest.fixture
def checkpoint_native(native_poll, tmp_path, monkeypatch):
    case = native_poll
    origin = tmp_path / "controlled-git-origin"
    origin.mkdir()
    original_checked = scm.checked
    transport = []

    async def git(argv, cwd, timeout=300, *, env=None):
        local_clone = argv[1] == "clone" and case.repo["url"] in argv
        if case.repo["url"] in argv:
            transport.append(argv[1])
            argv = [str(origin) if item == case.repo["url"] else item for item in argv]
        result = await original_checked(argv, cwd, timeout, env=env)
        if local_clone:
            await original_checked(["git", "remote", "set-url", "origin", case.repo["url"]], Path(argv[-1]))
        return result

    monkeypatch.setattr(scm, "checked", git)
    monkeypatch.setattr("firefly_forge.git_deliveries.checked", git)
    draft_state = {"body": None, "posts": 0}
    worker = runtime = None

    async def provider(request):
        from pyfly.data.transaction import current_unit_of_work

        assert current_unit_of_work() is None
        assert request.url.host == "api.github.com"
        assert request.url.path in {"/repos/team/product/pulls", "/repos/team/product/pulls/17"}
        if request.method == "GET" and draft_state["body"] is None:
            return httpx.Response(200, json=[])
        if request.method == "POST":
            draft_state["posts"] += 1
            assert draft_state["posts"] == 1
            draft_state["body"] = json.loads(request.content)
        assert request.method in {"GET", "POST"}
        body = draft_state["body"]
        revision = await original_checked(["git", "rev-parse", "refs/heads/" + body["head"]], origin)
        record = dict(
            number=17,
            node_id="PUBLIC_CHECKPOINT_PR",
            html_url="https://github.com/team/product/pull/17",
            state="open",
            draft=True,
            body=body["body"],
            head=dict(ref=body["head"], sha=revision, repo=dict(id=42, full_name="team/product")),
            base=dict(ref="main", repo=dict(id=42, full_name="team/product")),
        )
        return httpx.Response(
            201 if request.method == "POST" else 200,
            json=[record] if request.url.path.endswith("/pulls") and request.method == "GET" else record,
        )

    async def start():
        nonlocal worker, runtime
        await original_checked(["git", "init", "--initial-branch=main"], origin)
        (origin / "product.py").write_text("value = 1\n")
        await original_checked(["git", "add", "."], origin)
        await original_checked(
            [
                "git",
                "-c",
                "user.name=Public fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "Public source baseline",
            ],
            origin,
        )
        await case.store.release_ownership()
        worker = Store(case.store.path)
        worker.configuration = case.store.configuration
        worker.connections = case.scm
        runtime = RuntimeRoles(worker, "worker")
        worker.runtime_roles = runtime
        await worker.initialize()
        await runtime.start()
        await worker.acquire_ownership()
        calls = []

        class Worker:
            async def execute(self, workspace, *args):
                calls.append(workspace)
                assert len(calls) == 1
                (workspace / "product.py").write_text("value = 2\n")
                feedback = dict(
                    id="source-parent-feedback",
                    role_id="implementer",
                    task_id="implement",
                    attempt=1,
                    disposition="clarification",
                    summary="Parent is paused and unverified",
                    evidence_paths=[],
                    request=dict(
                        id="source-parent-question",
                        kind="other",
                        question="Keep this source?",
                        context="A fresh test does not answer or resume this question.",
                        choices=["Keep", "Stop"],
                    ),
                )
                return AgentResult(
                    json.dumps(dict(summary="Unverified retained checkpoint", feedback=feedback)), "public fixture"
                )

        journal = Journal(worker)
        engine = Engine(worker, case.http.app.state.engine.workspaces, lambda _: Worker(), journal=journal)
        engine.enforce_ownership = True
        engine.connections = case.scm
        engine.communication_writer = case.http.app.state.engine.communication_writer
        deliveries = GitDeliveries(worker, engine, journal)
        engine.git_deliveries = deliveries
        client = httpx.AsyncClient
        deliveries.http_client = lambda **kwargs: client(transport=httpx.MockTransport(provider), **kwargs)
        async with authenticated_operator(worker):
            await GitDeliveryPolicies(worker).configure(
                case.project["id"],
                GitDeliveryPolicyInputV1(
                    version=1,
                    request_id=uuid4().hex,
                    expected_previous_digest=None,
                    policy=dict(enabled=True, every_units=1, on_pause=True, remote="draft_pr", max_checkpoints=50),
                ),
            )
            parent = await worker.create_run(
                RunInput(
                    repository_id=case.repo["id"],
                    project_id=case.project["id"],
                    component_id="api",
                    brief="Create an explicitly unverified source checkpoint",
                    skills=[],
                )
            )
            engine.active[parent["id"]] = asyncio.current_task()
            try:
                await engine.execute_run(parent["id"])
            finally:
                engine.active.pop(parent["id"], None)
            parent = await worker.get_run(parent["id"])
            assert parent["status"] == "paused", parent.get("error")
            receipt = (await deliveries.view(parent["id"]))["items"][-1]
            assert receipt["remote_status"] == "draft" and draft_state["posts"] == 1 and len(calls) == 1
            prefix = await journal.entries(parent["id"])
        commands = [
            dict(argv=[sys.executable, "-c", f"from product import value; assert value == 2; print({i})"], timeout=30)
            for i in range(1, 8)
        ]
        body = dict(
            version=1,
            input=dict(
                repository_id=case.repo["id"],
                project_id=case.project["id"],
                component_id="api",
                brief="Verify retained checkpoint in a fresh ordinary test",
                operation="test",
                publish=False,
                commands=commands,
                skills=[],
                sandbox=dict(mode="host", network="none", command_limit=7),
                source_checkpoint=dict(
                    version=1,
                    run_id=parent["id"],
                    request_id=receipt["request_id"],
                    expected_checkpoint_digest=receipt["checkpoint_digest"],
                    expected_revision=receipt["revision"],
                ),
            ),
        )
        context = case.http.app.state.runtime.context
        assert context.get_bean(CheckpointTestSources).store is case.store
        assert context.get_bean(RunCreationRequests).store is case.store
        assert case.store.checkpoint_sources is context.get_bean(CheckpointTestSources)
        return SimpleNamespace(
            case=case,
            store=worker,
            engine=engine,
            journal=journal,
            deliveries=deliveries,
            parent=parent,
            checkpoint=receipt,
            prefix=prefix,
            commands=commands,
            body=body,
            transport=transport,
            draft_state=draft_state,
            origin=origin,
            original_checked=original_checked,
        )

    try:
        yield case.http.portal.call(start)
    finally:

        async def close():
            try:
                if runtime is not None:
                    await runtime.stop()
            finally:
                if worker is not None:
                    await worker.close()

        case.http.portal.call(close)
