# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Transactional memory admission controls; physical custody is independently substituted here."""
# ruff: noqa: F811

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_checkpoint_mode_adoption_host import mode_case  # noqa: F401
from test_original_selection_proof_host import case as historical_case
from test_sandbox_command_ordinals_host import command_case  # noqa: F401

from firefly_forge.journal import digest
from firefly_forge.original_selection_models import OriginalSelectionReadmissionInput
from firefly_forge.original_selection_readmissions import OriginalSelectionReadmissions


@pytest.fixture
async def selection_case(mode_case, monkeypatch):
    from firefly_forge.model_profiles import ModelProfiles
    from firefly_forge.persistence import ForgeJournalEntity
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.task_routing import TaskRouting
    from firefly_forge.unknown_command_proof import continuation_scope
    from firefly_forge.verification_models import verification_source

    c = mode_case
    memory_unit = c.store._unit

    async def advisory_only(*args, **kwargs):
        return None

    @asynccontextmanager
    async def unit():
        async with memory_unit():
            yield SimpleNamespace(execute=advisory_only)

    c.store._unit = unit
    data = historical_case()
    run, task, entries = data["run"], data["task"], data["entries"]
    for name in ("commands", "sandbox_snapshot", "tool_policy_snapshot"):
        run[name] = copy.deepcopy(c.store.run.data[name])
    c.run_id = run["id"]
    c.store.run.id = UUID(c.run_id)
    c.store.run_records.rows = [c.store.run]
    c.store.run.status = "failed"
    run.update(project_id=uuid4().hex, repository_id=uuid4().hex, execution_actor=ProjectAccess.actor_snapshot(c.actor))
    c.workspace = c.engine.workspaces / c.run_id
    c.workspace.mkdir()
    (c.workspace / "public.py").write_text("PUBLIC = True\n")
    run["verification_handoff"]["source_digest"] = verification_source(c.workspace)
    mode = data["mode_receipt"]
    actor = {
        **ProjectAccess.actor_snapshot(c.actor),
        "identity_digest": digest({"issuer": c.actor.issuer, "subject": c.actor.subject}),
    }
    candidate = mode["candidate"]
    candidate.update(
        actor=actor,
        original_scope_digest=continuation_scope(run),
        source_digest=run["verification_handoff"]["source_digest"],
    )
    candidate["proof"]["historical_custody_digest"] = digest({"closures": []})
    candidate["proof"]["frozen_budget_digest"] = digest(
        {k: run.get(k) for k in ("repair_budget", "tool_policy_snapshot", "sandbox_snapshot", "commands")}
    )
    candidate["proof"]["digest"] = digest({k: v for k, v in candidate["proof"].items() if k != "digest"})
    candidate["digest"] = digest({k: v for k, v in candidate.items() if k != "digest"})
    mode["request"]["candidate_digest"] = candidate["digest"]
    previous = "0" * 64
    mode_intent = mode_record = None
    for entry in entries:
        if entry["kind"] == "checkpoint_mode_intent":
            entry["payload"] = {"candidate_digest": candidate["digest"], "request": mode["request"], "actor": actor}
        elif entry["kind"] == "checkpoint_mode_adoption":
            mode["adoption_intent_digest"] = mode_intent["digest"]
        elif entry["kind"] == "checkpoint_mode_claim":
            entry["payload"]["adoption_digest"] = mode_record["digest"]
        entry["previous_hash"] = previous
        entry["digest"] = digest({k: v for k, v in entry.items() if k != "digest"})
        previous = entry["digest"]
        if entry["kind"] == "checkpoint_mode_intent":
            mode_intent = entry
        elif entry["kind"] == "checkpoint_mode_adoption":
            mode_record = entry
    run["checkpoint_mode_adoption"]["record_digest"] = mode_record["digest"]
    task["checkpoint_mode_claim_digest"] = entries[-1]["digest"]
    c.store.run.data = run
    row = c.store.tasks.rows[0]
    row.run_id, row.status, row.attempt = UUID(c.run_id), "running", 2
    row.data = {k: v for k, v in task.items() if k not in {"id", "status", "attempt"}}
    c.store.records.rows = [
        ForgeJournalEntity(
            id=uuid4(),
            run_id=UUID(c.run_id),
            sequence=e["sequence"],
            task_id=e["task_id"],
            idempotency_key=e["idempotency_key"],
            kind=e["kind"],
            previous_hash=e["previous_hash"],
            digest=e["digest"],
            data=copy.deepcopy(e),
        )
        for e in entries
    ]
    c.store.sandboxes.rows.clear()
    cfg = c.store.configuration
    for revision, settings in (
        (run["config_revision"], data["original_settings"]),
        (mode["execution_config_revision"], data["ineffective_settings"]),
        (data["current_selection"]["config_revision"], data["current_settings"]),
    ):
        cfg._write(cfg.revisions / (revision + ".json"), settings)
    profiles = ModelProfiles(cfg)
    cfg._write(profiles.path, {data["current_selection"]["profile_id"]: data["current_profile"]})
    cfg._write(profiles.manifests / (data["current_selection"]["config_revision"] + ".json"), data["current_selection"])
    c.store.task_router = TaskRouting(profiles)
    cfg._write(c.store.task_router.path, {k: data["current_routing"][k] for k in ("revision", "bindings")})
    c.selection_service = OriginalSelectionReadmissions(c.store, c.journal, c.engine)
    c.selection_service.native._operator = c.service.native._operator
    c.selection_service.native._locked = c.service.native._locked

    async def custody(*args, **kwargs):
        return [], mode["candidate"]["verification_custody_digest"]

    c.selection_service.modes._command_custody = custody
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository

    async def identity(self, user_id, **kwargs):
        return SimpleNamespace(enabled=True, issuer=c.actor.issuer, subject=c.actor.subject)

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    return c


