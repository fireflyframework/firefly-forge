# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real PostgreSQL recovery custody with controlled SDK and command transports."""

import asyncio
import copy
import json
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import result
from test_agentic_restore_receipt_native import seed_restore_workspace
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests, prepared
from test_checkpoint_mode_adoption_host import request
from test_identity import fixture as oidc_fixture
from test_identity import request as identity_request

from firefly_forge.agent_roles import ProjectRoles, RoleRegistry, WorkerFeedback
from firefly_forge.checkpoint_mode_adoptions import CheckpointModeAdoptions
from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.engine import Engine
from firefly_forge.git_workspace import metadata_digest
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import LOCAL_TASKS, Journal, digest
from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles
from firefly_forge.models import RunInput
from firefly_forge.original_selection_models import OriginalSelectionReadmissionInput
from firefly_forge.original_selection_readmissions import OriginalSelectionReadmissions
from firefly_forge.persistence import ForgePlanEntity
from firefly_forge.process import ProcessResult
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.providers import BridgeProvider
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.store import now
from firefly_forge.task_routing import TaskRouting
from firefly_forge.tool_broker import FactoryToolPolicyInput, tool_policy_snapshot
from firefly_forge.verification_handoffs import verification_intent


async def historical_lost_handoff(store, journal, run_id, intent):
    """Seed the earlier producer's transition in this isolated test schema only."""
    frozen = store.configuration.snapshot("bridge")
    async with store._unit():
        run = await journal._run(run_id)
        previous = copy.deepcopy(run.data)
        selection = previous["model_selection"]
        assert selection is not None
        transition = {
            "mode": "legacy_provider",
            "previous_agent": "bridge",
            "agent": "bridge",
            "previous_config_revision": previous["config_revision"],
            "config_revision": frozen["config_revision"],
            "previous_model_selection": selection,
            "model_selection": None,
        }
        plan = await journal.plans.find_current(run.id)
        definition = {
            **copy.deepcopy(plan.data),
            "revision": plan.revision + 1,
            "agent": "bridge",
            **frozen,
            "model_selection": None,
            "selection_transition": transition,
        }
        await journal.plans.save(
            ForgePlanEntity(
                id=uuid4(),
                run_id=run.id,
                revision=definition["revision"],
                digest=digest(definition),
                data=definition,
            )
        )
        for task in await journal.tasks.find_for_run(run.id):
            if task.status == "paused" and (task.task_id in LOCAL_TASKS or task.task_id == "review"):
                task.status, task.attempt = "queued", task.attempt + 1
                await journal.tasks.save(task)
        run.status = "queued"
        run.data = {
            **previous,
            **frozen,
            "model_selection": None,
            "model_profile_id": None,
            "status": "queued",
            "resume_existing": True,
            "display_status": "queued",
            "last_pause": previous["pause"],
            "pause": {},
            "error": None,
            "updated_at": now(),
        }
        await store.run_records.save(run)
        await journal._append(
            run_id,
            "handoff",
            uuid4().hex,
            "handoff",
            {
                **{k: v for k, v in transition.items() if k != "mode"},
                "selection_mode": "legacy_provider",
                "automatic": True,
                "verification_handoff_id": intent["id"],
            },
        )


def selection_request(candidate):
    return OriginalSelectionReadmissionInput(
        version=1,
        candidate_digest=candidate["digest"],
        policy_digest=candidate["policy_digest"],
        operator_observation_digest="9" * 64,
        observation_provenance="native_operator_pre_dispatch_observation",
        acknowledge_known_local_readiness_failure=True,
        acknowledge_no_external_model_dispatch=True,
        acknowledge_original_selection_only=True,
        acknowledge_original_mode_and_claim_preserved=True,
        acknowledge_spent_history_and_budgets_preserved=True,
        acknowledge_same_native_continuation=True,
        acknowledge_existing_unknown_command_risk=True,
    )


