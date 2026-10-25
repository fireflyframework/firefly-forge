# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared real PostgreSQL/ACL settlement cases with controlled SDK transport."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import result
from test_agentic_restore_receipt_native import seed_restore_workspace
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests, prepared
from test_known_worker_completion_service_host import consent

from firefly_forge.agent_roles import ProjectRoles, RoleRegistry
from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.engine import Engine
from firefly_forge.git_workspace import metadata_digest
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.known_worker_completions import KnownWorkerCompletions
from firefly_forge.models import RunInput
from firefly_forge.project_access import AccessDenied
from firefly_forge.providers import BridgeProvider
from firefly_forge.runtime_roles import RuntimeRoles


@asynccontextmanager
async def known_native(store, tmp_path, monkeypatch, *, unknown=False, automatic=False, normal=False):
    async with prepared(store, tmp_path) as (_, actor, seeded, *_):
        store.configuration.update("bridge", ProviderConfiguration(values={"checkpoints": "required"}))
        run = await store.create_run(
            RunInput(
                repository_id=seeded["repository_id"],
                project_id=seeded["project_id"],
                component_id="api",
                agent="bridge",
                brief="Controlled known worker outcome",
                skills=[],
            )
        )
        run_id = run["id"]
        await store.update(
            run_id,
            **store.configuration.snapshot("bridge"),
            role_snapshot=RoleRegistry(tmp_path / "known-roles").snapshot(
                ProjectRoles(disabled=["docs"], allowed_tools={"implementer": ["write_file"]})
            ),
            resume_existing=True,
            commands=[],
        )
        journal = Journal(store)
        registry = store.session_provider._registry()
        assert registry is not None
        for repository in (journal.records, journal.tasks):
            repository._bind_transaction_managers(registry)
        provider = BridgeProvider(store.configuration.load_snapshot((await store.get_run(run_id))["config_revision"]))
        model = provider.model()
        monkeypatch.setattr(provider, "model", lambda **kwargs: model)
        engine = Engine(store, tmp_path / "known-owned", lambda _: provider, journal=journal)
        engine.enforce_ownership = True
        workspace = engine.workspaces / run_id
        revision = seed_restore_workspace(workspace)
        await store.update(run_id, git_metadata_digest=metadata_digest(workspace))

        async def inspect(*args, **kwargs):
            return {"base_revision": revision, "head": revision, "source_tree": "c" * 40}

        monkeypatch.setattr(engine, "_inspect_workspace", inspect)
        feedback = dict(
            id="known-native-final",
            role_id="implementer",
            task_id="implement",
            attempt=1,
            disposition="blocked",
            summary="A native decision is retained",
            evidence_paths=[],
            request=dict(
                id="known-native-question",
                kind="other",
                question="Keep the existing value?",
                context="The known response remains retained.",
                choices=["Keep", "Review"],
            ),
        )
        posts = []
        if not automatic:
            accept_feedback = journal.worker_feedback

            async def historical_feedback(*args, **kwargs):
                current = await store.get_run(run_id)
                usage = current.get("usage", {})
                usage.pop("runtime_report", None)
                await store.update(run_id, usage=usage)
                return await accept_feedback(*args, **kwargs)

            monkeypatch.setattr(journal, "worker_feedback", historical_feedback)

        def transport(request):
            if request.method == "GET":
                return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
            posts.append(request.content)
            assert len(posts) <= 2
            if unknown and len(posts) == 2:
                raise httpx2.ReadError("Controlled original outcome unknown", request=request)
            return httpx2.Response(
                200,
                json=result(tool=True)
                if len(posts) == 1
                else result(
                    json.dumps(
                        {
                            "summary": "Known worker output",
                            "worker_feedback": None if normal else feedback,
                            "qa_report": None,
                        }
                    )
                ),
            )

        model.client._client._transport = httpx2.MockTransport(transport)
        model.client._client._mounts = {}
        runtime = RuntimeRoles(store)
        await runtime.start()
        try:
            with models.override_allow_model_requests(False), mock_model_requests(model):
                await engine.execute_run(run_id)
            current = await store.get_run(run_id)
            if not automatic:
                assert current["status"] == ("needs_reconciliation" if unknown else "paused"), current.get("error")
                assert current["bridge_completion_hold"]
            assert len(posts) == 2
            yield SimpleNamespace(
                store=store,
                journal=journal,
                service=KnownWorkerCompletions(store, journal),
                run_id=run_id,
                actor=actor,
                posts=posts,
                workspace=workspace,
            )
        finally:
            await runtime.stop()
            await model.client.close()