def readmission_request(candidate):
    fields = OriginalSelectionReadmissionInput.model_fields
    return OriginalSelectionReadmissionInput(
        version=1,
        candidate_digest=candidate["digest"],
        policy_digest=candidate["policy_digest"],
        operator_observation_digest="a" * 64,
        observation_provenance="native_operator_pre_dispatch_observation",
        **{name: True for name in fields if name.startswith("acknowledge_")},
    )


async def test_atomic_readmission_preserves_old_mode_claim_attempt_and_spent_history(selection_case):
    c = selection_case
    candidate = await c.selection_service.candidate(c.run_id)
    before = copy.deepcopy(c.store.run.data)
    task = copy.deepcopy(c.store.tasks.rows[0].__dict__)
    entries = await c.journal.entries(c.run_id)
    request_id = uuid4().hex
    request = readmission_request(candidate)
    record = await c.selection_service.request(c.run_id, request_id, request)
    assert c.store.run.status == "queued"
    for key in ("config_revision", "checkpoint_mode_adoption", "repair_budget", "implementation_rounds", "commands"):
        assert c.store.run.data.get(key) == before.get(key)
    assert c.store.tasks.rows[0].__dict__ == task
    assert (await c.journal.entries(c.run_id))[: len(entries)] == entries
    assert await c.selection_service.request(c.run_id, request_id, request) == record
    assert await c.selection_service.get(c.run_id, request_id) == record


async def test_atomic_receipt_failure_rolls_back_forward_intent_and_queue(selection_case, monkeypatch):
    c = selection_case
    candidate = await c.selection_service.candidate(c.run_id)
    before = copy.deepcopy((c.store.run, c.store.tasks.rows, c.store.records.rows))
    original = c.journal._append

    async def fail(run_id, task_id, key, kind, payload):
        if kind == "original_selection_readmission":
            raise RuntimeError("Controlled receipt failure")
        return await original(run_id, task_id, key, kind, payload)

    monkeypatch.setattr(c.journal, "_append", fail)
    with pytest.raises(RuntimeError, match="Controlled receipt"):
        await c.selection_service.request(c.run_id, uuid4().hex, readmission_request(candidate))
    assert c.store.run.__dict__ == before[0].__dict__
    assert [r.__dict__ for r in c.store.tasks.rows] == [r.__dict__ for r in before[1]]
    assert [r.data for r in c.store.records.rows] == [r.data for r in before[2]]