@asynccontextmanager
async def native_selection_case(store, tmp_path, monkeypatch):
    async with prepared(store, tmp_path) as (_, actor, seeded, *_):
        original = {
            key: value
            for key, value in store.configuration.resolve("bridge").items()
            if key not in {"FORGE_AGENTIC_CHECKPOINTS", "FORGE_BRIDGE_COMPLETION_RECOVERY"}
        }
        named = {
            **original,
            "FORGE_AGENTIC_CHECKPOINTS": "required",
            "FORGE_BRIDGE_COMPLETION_RECOVERY": "required",
        }
        profiles = ModelProfiles(store.configuration)
        profiles.save(
            "local-maintenance",
            ModelProfileInput(
                name="Original implementation",
                provider="bridge",
                values={
                    "endpoint": named["FORGE_BRIDGE_URL"],
                    "token": named["FORGE_BRIDGE_TOKEN"],
                    "model": named["FORGE_BRIDGE_MODEL"],
                    "timeout": named["FORGE_PROVIDER_TIMEOUT"],
                    "checkpoints": "required",
                    "completion_recovery": "required",
                },
            ),
        )
        store.task_router = TaskRouting(profiles)
        original = {**original, "FORGE_BRIDGE_URL": "", "FORGE_BRIDGE_TOKEN": ""}
        store.configuration._write(store.configuration.path, {"bridge": original})
        policy = FactoryToolPolicyInput(automatic_verification={"enabled": True, "max_continuations": 2})
        store.configuration._write(
            store.configuration.directory / "tool-policy.json",
            {"revision": uuid4().hex, "settings": policy.model_dump(mode="json")},
        )
        run = await store.create_run(
            RunInput(
                repository_id=seeded["repository_id"],
                project_id=seeded["project_id"],
                component_id="api",
                agent="bridge",
                brief="Native original selection continuation",
                model_profile_id="local-maintenance",
                skills=[],
            )
        )
        run_id = run["id"]
        await store.update(
            run_id,
            role_snapshot=RoleRegistry(tmp_path / "roles").snapshot(
                ProjectRoles(disabled=["docs"], allowed_tools={"implementer": ["write_file"]})
            ),
            commands=[{"argv": ["controlled-check", str(i)], "timeout": 30} for i in range(7)],
            repair_budget=2,
            implementation_rounds=1,
            implementation_pending=False,
            sandbox_snapshot=capture_sandbox(SandboxInput(mode="host", command_limit=22)),
            tool_policy_snapshot=tool_policy_snapshot(store.configuration),
        )
        journal = Journal(store)
        registry = store.session_provider._registry()
        for repository in (journal.records, journal.tasks, journal.sandboxes):
            repository._bind_transaction_managers(registry)
        provider = BridgeProvider(original)
        model = None
        monkeypatch.setattr(provider, "model", lambda **kwargs: model)
        engine = Engine(store, tmp_path / "owned", lambda _: provider, journal=journal)
        engine.enforce_ownership = True
        engine.configured_provider = True

        def selected(name, settings=None):
            assert name == "bridge" and settings is not None
            provider.settings = dict(settings)
            return provider

        monkeypatch.setattr("firefly_forge.providers.get_provider", selected)
        workspace = engine.workspaces / run_id
        revision = seed_restore_workspace(workspace)
        await store.update(run_id, git_metadata_digest=metadata_digest(workspace))

        async def inspect(*args, **kwargs):
            return {"base_revision": revision, "head": revision, "source_tree": "c" * 40}

        monkeypatch.setattr(engine, "_inspect_workspace", inspect)
        await journal.ensure_plan(run_id)
        prepare = await journal.intent(run_id, "prepare")
        await journal.receipt(run_id, prepare["idempotency_key"], {"base_revision": revision})
        await journal.intent(run_id, "implement")
        feedback = WorkerFeedback.model_validate(
            {
                "id": "native-mode-verification",
                "role_id": "implementer",
                "task_id": "implement",
                "attempt": 1,
                "disposition": "blocked",
                "summary": "Run the configured checks before continuing",
                "request": {
                    "id": "native-mode-question",
                    "kind": "verification",
                    "question": "Run checks?",
                    "context": "The retained source needs verification",
                },
            }
        )
        await journal.worker_feedback(run_id, feedback)
        # Reproduce the old producer's global-provider choice only while seeding
        # the historical verification intent. All recovery below uses production code.
        with monkeypatch.context() as historical:
            historical.setattr(
                "firefly_forge.verification_handoffs.verification_provider",
                lambda *_args: digest({"agent": "bridge", "settings": original}),
            )
            historical.setattr(
                "firefly_forge.verification_models.verification_provider",
                lambda *_args: digest({"agent": "bridge", "settings": original}),
            )
            intent = verification_intent(
                store,
                await store.get_run(run_id),
                workspace,
                await journal.entries(run_id),
                await journal.sandbox_executions(run_id),
            )
            await journal.verification_begin(run_id, intent, check_current=lambda _: None)
            effects = []

            class Backend:
                async def run(self, configuration, argv, cwd, timeout, output_limit):
                    effects.append(argv)
                    return ProcessResult(argv, int(len(effects) == 1), "Controlled native command outcome")

            sandbox = SandboxExecutor(store, journal, SimpleNamespace(select=lambda _: Backend()))
            engine.sandbox = sandbox
            for index, command in enumerate((await store.get_run(run_id))["commands"]):
                await sandbox.execute(
                    run_id,
                    workspace,
                    workspace,
                    command["argv"],
                    command["timeout"],
                    "implement",
                    ordinal=intent["ordinals"][index],
                )
                await journal.verification_command(run_id, intent, index, check_current=lambda _: None)
            await journal.verification_finish(run_id, intent, check_current=lambda _: None)
        await historical_lost_handoff(store, journal, run_id, intent)
        execute = provider.execute

        async def pre_dispatch_guard(*args, **kwargs):
            raise ValueError("native_agentic_checkpoint_wal_required")

        monkeypatch.setattr(provider, "execute", pre_dispatch_guard)
        runtime = RuntimeRoles(store)
        await runtime.start()
        try:
            with monkeypatch.context() as historical:
                historical.setattr(
                    "firefly_forge.verification_models.verification_provider",
                    lambda *_args: digest({"agent": "bridge", "settings": original}),
                )
                historical.setattr(
                    "firefly_forge.verification_handoffs.verification_provider",
                    lambda *_args: digest({"agent": "bridge", "settings": original}),
                )
                with models.override_allow_model_requests(False):
                    await engine.execute_run(run_id)
            failed = await store.get_run(run_id)
            assert failed["status"] == "failed" and failed["error"] == "native_agentic_checkpoint_wal_required", (
                failed["status"],
                failed.get("error"),
                failed.get("pause"),
            )
            assert len(effects) == 7 and failed["implementation_rounds"] == 1
            assert not any(e["kind"] == "bridge_request_intent" for e in await journal.entries(run_id))
            monkeypatch.setattr(provider, "execute", execute)
            store.configuration.update(
                "bridge",
                ProviderConfiguration(
                    values={
                        "checkpoints": "required",
                        "completion_recovery": "required",
                    }
                ),
            )
            model = BridgeProvider(named).model()
            mode_service = CheckpointModeAdoptions(store, journal, engine)
            mode_identity, _, _, _, issue = await oidc_fixture(store)
            mode_actor = await mode_identity.authenticate(
                identity_request(
                    bearer="Bearer "
                    + issue("forge-api", sub="original-mode-operator", email="mode-operator@example.test")
                )
            )
            token = CURRENT_PRINCIPAL.set(mode_actor)
            try:
                # Reproduce a retained earlier policy epoch. The prospective
                # readmission must explicitly mediate this exact old receipt.
                with monkeypatch.context() as old_epoch:
                    old_epoch.setattr("firefly_forge.checkpoint_mode_adoptions.policy_digest", lambda: "4" * 64)
                    old_epoch.setattr("firefly_forge.checkpoint_mode_runtime.policy_digest", lambda: "4" * 64)
                    mode_candidate = await mode_service.candidate(run_id)
                    mode = await mode_service.request(run_id, uuid4().hex, request(mode_candidate))
                    # Exercise the actual readiness guard before any HTTP dispatch.
                    with monkeypatch.context() as guard:
                        guard.setattr(provider, "model", BridgeProvider.model.__get__(provider))
                        with models.override_allow_model_requests(False):
                            await engine.execute_run(run_id)
            finally:
                CURRENT_PRINCIPAL.reset(token)
                await mode_identity.client.aclose()
            failed = await store.get_run(run_id)
            assert failed["status"] == "failed"
            assert failed["error"] == "Configure FORGE_BRIDGE_URL and FORGE_BRIDGE_TOKEN before selecting the bridge"
            assert not any(e["kind"] == "bridge_request_intent" for e in await journal.entries(run_id))
            yield SimpleNamespace(
                store=store,
                actor=actor,
                run_id=run_id,
                journal=journal,
                engine=engine,
                workspace=workspace,
                revision=revision,
                model=model,
                effects=effects,
                service=OriginalSelectionReadmissions(store, journal, engine),
                mode=mode,
                named=named,
                profiles=profiles,
                mode_actor=mode_actor,
                failed=failed,
                runtime=runtime,
            )
        finally:
            await runtime.stop()
            if model is not None:
                await model.client.close()