async def test_native_known_worker_receipt_projection_and_rollback_are_atomic(store, tmp_path, monkeypatch):
    async with known_native(store, tmp_path, monkeypatch) as c:
        candidate = await c.service.candidate(c.run_id)
        request = consent(candidate)
        before = await store.get_run(c.run_id)
        prefix = await c.journal.entries(c.run_id)
        original = store.run_records.save

        async def refused(row):
            result = await original(row)
            if row.data.get("bridge_completion_settlement"):
                raise RuntimeError("Controlled native projection rollback")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(store.run_records, "save", refused)
            with pytest.raises(RuntimeError, match="projection rollback"):
                await c.service.request(c.run_id, uuid4().hex, request)
        assert await store.get_run(c.run_id) == before and await c.journal.entries(c.run_id) == prefix
        request_id = uuid4().hex
        record = await c.service.request(c.run_id, request_id, request)
        after = await store.get_run(c.run_id)
        assert (
            after["bridge_completion_hold"] == {}
            and record["payload"]["observed_hold"] == before["bridge_completion_hold"]
        )
        assert {
            k: v for k, v in after.items() if k not in {"bridge_completion_hold", "bridge_completion_settlement"}
        } == {k: v for k, v in before.items() if k != "bridge_completion_hold"}
        assert await c.service.request(c.run_id, request_id, request) == record
        assert len(c.posts) == 2 and (c.workspace / "service.py").read_text() == "VALUE = 1\n"


async def test_native_competing_settlements_have_one_receipt_without_extra_grant(store, tmp_path, monkeypatch):
    async with known_native(store, tmp_path, monkeypatch) as c:
        candidate = await c.service.candidate(c.run_id)
        request = consent(candidate)
        outcomes = await asyncio.gather(
            *(c.service.request(c.run_id, uuid4().hex, request) for _ in range(2)), return_exceptions=True
        )
        assert sum(isinstance(value, dict) for value in outcomes) == 1
        assert all(isinstance(value, (dict, ValueError, AccessDenied)) for value in outcomes)
        entries = await c.journal.entries(c.run_id)
        assert sum(e["kind"] == "worker_completion_receipt" for e in entries) == 1
        run = await store.get_run(c.run_id)
        assert run["status"] == "paused" and run["worker_request"]["status"] == "pending" and len(c.posts) == 2
        assert not any(e["kind"] in {"resume", "agent_restore"} for e in entries)


async def test_native_current_actor_revocation_refuses_known_output_settlement(store, tmp_path, monkeypatch):
    async with known_native(store, tmp_path, monkeypatch) as c:
        candidate = await c.service.candidate(c.run_id)
        before = await store.get_run(c.run_id)
        async with store._unit():
            users = ForgeIdentityUserRepository()
            user = await users.find_by_id(c.actor.id)
            user.enabled = False
            await users.save(user)
        with pytest.raises(AccessDenied):
            await c.service.request(c.run_id, uuid4().hex, consent(candidate))
        assert await store.get_run(c.run_id) == before and len(c.posts) == 2
        async with store._unit():
            rows = await c.journal.records.find_for_run(UUID(c.run_id))
        assert not any(row.kind == "worker_completion_receipt" for row in rows)


async def test_native_unknown_completion_never_becomes_a_known_worker_receipt(store, tmp_path, monkeypatch):
    async with known_native(store, tmp_path, monkeypatch, unknown=True) as c:
        before = await store.get_run(c.run_id)
        with pytest.raises(ValueError, match="ineligible"):
            await c.service.candidate(c.run_id)
        assert await store.get_run(c.run_id) == before and len(c.posts) == 2
        entries = await c.journal.entries(c.run_id)
        assert not any(e["kind"] == "worker_completion_receipt" for e in entries)
