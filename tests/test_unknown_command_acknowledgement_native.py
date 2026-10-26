# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED: actual PostgreSQL custody; controlled workers/backends, no model or remote Git."""

import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_execution import seed_repository
from test_identity import fixture as oidc_fixture
from test_identity import request as identity_request
from test_unknown_command_acknowledgement_host import consent
from worker_reply_fixtures import authenticated_operator

from firefly_forge.engine import Engine
from firefly_forge.engine import checked as native_git
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.process import ProcessResult
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.providers import AgentResult
from firefly_forge.sandbox import SandboxExecutor, SandboxReconciliationRequired
from firefly_forge.tool_broker import FactoryToolPolicyInput, tool_policy_snapshot
from firefly_forge.unknown_command_acknowledgements import (
    UnknownCommandAcknowledgements,
)
from firefly_forge.unknown_command_proof import ACKNOWLEDGEMENT
from firefly_forge.verification_handoffs import VerificationHandoffs
from firefly_forge.verification_models import VerificationAdoption


def renew(store, continuations):
    settings = copy.deepcopy(tool_policy_snapshot(store.configuration)["settings"])
    settings["automatic_verification"] = {
        "enabled": True,
        "max_continuations": continuations,
    }
    policy = FactoryToolPolicyInput.model_validate(settings)
    store.configuration._write(
        store.configuration.directory / "tool-policy.json",
        {"revision": uuid4().hex, "settings": policy.model_dump()},
    )


async def adopt(case, candidate):
    followup = candidate["followup"]
    await case.service.adopt(
        case.run["id"],
        VerificationAdoption.model_validate(
            {
                "request_id": followup["adoption_request_id"],
                **{
                    key: followup[key]
                    for key in (
                        "pending_request_digest",
                        "source_digest",
                        "commands_digest",
                        "policy_digest",
                        "provider_digest",
                        "agent",
                    )
                },
            }
        ),
    )


async def select(case, execution=None, adoption=None):
    return await case.acks.candidate(
        case.run["id"],
        execution or case.unknown["execution_id"],
        agent="codex",
        adoption_request_id=adoption or uuid4().hex,
    )


async def acknowledge(case, candidate):
    return await case.acks.request(case.run["id"], candidate["execution_id"], uuid4().hex, consent(candidate))


async def disable(case):
    async with case.store._unit():
        row = await ForgeIdentityUserRepository().find_by_id(case.actor.id)
        assert row is not None and row.enabled
        row.enabled = False
        await ForgeIdentityUserRepository().save(row)


async def observe_after_revocation(case):
    run_id = case.run["id"]
    assert CURRENT_PRINCIPAL.get() == case.actor
    with pytest.raises(AccessDenied):
        await case.journal.entries(run_id)
    token = CURRENT_PRINCIPAL.set(case.original_actor)
    try:
        await ProjectAccess(case.store).require_project(case.original_actor, case.run["project_id"], "read")
        return (
            await case.journal.entries(run_id),
            await case.journal.sandbox_executions(run_id),
            await case.store.get_run(run_id),
        )
    finally:
        CURRENT_PRINCIPAL.reset(token)
        assert CURRENT_PRINCIPAL.get() == case.actor


