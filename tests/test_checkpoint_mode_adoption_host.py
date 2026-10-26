# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Journal/Engine with explicit transactional memory and ACL substitutes, not SQL proof."""

# ruff: noqa: F811
import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from test_checkpoint_mode_adoption_proof import case as proof_case
from test_sandbox_command_ordinals_host import command_case as command_case

from firefly_forge.checkpoint_mode_adoptions import CheckpointModeAdoptions
from firefly_forge.checkpoint_mode_models import CheckpointModeAdoptionInput
from firefly_forge.checkpoint_mode_runtime import mode_adoption_scope, prepare_current_mode
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.store import Store


@pytest.fixture
async def mode_case(command_case, tmp_path, monkeypatch):
    c = command_case
    c.allowed = True
    c.admin = True
    oldrun, oldtask, entries, settings = proof_case()
    run_id = c.run_id
    oldrun.update(id=run_id, project_id=uuid4().hex, repository_id=uuid4().hex)
    key = uuid5(NAMESPACE_URL, "forge:" + run_id + ":implement:2").hex
    oldtask["idempotency_key"] = key
    c.store.run.status = "failed"
    c.store.run.data = oldrun
    row = c.store.tasks.rows[0]
    row.task_id = "implement"
    row.status = "running"
    row.attempt = 2
    row.data = {k: v for k, v in oldtask.items() if k not in {"id", "status", "attempt"}}
    c.store.configuration = ConfigurationStore(tmp_path / "configuration")
    c.store.configuration._write(c.store.configuration.revisions / (oldrun["config_revision"] + ".json"), settings)
    c.store.configuration._write(
        c.store.configuration.path,
        {
            "bridge": settings
            | {"FORGE_AGENTIC_CHECKPOINTS": "required", "FORGE_BRIDGE_COMPLETION_RECOVERY": "required"}
        },
    )
    c.actor = Principal(
        uuid4(), "https://identity.example.test", "controlled-operator", authentication_method="local_bootstrap"
    )
    oldrun["execution_actor"] = ProjectAccess.actor_snapshot(c.actor)
    token = CURRENT_PRINCIPAL.set(c.actor)
    c.workspace = tmp_path / "workspaces" / run_id
    c.workspace.mkdir(parents=True)
    (c.workspace / "public.py").write_text("PUBLIC = True\n")
    c.store.credential_registry = dict
    c.store.reject_credentials = Store.reject_credentials.__get__(c.store)

    async def admit(value):
        c.store.reject_credentials(value)

    c.store.admit_credentials = admit
    c.engine = SimpleNamespace(workspaces=c.workspace.parent, active={}, tool_broker=None)
    c.service = CheckpointModeAdoptions(c.store, c.journal, c.engine)

    async def authorize(*args, **kwargs):
        if not c.allowed:
            raise AccessDenied("Controlled current authority revoked")
        return True

    async def operator(run):
        if not c.admin or CURRENT_PRINCIPAL.get() != c.actor:
            raise AccessDenied("Controlled native administrator revoked")
        return c.actor

    async def access(run, actor):
        await operator(run)
        await authorize()
        return await c.store.get_run(run)

    @asynccontextmanager
    async def unit():
        if c.store.depth.get():
            yield None
            return
        async with c.store.lock:
            mark = c.store.depth.set(True)
            before = copy.deepcopy((c.store.run, c.store.tasks.rows, c.store.records.rows, c.store.sandboxes.rows))
            try:
                yield None
            except BaseException:
                c.store.run, c.store.tasks.rows, c.store.records.rows, c.store.sandboxes.rows = before
                c.store.run_records.rows = [c.store.run]
                raise
            finally:
                c.store.depth.reset(mark)

    c.store._unit = unit
    import firefly_forge.store as store_module

    monkeypatch.setattr(store_module, "current_unit_of_work", lambda: object() if c.store.depth.get() else None)
    c.store.assert_campaign_current = authorize

    @asynccontextmanager
    async def locked(run, actor):
        async with unit():
            await access(run, c.actor)
            yield await c.journal._run(run)

    c.service._access = access
    c.service.native._operator = operator
    c.service.native._locked = locked
    monkeypatch.setattr(ProjectAccess, "authorize_actor", authorize)
    monkeypatch.setattr(ProjectAccess, "authorize_execution", authorize)
    from firefly_forge.bridge_completions import BridgeCompletions

    async def identity(self, user_id):
        return SimpleNamespace(issuer=c.actor.issuer, subject=c.actor.subject)

    monkeypatch.setattr(BridgeCompletions, "_identity", identity)

    async def plan(run):
        return {
            "tasks": [
                {**copy.deepcopy(x.data), "id": x.task_id, "status": x.status, "attempt": x.attempt}
                for x in c.store.tasks.rows
            ]
        }

    c.journal.ensure_plan = plan
    c.journal.plan = plan
    from checkpoint_mode_fixture import missing_mode_failure

    await missing_mode_failure(c.store, c.journal, c.workspace, oldrun["config_revision"], settings)
    try:
        yield c
    finally:
        CURRENT_PRINCIPAL.reset(token)