@pytest.mark.parametrize("boundary", ["complete", "rollback", "revoked", "competing", "restore"])
async def test_native_original_selection_preserves_history_and_current_authority(
    store, tmp_path, monkeypatch, boundary
):
    async with native_selection_case(store, tmp_path, monkeypatch) as c:
        candidate = await c.service.candidate(c.run_id)
        prefix = await c.journal.entries(c.run_id)
        frozen = copy.deepcopy(c.failed)
        identifier = uuid4().hex
        if boundary == "rollback":
            append = c.journal._append

            async def fail(run, task, key, kind, payload):
                if kind == "original_selection_readmission":
                    raise RuntimeError("Controlled native original selection receipt rollback")
                return await append(run, task, key, kind, payload)

            with monkeypatch.context() as patch:
                patch.setattr(c.journal, "_append", fail)
                with pytest.raises(RuntimeError, match="receipt rollback"):
                    await c.service.request(c.run_id, identifier, selection_request(candidate))
            assert await store.get_run(c.run_id) == frozen
            assert await c.journal.entries(c.run_id) == prefix
            return
        if boundary == "revoked":
            async with store._unit():
                users = ForgeIdentityUserRepository()
                user = await users.find_by_id(c.actor.id)
                user.enabled = False
                await users.save(user)
            with pytest.raises(AccessDenied):
                await c.service.request(c.run_id, identifier, selection_request(candidate))
            assert await store.get_run(c.run_id) == frozen
            with pytest.raises(AccessDenied):
                await c.journal.entries(c.run_id)
            from firefly_forge.known_worker_completions import _entries
            from firefly_forge.store import identifier as native_identifier

            async with store._unit():
                assert _entries(await c.journal.records.find_for_run(native_identifier(c.run_id)), c.run_id) == prefix
            return
        if boundary == "competing":
            outcomes = await asyncio.gather(
                *(c.service.request(c.run_id, uuid4().hex, selection_request(candidate)) for _ in range(2)),
                return_exceptions=True,
            )
            assert sum(isinstance(value, dict) for value in outcomes) == 1
            assert all(isinstance(value, (dict, ValueError, AccessDenied)) for value in outcomes)
            assert (
                len([e for e in await c.journal.entries(c.run_id) if e["kind"] == "original_selection_readmission"])
                == 1
            )
            return
        adoption = await c.service.request(c.run_id, identifier, selection_request(candidate))
        assert await c.service.get(c.run_id, identifier) == adoption
        assert await c.service.request(c.run_id, identifier, selection_request(candidate)) == adoption
        assert (await c.journal.entries(c.run_id))[: len(prefix)] == prefix
        posts = []

        def transport(http_request):
            if http_request.method == "GET":
                return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
            posts.append(http_request.content)
            assert len(posts) <= 2
            return httpx2.Response(
                200,
                json=result(tool=True)
                if len(posts) == 1
                else result(
                    json.dumps(
                        {
                            "summary": "Complete the retained change",
                            "worker_feedback": None,
                            "qa_report": None,
                        }
                    )
                ),
            )

        c.model.client._client._transport = httpx2.MockTransport(transport)
        c.model.client._client._mounts = {}
        with monkeypatch.context() as patch:
            if boundary == "restore":
                from firefly_forge.agent_checkpoints import AgentCheckpoints
                from firefly_forge.store import identifier as native_identifier

                consume = AgentCheckpoints._consume

                async def interrupt_second(service, current_run, intent, body, current, reader):
                    async with store._unit():
                        count = (await c.journal.records.checkpoint_budget(native_identifier(c.run_id)))["count"]
                    if count == 2:
                        raise RuntimeError("Controlled interruption after retained original HTTP response")
                    return await consume(service, current_run, intent, body, current, reader)

                patch.setattr(AgentCheckpoints, "_consume", interrupt_second)
            with models.override_allow_model_requests(False), mock_model_requests(c.model):
                await c.engine.execute_run(c.run_id)
        if boundary == "restore":
            from firefly_forge.agent_checkpoint_models import RestoreInput
            from firefly_forge.agent_restores import AgentRestores
            from firefly_forge.execution_requests import ExecutionRequests

            interrupted = await store.get_run(c.run_id)
            assert interrupted["status"] == "needs_reconciliation" and len(posts) == 2
            entries = await c.journal.entries(c.run_id)
            checkpoint = [e for e in entries if e["kind"] == "agent_checkpoint"][-1]
            intent = next(
                e
                for e in entries
                if e["kind"] == "bridge_request_intent" and e["idempotency_key"] == checkpoint["idempotency_key"]
            )
            response = next(
                e
                for e in entries
                if e["kind"] == "bridge_request_response" and e["idempotency_key"] == checkpoint["idempotency_key"]
            )
            requests = ExecutionRequests(store, c.runtime)
            restores = AgentRestores(store, requests, c.engine)
            restore = RestoreInput(
                request_id=uuid4().hex,
                expected_checkpoint_digest=checkpoint["digest"],
                expected_intent_digest=intent["digest"],
                source={"kind": "http_response", "record_digest": response["digest"]},
            )
            await restores.request(c.run_id, intent["idempotency_key"], restore)
            before = (c.workspace / "service.py").stat().st_mtime_ns
            with models.override_allow_model_requests(False), mock_model_requests(c.model):
                await restores.execute(restore.request_id)
            assert (await requests.get(restore.request_id))["status"] == "completed"
            assert len(posts) == 2 and (c.workspace / "service.py").stat().st_mtime_ns == before
            consumed = [
                e
                for e in await c.journal.entries(c.run_id)
                if e["kind"] == "agent_consume" and e["payload"]["version"] == 2
            ]
            assert len(consumed) == 1 and consumed[0]["payload"]["restore_request_id"] == restore.request_id
        current = await store.get_run(c.run_id)
        assert current["status"] == "succeeded", (current.get("error"), len(posts))
        assert current["config_revision"] == frozen["config_revision"]
        assert current["model_selection"] is None and not current["execution_config"]["endpoint"]
        assert (
            c.mode["payload"]["execution_config_revision"]
            != adoption["payload"]["candidate"]["execution_config_revision"]
        )
        assert current["repair_budget"] == 2 and current["implementation_rounds"] == 2
        assert len(posts) == 2 and len(c.effects) == 14
        assert len(current["final_verification"]) == 7
        assert (c.workspace / "service.py").read_text() == "VALUE = 1\n"
        assert current["checkpoint_revision"] != c.revision
        assert (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=c.workspace).decode().strip()
            == current["checkpoint_revision"]
        )
        entries = await c.journal.entries(c.run_id)
        assert Journal.verify_chain(entries)
        assert len([e for e in entries if e["kind"] == "checkpoint_mode_claim"]) == 1
        assert len([e for e in entries if e["kind"] == "original_selection_claim"]) == 1
        assert (await c.service.get(c.run_id, identifier)) == adoption
        http = [e for e in entries if e["kind"] == "bridge_request_intent"]
        assert len(http) == 2 and all(e["payload"]["task_attempt"] == 2 for e in http)
        assert all(e["payload"]["profile_id"] == "local-maintenance" for e in http)
        assert all(
            e["payload"]["config_revision"] == adoption["payload"]["candidate"]["execution_config_revision"]
            for e in http
        )
        assert len([e for e in entries if e["kind"] == "terminal_completion_receipt"]) == 1
        assert len(await c.journal.sandbox_executions(c.run_id)) == 14
        assert len([e for e in entries if e["task_id"] == "verify" and e["kind"] == "receipt"]) == 1
        assert not store.configuration.load_snapshot(frozen["config_revision"]).get("FORGE_AGENTIC_CHECKPOINTS")