async def prepared_project(store, tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_REPAIR_ATTEMPTS", "0")
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = await seed_repository(tmp_path / "source")
    repo = await store.add_repository(RepositoryInput(name="Native command custody", url=str(source)))
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Native command acknowledgement",
            description="Isolated PostgreSQL evidence only",
            topology="monorepo",
            owners=["Native fixture"],
            components=[
                dict(id="api", name="API", repository_id=repo["id"], responsibility="Retain native command evidence")
            ],
        )
    )
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            project_id=project["id"],
            component_id="api",
            agent="codex",
            brief="Native acknowledgement continuation proof",
            frontend="none",
            skills=[],
            commands=[Command(argv=["approved-check", str(index)], timeout=10) for index in range(7)],
        )
    )
    assert run["project_id"] == project["id"] and run["execution_actor"]["user_id"]
    journal, worker_calls, backend_calls = Journal(store), [], []

    class Worker:
        async def execute(self, workspace, brief, *args):
            worker_calls.append(brief)
            (workspace / "partial.py").write_text("VALUE = 1\n")
            return AgentResult(
                "Native fixture requests configured verification",
                "fixture",
                usage={
                    "worker_feedback": {
                        "id": "native-feedback-" + str(len(worker_calls)),
                        "role_id": "implementer",
                        "task_id": "implement",
                        "attempt": len(worker_calls),
                        "disposition": "blocked",
                        "summary": "Configured native verification receipts are needed",
                        "request": {
                            "id": "native-request-" + str(len(worker_calls)),
                            "kind": "verification",
                            "question": "Can the approved checks run?",
                            "context": "No additional command authority",
                        },
                    },
                },
            )

    class Backend:
        async def run(self, config, argv, cwd, timeout, limit, **kwargs):
            backend_calls.append(list(argv))
            return ProcessResult(argv, 0, "Known controlled native backend receipt")

    backend = Backend()
    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal)
    engine.sandbox = SandboxExecutor(store, journal, backends=SimpleNamespace(select=lambda config: backend))
    service = VerificationHandoffs(store, journal, engine)
    engine.enforce_ownership = True
    await store.acquire_ownership()
    await engine.execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "paused"
    assert len(worker_calls) == 1 and not backend_calls
    return SimpleNamespace(
        run=run,
        repo=repo,
        journal=journal,
        engine=engine,
        service=service,
        calls=worker_calls,
        backend=backend,
        backend_calls=backend_calls,
    )


@asynccontextmanager
async def native_case(store, tmp_path, monkeypatch):
    async with authenticated_operator(store) as original_actor:
        env = await prepared_project(store, tmp_path, monkeypatch)
        env.store = store
        env.engine.enforce_ownership = True
        env.workspace = env.engine.workspaces / env.run["id"]
        successful = env.backend.run

        async def uncertain(config, argv, *args, **kwargs):
            if env.backend_calls:
                env.backend_calls.append(list(argv))
                raise TimeoutError("Controlled original native command outcome unknown")
            return await successful(config, argv, *args, **kwargs)

        env.backend.run = uncertain
        await env.service.execute(env.run["id"])
        rows = await env.journal.sandbox_executions(env.run["id"])
        assert [row["ordinal"] for row in rows] == [1, 2]
        env.unknown = copy.deepcopy(rows[-1])
        assert env.unknown["status"] == "removed"
        assert env.unknown["effects"]["start"] == {"status": "intent"}
        assert env.unknown["effects"]["cleanup"]["status"] == "done"
        renew(store, 2)
        candidate = await env.service.candidates(env.run["id"], "codex")
        await env.service.adopt(
            env.run["id"],
            VerificationAdoption.model_validate(
                {
                    "request_id": uuid4().hex,
                    **{
                        key: candidate[key]
                        for key in (
                            "pending_request_digest",
                            "source_digest",
                            "commands_digest",
                            "policy_digest",
                            "provider_digest",
                            "agent",
                        )
                    },
                }
            ),
        )
        await env.service.execute(env.run["id"])
        assert (await store.get_run(env.run["id"]))["verification_handoff"]["status"] == "held"
        assert await env.journal.sandbox_executions(env.run["id"]) == rows
        assert len(env.backend_calls) == 2
        renew(store, 3)
        identity, _, _, _, issue = await oidc_fixture(store)
        token = None
        try:
            actor = await identity.authenticate(
                identity_request(
                    bearer="Bearer " + issue("forge-api", roles=["forge-admin"], sub="native-ack-" + uuid4().hex)
                )
            )
            assert actor is not None and actor.id != original_actor.id
            assert await ProjectAccess(store).current_administrator(actor)
            token = CURRENT_PRINCIPAL.set(actor)
            env.actor, env.original_actor = actor, original_actor
            env.acks = UnknownCommandAcknowledgements(store, env.journal, env.service)
            env.successful = successful
            yield env
        finally:
            if token is not None:
                CURRENT_PRINCIPAL.reset(token)
            await identity.client.aclose()