def request(candidate):
    return CheckpointModeAdoptionInput(
        version=1,
        candidate_digest=candidate["digest"],
        policy_digest=candidate["policy_digest"],
        operator_observation_digest="d" * 64,
        observation_provenance="native_operator_pre_dispatch_observation",
        acknowledge_known_pre_dispatch_failure=True,
        acknowledge_no_external_model_dispatch=True,
        acknowledge_original_intents_not_completed=True,
        acknowledge_only_stronger_modes=True,
        acknowledge_spent_history_and_budgets_preserved=True,
        acknowledge_same_native_continuation=True,
        acknowledge_existing_unknown_command_risk=True,
    )


async def test_native_adoption_queues_same_task_preserves_original_settings_and_duplicate(mode_case):
    c = mode_case
    candidate = await c.service.candidate(c.run_id)
    before = await c.journal.entries(c.run_id)
    old = copy.deepcopy(c.store.run.data)
    task = copy.deepcopy(c.store.tasks.rows[0])
    identifier = uuid4().hex
    record = await c.service.request(c.run_id, identifier, request(candidate))
    assert record["kind"] == "checkpoint_mode_adoption"
    assert c.store.run.status == "queued" and c.store.run.data["config_revision"] == old["config_revision"]
    assert c.store.configuration.load_snapshot(old["config_revision"]).get("FORGE_AGENTIC_CHECKPOINTS") is None
    assert c.store.tasks.rows[0].__dict__ == task.__dict__
    assert (await c.journal.entries(c.run_id))[: len(before)] == before
    assert await c.service.get(c.run_id, identifier) == record
    assert await c.service.request(c.run_id, identifier, request(candidate)) == record
    assert len(await c.journal.entries(c.run_id)) == len(before) + 2


async def test_real_begin_task_claims_old_intent_once_without_new_attempt_or_fake_receipt(mode_case):
    from firefly_forge.engine import Engine

    c = mode_case
    candidate = await c.service.candidate(c.run_id)
    await c.service.request(c.run_id, uuid4().hex, request(candidate))
    prepared = await prepare_current_mode(c.journal, c.run_id)
    engine = object.__new__(Engine)
    engine.store = c.store
    engine.journal = c.journal
    engine.assert_ownership = c.store.assert_ownership
    with mode_adoption_scope(prepared):
        begun = await engine._begin_task(c.run_id, "implement", c.workspace, {})
        assert begun["disposition"] == "execute" and begun["idempotency_key"] == candidate["proof"]["task_key"]
        assert c.store.tasks.rows[0].attempt == 2
        await c.journal.verification_provider_admission(c.run_id)
        again = await c.journal.intent(c.run_id, "implement", 2)
        assert again["disposition"] == "needs_reconciliation"
    entries = await c.journal.entries(c.run_id)
    assert len([e for e in entries if e["kind"] == "intent"]) == 2
    assert (
        len([e for e in entries if e["kind"] == "intent" and e["idempotency_key"] == candidate["proof"]["task_key"]])
        == 1
    )
    assert len([e for e in entries if e["kind"] == "verification_continuation_intent"]) == 1
    assert not any(e["kind"] == "receipt" for e in entries)