async def test_same_attempt_is_claimed_once_and_effective_connection_is_original(selection_case):
    from firefly_forge.checkpoint_mode_runtime import execution_settings, mode_adoption_scope, prepare_current_mode
    from firefly_forge.engine import Engine

    c = selection_case
    candidate = await c.selection_service.candidate(c.run_id)
    await c.selection_service.request(c.run_id, uuid4().hex, readmission_request(candidate))
    prepared = await prepare_current_mode(c.journal, c.run_id)
    engine = object.__new__(Engine)
    engine.store, engine.journal, engine.assert_ownership = c.store, c.journal, c.store.assert_ownership
    with mode_adoption_scope(prepared):
        settings = execution_settings(c.store.configuration, c.store.run.data)
        assert digest(settings) == candidate["proof"]["current_settings_digest"]
        claim = await engine._begin_task(c.run_id, "implement", c.workspace, {})
        assert claim["disposition"] == "execute" and claim["idempotency_key"] == candidate["proof"]["task_key"]
        again = await c.journal.intent(c.run_id, "implement", 2)
        assert again["disposition"] == "needs_reconciliation"
    rows = await c.journal.entries(c.run_id)
    assert len([r for r in rows if r["kind"] == "checkpoint_mode_claim"]) == 1
    assert len([r for r in rows if r["kind"] == "original_selection_claim"]) == 1
    assert c.store.tasks.rows[0].attempt == 2


async def test_new_verification_provider_cut_uses_prepared_effective_connection(selection_case):
    from firefly_forge.checkpoint_mode_runtime import mode_adoption_scope, prepare_current_mode
    from firefly_forge.verification_models import verification_provider

    c = selection_case
    candidate = await c.selection_service.candidate(c.run_id)
    await c.selection_service.request(c.run_id, uuid4().hex, readmission_request(candidate))
    prepared = await prepare_current_mode(c.journal, c.run_id)
    with mode_adoption_scope(prepared):
        settings = prepared.selection.selected_settings
        assert verification_provider(c.store, c.store.run.data, "bridge") == digest(
            {"agent": "bridge", "settings": settings}
        )


async def test_later_verification_candidate_prepares_effective_selection_authority(selection_case):
    from firefly_forge.verification_handoffs import VerificationHandoffs

    c = selection_case
    candidate = await c.selection_service.candidate(c.run_id)
    await c.selection_service.request(c.run_id, uuid4().hex, readmission_request(candidate))
    c.store.run.status = "paused"
    c.store.run.data.update(
        status="paused",
        worker_request={
            "id": "later-native-verification",
            "kind": "verification",
            "status": "pending",
            "task_id": "implement",
            "attempt": 2,
        },
    )
    result = await VerificationHandoffs(c.store, c.journal, c.engine).candidates(c.run_id, "bridge")
    selected = c.store.configuration.load_snapshot(candidate["execution_config_revision"])
    assert result["provider_digest"] == digest({"agent": "bridge", "settings": selected})


async def test_later_factory_feedback_adopts_executes_and_finishes_its_own_verification(selection_case):
    import asyncio

    from firefly_forge.agent_roles import WorkerFeedback
    from firefly_forge.checkpoint_mode_runtime import mode_adoption_scope, prepare_current_mode
    from firefly_forge.engine import Engine
    from firefly_forge.process import ProcessResult
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.verification_handoffs import VerificationHandoffs
    from firefly_forge.verification_models import VerificationAdoption

    c = selection_case
    original_revision = c.store.run.data["config_revision"]
    candidate = await c.selection_service.candidate(c.run_id)
    await c.selection_service.request(c.run_id, uuid4().hex, readmission_request(candidate))
    prepared = await prepare_current_mode(c.journal, c.run_id)
    engine = object.__new__(Engine)
    engine.store, engine.journal, engine.assert_ownership = c.store, c.journal, c.store.assert_ownership
    with mode_adoption_scope(prepared):
        await engine._begin_task(c.run_id, "implement", c.workspace, {})
    c.store.run.status = "developing"
    c.store.run.data.update(status="developing", implementation_rounds=2, implementation_pending=False)
    await c.journal.worker_feedback(
        c.run_id,
        WorkerFeedback.model_validate(
            {
                "id": "later-feedback",
                "role_id": "implementer",
                "task_id": "implement",
                "attempt": 2,
                "disposition": "blocked",
                "summary": "Run the frozen checks again",
                "request": {
                    "id": "later-factory-request",
                    "kind": "verification",
                    "question": "Run the approved checks?",
                    "context": "The existing native continuation and budgets",
                },
            }
        ),
    )
    service = VerificationHandoffs(c.store, c.journal, c.engine)
    value = await service.candidates(c.run_id, "bridge")
    request = VerificationAdoption(
        request_id=uuid4().hex,
        **{
            k: value[k]
            for k in (
                "pending_request_digest",
                "source_digest",
                "commands_digest",
                "policy_digest",
                "provider_digest",
                "agent",
            )
        },
    )
    await service.adopt(c.run_id, request)
    calls = []

    class Backend:
        async def run(self, configuration, argv, cwd, timeout, limit):
            calls.append(argv)
            return ProcessResult(argv, 1, "Known controlled RED")

    c.engine.publication_lock = asyncio.Lock()
    c.engine.assert_ownership = c.store.assert_ownership
    c.engine.component_directory = lambda run, workspace: workspace
    c.engine.sandbox = SandboxExecutor(c.store, c.journal, SimpleNamespace(select=lambda _: Backend()))
    await service.execute(c.run_id)
    current = await c.store.get_run(c.run_id)
    assert current["status"] == "queued", current.get("verification_refusal")
    assert len(calls) == len(current["commands"])
    assert current["verification_handoff"]["status"] == "completed"
    assert current["verification_handoff"]["failed_commands"] == len(calls)
    assert current["config_revision"] == original_revision
    assert current["repair_budget"] == 2 and c.store.tasks.rows[0].attempt == 3