async def test_native_ack_atomic_rollback_exact_duplicate_and_unchanged_original(store, tmp_path, monkeypatch):
    async with native_case(store, tmp_path, monkeypatch) as case:
        run_id = case.run["id"]
        before = copy.deepcopy(
            (
                await store.get_run(run_id),
                await case.journal.entries(run_id),
                await case.journal.sandbox_executions(run_id),
            )
        )
        candidate = await select(case)
        request_id = uuid4().hex
        save = case.journal.records.save

        async def fail_after_insert(row):
            result = await save(row)
            if row.kind == ACKNOWLEDGEMENT:
                raise RuntimeError("Controlled native acknowledgement transaction rollback")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(case.journal.records, "save", fail_after_insert)
            with pytest.raises(RuntimeError, match="transaction rollback"):
                await case.acks.request(run_id, case.unknown["execution_id"], request_id, consent(candidate))
        assert (
            await store.get_run(run_id),
            await case.journal.entries(run_id),
            await case.journal.sandbox_executions(run_id),
        ) == before
        receipt = await case.acks.request(run_id, case.unknown["execution_id"], request_id, consent(candidate))
        assert await case.acks.request(run_id, case.unknown["execution_id"], request_id, consent(candidate)) == receipt
        assert await case.acks.get(run_id, case.unknown["execution_id"], request_id) == receipt
        entries = await case.journal.entries(run_id)
        assert len(entries) == len(before[1]) + 1 and entries[-1] == receipt
        assert receipt["kind"] == ACKNOWLEDGEMENT and Journal.verify_chain(entries)
        assert await store.get_run(run_id) == before[0]
        assert await case.journal.sandbox_executions(run_id) == before[2]
        assert len(case.backend_calls) == 2


async def test_native_ack_competing_writers_stale_cross_scope_and_current_identity_refuse(store, tmp_path, monkeypatch):
    async with native_case(store, tmp_path, monkeypatch) as case:
        run_id = case.run["id"]
        candidate = await select(case)
        other = UnknownCommandAcknowledgements(
            store,
            case.journal,
            VerificationHandoffs(
                store,
                case.journal,
                SimpleNamespace(workspaces=case.engine.workspaces, publication_lock=asyncio.Lock()),
            ),
        )
        results = await asyncio.gather(
            *[
                api.request(
                    run_id,
                    case.unknown["execution_id"],
                    uuid4().hex,
                    consent(candidate),
                )
                for api in (case.acks, other)
            ],
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, (ValueError, AccessDenied)) for result in results) == 1
        entries = await case.journal.entries(run_id)
        assert len([row for row in entries if row["kind"] == ACKNOWLEDGEMENT]) == 1
        candidate = await select(case)
        foreign = await prepared_project(store, tmp_path / "foreign-project", monkeypatch)

        async def foreign_unknown(config, argv, *args, **kwargs):
            foreign.backend_calls.append(list(argv))
            raise TimeoutError("Controlled foreign project's unknown command")

        foreign.backend.run = foreign_unknown
        await foreign.service.execute(foreign.run["id"])
        foreign_rows = await foreign.journal.sandbox_executions(foreign.run["id"])
        assert len(foreign_rows) == 1 and foreign_rows[0]["status"] == "removed"
        assert foreign.run["project_id"] != case.run["project_id"]
        with pytest.raises((ValueError, KeyError, AccessDenied)):
            await case.acks.request(run_id, foreign_rows[0]["execution_id"], uuid4().hex, consent(candidate))
        assert await foreign.journal.sandbox_executions(foreign.run["id"]) == foreign_rows
        async with store._unit():
            await case.journal._append(
                run_id,
                "implement",
                uuid4().hex,
                "observation",
                {"native_test": "stale-cut"},
            )
        with pytest.raises(ValueError, match="changed|differs"):
            await acknowledge(case, candidate)
        candidate = await select(case)
        before = await case.journal.entries(run_id)
        await disable(case)
        with pytest.raises(AccessDenied):
            await acknowledge(case, candidate)
        entries, rows, _ = await observe_after_revocation(case)
        assert entries == before
        assert rows[-1] == case.unknown
        assert len(case.backend_calls) == 2 and Journal.verify_chain(before)