async def test_atomic_wal_failure_retains_original_task_run_and_no_active_revision(mode_case, monkeypatch):
    c = mode_case
    candidate = await c.service.candidate(c.run_id)
    before = copy.deepcopy((c.store.run, c.store.tasks.rows, await c.journal.entries(c.run_id)))
    original = c.journal._append

    async def fail(run, task, key, kind, payload):
        if kind == "checkpoint_mode_adoption":
            raise RuntimeError("controlled receipt rollback")
        return await original(run, task, key, kind, payload)

    monkeypatch.setattr(c.journal, "_append", fail)
    with pytest.raises(RuntimeError, match="controlled receipt rollback"):
        await c.service.request(c.run_id, uuid4().hex, request(candidate))
    assert c.store.run.__dict__ == before[0].__dict__
    assert [x.__dict__ for x in c.store.tasks.rows] == [x.__dict__ for x in before[1]]
    assert await c.journal.entries(c.run_id) == before[2]
    assert "checkpoint_mode_adoption" not in c.store.run.data
    assert len(list(c.store.configuration.revisions.glob("*.json"))) == 2


async def test_competing_native_requests_commit_one_adoption(mode_case):
    c = mode_case
    candidate = await c.service.candidate(c.run_id)
    results = await asyncio.gather(
        *(c.service.request(c.run_id, uuid4().hex, request(candidate)) for _ in range(2)), return_exceptions=True
    )
    assert sum(isinstance(x, dict) for x in results) == 1
    assert sum(isinstance(x, (ValueError, AccessDenied)) for x in results) == 1
    assert len([x for x in await c.journal.entries(c.run_id) if x["kind"] == "checkpoint_mode_adoption"]) == 1


@pytest.mark.parametrize("change", ["source", "budget", "provider", "operator"])
async def test_stale_or_revoked_candidate_never_queues(mode_case, change):
    c = mode_case
    candidate = await c.service.candidate(c.run_id)
    if change == "source":
        (c.workspace / "public.py").write_text("CHANGED = True\n")
    elif change == "budget":
        c.store.run.data["repair_budget"] += 1
    elif change == "operator":
        c.admin = False
    else:
        c.store.configuration.update(
            "bridge",
            __import__("firefly_forge.configuration", fromlist=["ProviderConfiguration"]).ProviderConfiguration(
                values={"model": "different/model"}
            ),
        )
    before = await c.journal.entries(c.run_id)
    with pytest.raises((ValueError, AccessDenied)):
        await c.service.request(c.run_id, uuid4().hex, request(candidate))
    assert c.store.run.status == "failed" and await c.journal.entries(c.run_id) == before


async def test_new_mode_configuration_io_never_occurs_in_the_native_writer(mode_case, monkeypatch):
    from firefly_forge.engine import Engine

    c = mode_case
    for name in ["load_snapshot", "resolve", "snapshot"]:
        original = getattr(c.store.configuration, name)

        def guarded(*args, __original=original, **kwargs):
            assert not c.store.depth.get(), "Configuration IO crossed the native writer"
            return __original(*args, **kwargs)

        monkeypatch.setattr(c.store.configuration, name, guarded)
    candidate = await c.service.candidate(c.run_id)
    await c.service.request(c.run_id, uuid4().hex, request(candidate))
    prepared = await prepare_current_mode(c.journal, c.run_id)
    engine = object.__new__(Engine)
    engine.store = c.store
    engine.journal = c.journal
    engine.assert_ownership = c.store.assert_ownership
    with mode_adoption_scope(prepared):
        assert (await engine._begin_task(c.run_id, "implement", c.workspace, {}))["disposition"] == "execute"
        await c.journal.verification_provider_admission(c.run_id)