@pytest.mark.parametrize("drift", ["profile", "source", "actor"])
async def test_stale_candidate_refuses_without_forward_effects(selection_case, drift):
    from firefly_forge.project_access import AccessDenied

    c = selection_case
    candidate = await c.selection_service.candidate(c.run_id)
    before = await c.journal.entries(c.run_id)
    if drift == "profile":
        profiles = c.store.task_router.profiles
        raw = c.store.configuration._read(profiles.path)
        raw[candidate["profile_id"]]["revision"] += 1
        c.store.configuration._write(profiles.path, raw)
    elif drift == "source":
        (c.workspace / "public.py").write_text("PUBLIC = False\n")
    else:
        c.admin = False
    with pytest.raises((ValueError, AccessDenied)):
        await c.selection_service.request(c.run_id, uuid4().hex, readmission_request(candidate))
    c.admin = True
    assert await c.journal.entries(c.run_id) == before
    assert c.store.run.status == "failed"


async def test_competing_readmission_requests_have_one_winner(selection_case):
    import asyncio

    c = selection_case
    candidate = await c.selection_service.candidate(c.run_id)
    results = await asyncio.gather(
        *(c.selection_service.request(c.run_id, uuid4().hex, readmission_request(candidate)) for _ in range(2)),
        return_exceptions=True,
    )
    assert len([x for x in results if isinstance(x, dict)]) == 1
    assert len([x for x in await c.journal.entries(c.run_id) if x["kind"] == "original_selection_readmission"]) == 1


async def test_late_actor_refusal_after_receipt_rolls_back_the_whole_admission(selection_case, monkeypatch):
    from firefly_forge.project_access import AccessDenied

    c = selection_case
    candidate = await c.selection_service.candidate(c.run_id)
    before = await c.journal.entries(c.run_id)
    save = c.store.run_records.save

    async def revoke(row):
        await save(row)
        c.admin = False

    monkeypatch.setattr(c.store.run_records, "save", revoke)
    with pytest.raises(AccessDenied):
        await c.selection_service.request(c.run_id, uuid4().hex, readmission_request(candidate))
    c.admin = True
    assert await c.journal.entries(c.run_id) == before
    assert c.store.run.status == "failed"


async def test_configuration_reads_stay_outside_native_admission_and_claim_units(selection_case, monkeypatch):
    from firefly_forge.checkpoint_mode_runtime import mode_adoption_scope, prepare_current_mode
    from firefly_forge.engine import Engine

    c = selection_case
    original = c.store.configuration.load_snapshot

    def outside(revision):
        assert not c.store.depth.get(), "Configuration I/O entered a native writer"
        return original(revision)

    monkeypatch.setattr(c.store.configuration, "load_snapshot", outside)
    candidate = await c.selection_service.candidate(c.run_id)
    await c.selection_service.request(c.run_id, uuid4().hex, readmission_request(candidate))
    prepared = await prepare_current_mode(c.journal, c.run_id)
    engine = object.__new__(Engine)
    engine.store, engine.journal, engine.assert_ownership = c.store, c.journal, c.store.assert_ownership
    with mode_adoption_scope(prepared):
        result = await engine._begin_task(c.run_id, "implement", c.workspace, {})
    assert result["disposition"] == "execute"