async def test_native_ack_known_red_resume_round_two_qa_git_and_late_actor_revocation(store, tmp_path, monkeypatch):
    async with native_case(store, tmp_path / "closed-loop", monkeypatch) as case:
        run_id = case.run["id"]
        first = await select(case)
        await acknowledge(case, first)
        await adopt(case, first)
        original_receipt = case.journal.sandbox_effect_receipt

        async def close_before_start(*args, effect, **kwargs):
            result = await original_receipt(*args, effect=effect, **kwargs)
            if effect == "create":
                execution = next(
                    row for row in await case.journal.sandbox_executions(run_id) if row["execution_id"] == args[2]
                )
                await case.engine.sandbox._cleanup_host(run_id, "implement", args[2], execution["request"])
                raise RuntimeError("Controlled native create completed without a start intent")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(case.journal, "sandbox_effect_receipt", close_before_start)
            await case.service.execute(run_id)
        rows = await case.journal.sandbox_executions(run_id)
        closed = copy.deepcopy(rows[-1])
        assert closed["ordinal"] == 3 and closed["status"] == "removed"
        assert set(closed["effects"]) == {"create", "cleanup"}
        renew(store, 4)
        adoption_id = uuid4().hex
        old = await select(case, adoption=adoption_id)
        await acknowledge(case, old)
        new = await select(case, execution=closed["execution_id"], adoption=adoption_id)
        assert new["classification"] == "closed_before_start" and new["start_intent_digest"] is None
        await acknowledge(case, new)
        await adopt(case, new)

        async def known_red(config, argv, *args, **kwargs):
            case.backend_calls.append(list(argv))
            public = case.engine.workspaces / run_id / "partial.py"
            raw, before = public.read_bytes(), public.stat()
            await asyncio.sleep(0.002)
            public.chmod(before.st_mode & 0o7777)
            after = public.stat()
            assert public.read_bytes() == raw
            assert (
                after.st_dev,
                after.st_ino,
                after.st_uid,
                after.st_gid,
                after.st_mode,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
            ) == (
                before.st_dev,
                before.st_ino,
                before.st_uid,
                before.st_gid,
                before.st_mode,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
            )
            assert after.st_ctime_ns != before.st_ctime_ns
            return ProcessResult(argv, 1, "Controlled known RED receipt; no unknown command result")

        case.backend.run = known_red
        await case.service.execute(run_id)
        resumed = await store.get_run(run_id)
        assert resumed["status"] == "queued" and resumed["verification_handoff"]["status"] == "completed"
        assert resumed["worker_response"]["source"] == "factory_verification"
        assert all(
            row["effects"]["start"]["evidence"]["exit_code"] == 1
            for row in (await case.journal.sandbox_executions(run_id))[3:]
        )
        case.backend.run = case.successful

        class ContinuedWorker:
            async def execute(self, workspace, brief, *args):
                current = await store.get_run(run_id)
                assert current["implementation_pending"] and current["implementation_rounds"] == 1
                result = await case.engine.sandbox.execute(
                    run_id,
                    workspace,
                    workspace,
                    ["authorized-round-two-tool"],
                    2,
                    "implement",
                    git_root=workspace,
                )
                assert result.exit_code == 0
                (workspace / "partial.py").write_text("VALUE = 2\n")
                return AgentResult("Controlled native continuation completed", "fixture", usage={})

        case.engine.provider_factory = lambda _: ContinuedWorker()
        await case.engine.execute_run(run_id)
        final = await store.get_run(run_id)
        assert final["status"] == "succeeded" and final["implementation_rounds"] == 2
        assert final["implementation_pending"] is False and len(final["final_verification"]) == 7
        assert all(check["exit_code"] == 0 for check in final["final_verification"])
        assert final["checkpoint_revision"] and final["publish"] is False
        entries = await case.journal.entries(run_id)
        assert Journal.verify_chain(entries)
        assert len([row for row in entries if row["kind"] == "verification_continuation_intent"]) == 1
        assert any(row["task_id"] == "verify" and row["kind"] == "receipt" for row in entries)
        assert any(row["task_id"] == "checkpoint" and row["kind"] == "receipt" for row in entries)
        rows = await case.journal.sandbox_executions(run_id)
        assert rows[1] == case.unknown and rows[2] == closed
        before_calls = len(case.backend_calls)
        with pytest.raises((ValueError, SandboxReconciliationRequired)):
            await case.journal.sandbox_command_replay(
                run_id,
                "implement",
                1,
                final["sandbox_snapshot"],
                lambda *args: {},
                ordinal=2,
            )
        assert len(case.backend_calls) == before_calls

    for phase in ("verification", "continuation"):
        async with native_case(store, tmp_path / ("late-" + phase), monkeypatch) as case:
            run_id = case.run["id"]
            candidate = await select(case)
            await acknowledge(case, candidate)
            await adopt(case, candidate)
            if phase == "continuation":
                case.backend.run = case.successful
                await case.service.execute(run_id)
                assert (await store.get_run(run_id))["status"] == "queued"

                class RevokedWorker:
                    async def execute(self, workspace, brief, *args):
                        await case.engine.sandbox.execute(
                            case.run["id"],
                            workspace,
                            workspace,
                            ["revoked-round-two-tool"],
                            2,
                            "implement",
                            git_root=workspace,
                        )
                        raise AssertionError("Revoked ACK actor reached the coding worker after dispatch")

                case.engine.provider_factory = lambda _: RevokedWorker()
            before_calls = len(case.backend_calls)
            original_receipt = case.journal.sandbox_effect_receipt
            revoked = False

            async def revoke_after_create(*args, effect, _receipt=original_receipt, **kwargs):
                nonlocal revoked
                result = await _receipt(*args, effect=effect, **kwargs)
                if effect == "create" and not revoked:
                    revoked = True
                    await disable(case)
                return result

            with monkeypatch.context() as patch:
                patch.setattr(case.journal, "sandbox_effect_receipt", revoke_after_create)
                if phase == "verification":
                    await case.service.execute(run_id)
                else:
                    await case.engine.execute_run(run_id)
            assert revoked and len(case.backend_calls) == before_calls
            _, rows, _ = await observe_after_revocation(case)
            fresh = rows[-1]
            assert fresh["parent_attempt"] == (1 if phase == "verification" else 2)
            assert fresh["ordinal"] == (3 if phase == "verification" else 1)
            assert fresh["effects"]["create"]["status"] == "done"
            assert fresh["effects"].get("start", {}).get("status") != "done"
            assert fresh["effects"]["cleanup"]["status"] == "done"
            await case.engine.sandbox._cleanup_host(run_id, "implement", fresh["execution_id"], fresh["request"])
            entries, rows, current = await observe_after_revocation(case)
            assert rows[-1]["effects"]["cleanup"]["status"] == "done"
            assert rows[1] == case.unknown
            assert current["status"] != "succeeded"
            assert not any(row["task_id"] == "checkpoint" and row["kind"] == "receipt" for row in entries)

    async with native_case(store, tmp_path / "late-local-git", monkeypatch) as case:
        run_id = case.run["id"]
        candidate = await select(case)
        await acknowledge(case, candidate)
        await adopt(case, candidate)
        case.backend.run = case.successful
        await case.service.execute(run_id)
        assert (await store.get_run(run_id))["status"] == "queued"
        original_commit = case.engine._checkpoint_commit
        before_head = await native_git(["git", "rev-parse", "HEAD"], case.workspace)
        revoked = False

        class FinalWorker:
            async def execute(self, workspace, brief, *args):
                (workspace / "partial.py").write_text("VALUE = 2\n")
                return AgentResult("Controlled continuation reaches final local Git boundary", "fixture", usage={})

        async def revoke_before_local_git(identifier, workspace):
            nonlocal revoked
            current = await store.get_run(identifier)
            assert len(current["final_verification"]) == 7
            assert all(check["exit_code"] == 0 for check in current["final_verification"])
            await disable(case)
            revoked = True
            await original_commit(identifier, workspace)

        case.engine.provider_factory = lambda _: FinalWorker()
        with monkeypatch.context() as patch:
            patch.setattr(case.engine, "_checkpoint_commit", revoke_before_local_git)
            await case.engine.execute_run(run_id)
        entries, rows, current = await observe_after_revocation(case)
        assert revoked and current["status"] != "succeeded"
        assert await native_git(["git", "rev-parse", "HEAD"], case.workspace) == before_head
        assert any(row["task_id"] == "verify" and row["kind"] == "receipt" for row in entries)
        assert not any(row["task_id"] == "checkpoint" and row["kind"] == "receipt" for row in entries)
        assert rows[1] == case.unknown
        assert Journal.verify_chain(entries)