@pytest.mark.parametrize("change", ["mode_actor", "profile", "source"])
async def test_native_original_selection_late_change_never_commits_or_dispatches(
    store,
    tmp_path,
    monkeypatch,
    change,
):
    async with native_selection_case(store, tmp_path, monkeypatch) as c:
        candidate = await c.service.candidate(c.run_id)
        before = await c.journal.entries(c.run_id)
        run = await store.get_run(c.run_id)
        original_prepare = c.service._prepare_selection

        async def late(*args, **kwargs):
            prepared = await original_prepare(*args, **kwargs)
            if change == "mode_actor":
                assert c.mode_actor.id != c.actor.id
                async with store._unit():
                    users = ForgeIdentityUserRepository()
                    actor = await users.find_by_id(c.mode_actor.id)
                    actor.enabled = False
                    await users.save(actor)
            elif change == "profile":
                c.profiles.save(
                    "local-maintenance",
                    ModelProfileInput(
                        name="Changed connection", provider="bridge", values={"endpoint": "https://other.invalid"}
                    ),
                )
            else:
                (c.workspace / "service.py").write_text("VALUE = 9\n")
            return prepared

        monkeypatch.setattr(c.service, "_prepare_selection", late)
        with pytest.raises((ValueError, AccessDenied)):
            await c.service.request(c.run_id, uuid4().hex, selection_request(candidate))
        assert await store.get_run(c.run_id) == run
        assert await c.journal.entries(c.run_id) == before
        assert len(c.effects) == 7
        assert not any(e["kind"] == "bridge_request_intent" for e in before)
