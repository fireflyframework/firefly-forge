# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real command WAL and acknowledgement service with explicit memory/ACL substitutes."""
# ruff: noqa: F811

import asyncio
import copy
import importlib
import importlib.util
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_sandbox_command_ordinals_host import command_case  # noqa: F401

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.sandbox import SandboxReconciliationRequired
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.store import Store
from firefly_forge.tool_broker import FactoryToolPolicyInput, tool_policy_snapshot
from firefly_forge.verification_handoffs import VerificationHandoffs, verification_intent


@pytest.fixture
async def unknown_case(command_case, monkeypatch, tmp_path):
    case = command_case
    case.allowed = True

    async def access(*args, **kwargs):
        if not case.allowed:
            raise AccessDenied("Controlled operator permission revoked")
        return True

    monkeypatch.setattr(ProjectAccess, "authorize_execution", access)
    monkeypatch.setattr(ProjectAccess, "authorize_actor", access)
    case.journal._worker_authority = access
    case.store.assert_campaign_current = access
    case.store.configuration = ConfigurationStore(tmp_path / "configuration")
    policy = FactoryToolPolicyInput(automatic_verification={"enabled": True, "max_continuations": 3})
    case.store.configuration._write(
        case.store.configuration.directory / "tool-policy.json",
        {"revision": uuid4().hex, "settings": policy.model_dump()},
    )
    case.store.run.data.update(
        id=case.run_id,
        status="developing",
        agent="agentic",
        active_task="implement",
        project_id=uuid4().hex,
        repository_id=uuid4().hex,
        commands=[{"argv": ["fixture-check"], "timeout": 2}],
        sandbox_snapshot=capture_sandbox(SandboxInput(mode="host", command_limit=22)),
        tool_policy_snapshot=tool_policy_snapshot(case.store.configuration),
        implementation_rounds=1,
    )
    case.store.run.data.update(case.store.configuration.snapshot("agentic"))
    case.store.run.status = "developing"
    case.store.tasks.rows[0].task_id = "implement"
    case.workspace = tmp_path / "workspaces" / case.run_id
    case.workspace.mkdir(parents=True)
    (case.workspace / "public.py").write_text("PUBLIC = True\n")
    await case.fresh().execute(case.run_id, case.workspace, case.workspace, ["fixture-before"], 2, "implement")
    case.successful_backend = case.backend.run

    async def unknown(*args):
        raise RuntimeError("Controlled unknown original command outcome")

    case.backend.run = unknown
    with pytest.raises(RuntimeError, match="unknown original"):
        await case.fresh().execute(case.run_id, case.workspace, case.workspace, ["fixture-unknown"], 2, "implement")
    case.unknown = case.store.sandboxes.rows[-1]
    assert case.unknown.ordinal == 2 and case.unknown.status == "removed"
    assert case.unknown.data["effects"]["start"] == {"status": "intent"}
    case.store.run.data.update(
        status="paused",
        worker_request={
            "id": "fixture-verification",
            "kind": "verification",
            "status": "pending",
            "task_id": "implement",
            "attempt": 1,
        },
    )
    case.store.run.status = "paused"
    case.store.tasks.rows[0].status = "paused"
    case.engine = SimpleNamespace(workspaces=case.workspace.parent, publication_lock=asyncio.Lock())
    case.handoffs = VerificationHandoffs(case.store, case.journal, case.engine)
    for _ in range(2):
        run = await case.store.get_run(case.run_id)
        temporary = {**run, "verification_handoff": {}}
        intent = verification_intent(
            case.store,
            temporary,
            case.workspace,
            await case.journal.entries(case.run_id),
            await case.journal.sandbox_executions(case.run_id),
        )
        await case.journal._append(case.run_id, "implement", intent["id"], "verification_handoff_intent", intent)
        await case.journal._append(
            case.run_id,
            "implement",
            intent["id"] + ":held",
            "verification_handoff_outcome",
            {"id": intent["id"], "status": "held"},
        )
        case.store.run.data["verification_handoff"] = {**intent, "status": "held"}
    case.store.credential_registry = dict
    case.store.reject_credentials = Store.reject_credentials.__get__(case.store)

    async def admit(value):
        case.store.reject_credentials(value)

    case.store.admit_credentials = admit
    case.principal = Principal(uuid4(), "fixture", "operator", authentication_method="local_bootstrap")
    case.store.run.data["execution_actor"] = ProjectAccess.actor_snapshot(case.principal)
    case.actor_token = CURRENT_PRINCIPAL.set(case.principal)
    try:
        yield case
    finally:
        CURRENT_PRINCIPAL.reset(case.actor_token)


def service(case, monkeypatch):
    assert importlib.util.find_spec("firefly_forge.unknown_command_acknowledgements") is not None, (
        "Native unknown-command acknowledgement service is missing"
    )
    module = importlib.import_module("firefly_forge.unknown_command_acknowledgements")
    api = module.UnknownCommandAcknowledgements(case.store, case.journal, case.handoffs)

    async def access(run_id, actor, **kwargs):
        assert run_id == case.run_id
        if CURRENT_PRINCIPAL.get() != actor or not case.allowed:
            raise AccessDenied("Controlled current native operator changed")
        await case.store.assert_ownership()
        return await case.store.get_run(run_id)

    async def writer(*args):
        assert case.store.depth.get(), "Acknowledgement must use the native writer unit"

    monkeypatch.setattr(api.native, "_access", access)
    monkeypatch.setattr(api.native.completions, "_reader", access)
    monkeypatch.setattr(api.native.completions, "_writer", writer)
    return api


def consent(candidate):
    from firefly_forge.unknown_command_models import UnknownCommandAcknowledgementV1

    return UnknownCommandAcknowledgementV1(
        version=1,
        candidate_digest=candidate["digest"],
        policy_digest=candidate["policy_digest"],
        expected_run_digest=candidate["run_digest"],
        journal_digest=candidate["journal_digest"],
        execution_digest=candidate["execution_digest"],
        classification=candidate["classification"],
        followup=candidate["followup"],
        **(
            {"acknowledge_result_unknown": True, "acknowledge_effects_may_have_occurred": True}
            if candidate["classification"] == "start_unknown"
            else {"acknowledge_no_start_intent": True}
        ),
        acknowledge_no_original_replay=True,
        acknowledge_bound_verification_and_continuation=True,
    )


async def test_acknowledgement_appends_once_without_settling_or_replaying_old_command(unknown_case, monkeypatch):
    case = unknown_case
    api = service(case, monkeypatch)
    before = copy.deepcopy((case.store.run.data, case.unknown.data, case.unknown.status))
    candidate = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    request_id = uuid4().hex
    receipt = await api.request(case.run_id, case.unknown.id.hex, request_id, consent(candidate))
    assert receipt["kind"] == "unknown_command_acknowledgement"
    assert receipt["payload"]["disposition"] == "unknown_abandoned_for_verification"
    assert receipt["payload"]["followup"]["continuation"] == 3
    assert receipt["payload"]["followup"]["ordinals"] == [3]
    assert (case.store.run.data, case.unknown.data, case.unknown.status) == before
    assert await api.request(case.run_id, case.unknown.id.hex, request_id, consent(candidate)) == receipt
    assert len([row for row in case.store.records.rows if row.kind == receipt["kind"]]) == 1
    assert len(case.calls) == 1


async def test_spent_current_policy_is_not_reset_or_inherited(unknown_case, monkeypatch):
    case = unknown_case
    api = service(case, monkeypatch)
    policy = FactoryToolPolicyInput(automatic_verification={"enabled": True, "max_continuations": 2})
    case.store.configuration._write(
        case.store.configuration.directory / "tool-policy.json",
        {"revision": uuid4().hex, "settings": policy.model_dump()},
    )
    with pytest.raises(ValueError, match="budget|exhaust"):
        await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    assert len(case.store.sandboxes.rows) == 2
    assert case.unknown.data["effects"]["start"] == {"status": "intent"}


async def adopt_and_begin(case, candidate, monkeypatch):
    from contextlib import asynccontextmanager

    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.verification_models import VerificationAdoption, check_verification_cut

    async def execute(*args, **kwargs):
        pass

    async def identity(*args, **kwargs):
        return SimpleNamespace(enabled=case.allowed, issuer=case.principal.issuer, subject=case.principal.subject)

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    original = case.store._unit

    @asynccontextmanager
    async def unit():
        async with original():
            yield SimpleNamespace(execute=execute)

    case.store._unit = unit
    followup = candidate["followup"]
    body = VerificationAdoption.model_validate(
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
    )
    await case.handoffs.adopt(case.run_id, body)
    run = await case.store.get_run(case.run_id)
    intent = verification_intent(
        case.store,
        run,
        case.workspace,
        await case.journal.entries(case.run_id),
        await case.journal.sandbox_executions(case.run_id),
    )
    await case.journal.verification_begin(
        case.run_id, intent, check_current=lambda current: check_verification_cut(case.store, current, intent)
    )
    return intent


async def test_only_acknowledged_new_verification_ordinals_can_pass_old_unknown_row(unknown_case, monkeypatch):
    case = unknown_case
    api = service(case, monkeypatch)
    candidate = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    await api.request(case.run_id, case.unknown.id.hex, uuid4().hex, consent(candidate))
    intent = await adopt_and_begin(case, candidate, monkeypatch)
    assert intent["continuation"] == 3 and intent["ordinals"] == [3]
    old = copy.deepcopy((case.unknown.data, case.unknown.status))
    case.backend.run = case.successful_backend
    result = await case.fresh().execute(
        case.run_id, case.workspace, case.workspace, ["fixture-check"], 2, "implement", ordinal=3
    )
    assert result.exit_code == 0 and len(case.calls) == 2
    assert [row.ordinal for row in case.store.sandboxes.rows] == [1, 2, 3]
    assert (case.unknown.data, case.unknown.status) == old
    with pytest.raises((ValueError, SandboxReconciliationRequired)):
        await case.fresh().execute(
            case.run_id, case.workspace, case.workspace, ["fixture-unknown"], 2, "implement", ordinal=2
        )
    assert len(case.calls) == 2


@pytest.mark.parametrize("repair_budget", [0, 1])
async def test_completed_known_red_handoff_binds_native_continuation_and_frozen_grants(
    unknown_case, monkeypatch, repair_budget
):
    from firefly_forge.process import ProcessResult
    from firefly_forge.unknown_command_proof import acknowledged_continuation

    case = unknown_case
    case.store.run.data["repair_budget"] = repair_budget
    api = service(case, monkeypatch)
    candidate = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    await api.request(case.run_id, case.unknown.id.hex, uuid4().hex, consent(candidate))
    intent = await adopt_and_begin(case, candidate, monkeypatch)

    async def known_red(config, argv, *args):
        return ProcessResult(argv, 1, "Known RED; original result remains unknown")

    case.backend.run = known_red
    await case.fresh().execute(
        case.run_id, case.workspace, case.workspace, ["fixture-check"], 2, "implement", ordinal=3
    )

    def check(run):
        return None

    await case.journal.verification_command(case.run_id, intent, 0, check_current=check)
    await case.journal.verification_finish(case.run_id, intent, check_current=check)
    # Native resume/provider producers are qualified in the separate PostgreSQL cohort.
    await case.journal._append(
        case.run_id,
        "handoff",
        uuid4().hex,
        "handoff",
        {
            "automatic": True,
            "verification_handoff_id": intent["id"],
            "agent": "agentic",
            "config_revision": case.store.run.data["config_revision"],
        },
    )
    task = case.store.tasks.rows[0]
    task.status, task.attempt = "queued", 2
    case.store.run.data.update(attempt=2, implementation_pending=True)

    async def ensure_plan(*args):
        return {}

    monkeypatch.setattr(case.journal, "ensure_plan", ensure_plan)
    await case.journal.intent(case.run_id, "implement", 2)
    await case.journal.verification_provider_admission(case.run_id)
    case.backend.run = case.successful_backend
    from firefly_forge.sandbox_command_slots import _canonical
    from firefly_forge.unknown_command_acknowledgements import policy_digest

    entries = await case.journal.entries(case.run_id)
    run = await case.store.get_run(case.run_id)
    rows = case.store.sandboxes.rows
    values = {row.id.hex: _canonical(row, entries, case.run_id) for row in rows}
    assert acknowledged_continuation(case.unknown, values, rows, entries, run, [task], "implement", policy_digest())
    result = await case.fresh().execute(
        case.run_id, case.workspace, case.workspace, ["next-round-tool"], 2, "implement"
    )
    assert result.exit_code == 0
    for changed in (
        {"repair_budget": 17},
        {"id": uuid4().hex},
        {"attempt": 3},
        {"agent": "codex"},
        {"branding_snapshot": {"digest": "changed"}},
        {"product_definition": {"definition": "changed"}},
    ):
        assert (
            acknowledged_continuation(
                case.unknown, values, rows, entries, {**run, **changed}, [task], "implement", policy_digest()
            )
            is None
        )
    await case.journal.receipt(case.run_id, task.data["idempotency_key"], {"implementation_round": 2})
    case.store.run.data.update(implementation_rounds=2, implementation_pending=False)
    verify = SimpleNamespace(
        id=uuid4(), run_id=case.store.run.id, task_id="verify", attempt=1, status="queued", data={}
    )
    case.store.tasks.rows.append(verify)
    await case.journal.intent(case.run_id, "verify", 1)
    qa = await case.fresh().execute(case.run_id, case.workspace, case.workspace, ["normal-qa"], 2, "verify")
    assert qa.exit_code == 0
    from firefly_forge.unknown_command_proof import assert_continuation_current, continuation_policy_scope

    assert await assert_continuation_current(case.journal, case.run_id) == [case.unknown.id.hex]
    with continuation_policy_scope(case.run_id):
        assert await assert_continuation_current(case.journal, case.run_id) == [case.unknown.id.hex]
    case.allowed = False
    with pytest.raises(AccessDenied):
        await assert_continuation_current(case.journal, case.run_id)
    case.allowed = True
    failed = [{"argv": ["normal-qa"], "exit_code": 1, "output": "Known failure"}]
    await case.journal.receipt(
        case.run_id, verify.data["idempotency_key"], {"implementation_round": 2, "failed_checks": failed}
    )
    await case.journal.intent(case.run_id, "implement", 3, {"reason": "verification_repair", "failed_checks": failed})
    case.store.run.data.update(attempt=3, implementation_pending=True)
    if repair_budget:
        assert (
            await case.fresh().execute(case.run_id, case.workspace, case.workspace, ["granted-repair"], 2, "implement")
        ).exit_code == 0
    else:
        before = len(case.calls)
        with pytest.raises(ValueError, match="custody acknowledgement"):
            await case.fresh().execute(case.run_id, case.workspace, case.workspace, ["no-new-budget"], 2, "implement")
        assert len(case.calls) == before


@pytest.mark.parametrize("change", ["actor", "permission", "lease", "source", "journal", "task"])
async def test_candidate_wait_cannot_admit_changed_authority_source_or_custody(unknown_case, monkeypatch, change):
    case = unknown_case
    api = service(case, monkeypatch)
    original = case.store.admit_credentials
    old_execution = copy.deepcopy(case.unknown.data)

    async def changed(value):
        await original(value)
        if not isinstance(value, dict) or "execution_digest" not in value:
            return
        if change == "actor":
            CURRENT_PRINCIPAL.set(None)
        elif change == "permission":
            case.allowed = False
        elif change == "lease":
            case.store.owned = False
        elif change == "source":
            (case.workspace / "public.py").write_text("PUBLIC = False\n")
        elif change == "journal":
            await case.journal._append(case.run_id, "implement", "independent", "observation", {})
        else:
            case.store.tasks.rows[0].data["idempotency_key"] = "changed-after-preparation"

    case.store.admit_credentials = changed
    with pytest.raises((AccessDenied, ValueError, RuntimeError)):
        await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    assert not any(row.kind == "unknown_command_acknowledgement" for row in case.store.records.rows)
    assert case.unknown.data == old_execution


@pytest.mark.parametrize(
    "field",
    [
        "acknowledge_result_unknown",
        "acknowledge_effects_may_have_occurred",
        "acknowledge_no_original_replay",
        "acknowledge_bound_verification_and_continuation",
    ],
)
@pytest.mark.parametrize("value", [False, 1])
async def test_operator_acknowledgements_require_literal_true(unknown_case, monkeypatch, field, value):
    case = unknown_case
    api = service(case, monkeypatch)
    candidate = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    from firefly_forge.unknown_command_models import UnknownCommandAcknowledgementV1

    with pytest.raises(ValueError):
        UnknownCommandAcknowledgementV1.model_validate({**consent(candidate).model_dump(), field: value})


async def test_acknowledgement_insert_failure_rolls_back_without_rewriting_old_evidence(unknown_case, monkeypatch):
    case = unknown_case
    api = service(case, monkeypatch)
    candidate = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    before = copy.deepcopy([row.data for row in case.store.records.rows])
    original = case.store.records.save

    async def fail(row):
        await original(row)
        if row.kind == "unknown_command_acknowledgement":
            raise RuntimeError("Controlled acknowledgement WAL failure")

    monkeypatch.setattr(case.store.records, "save", fail)
    with pytest.raises(RuntimeError, match="WAL failure"):
        await api.request(case.run_id, case.unknown.id.hex, uuid4().hex, consent(candidate))
    assert [row.data for row in case.store.records.rows] == before
    assert case.unknown.data["effects"]["start"] == {"status": "intent"}


@pytest.mark.parametrize("route", ["get", "idempotent_post"])
async def test_current_credentials_withhold_retained_acknowledgements_without_resend(unknown_case, monkeypatch, route):
    case = unknown_case
    api = service(case, monkeypatch)
    candidate = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    request_id = uuid4().hex
    record = await api.request(case.run_id, case.unknown.id.hex, request_id, consent(candidate))
    case.store.credential_registry = lambda: {"API_TOKEN": record["payload"]["candidate"]["execution_digest"]}
    with pytest.raises(ValueError, match="configured credentials"):
        if route == "get":
            await api.get(case.run_id, case.unknown.id.hex, request_id)
        else:
            await api.request(case.run_id, case.unknown.id.hex, request_id, consent(candidate))
    assert len([row for row in case.store.records.rows if row.kind == record["kind"]]) == 1


async def test_allocator_rechecks_acknowledging_operator_permission(unknown_case, monkeypatch):
    case = unknown_case
    api = service(case, monkeypatch)
    candidate = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    await api.request(case.run_id, case.unknown.id.hex, uuid4().hex, consent(candidate))
    await adopt_and_begin(case, candidate, monkeypatch)

    async def revoked(*args, **kwargs):
        raise AccessDenied("Acknowledging native operator was revoked")

    monkeypatch.setattr(ProjectAccess, "authorize_actor", revoked)
    case.backend.run = case.successful_backend
    with pytest.raises(AccessDenied, match="revoked"):
        await case.fresh().execute(
            case.run_id, case.workspace, case.workspace, ["fixture-check"], 2, "implement", ordinal=3
        )
    assert len(case.store.sandboxes.rows) == 2 and len(case.calls) == 1


@pytest.mark.parametrize("boundary", ["create_receipt", "start_intent"])
async def test_distinct_ack_operator_revocation_before_dispatch_preserves_cleanup(unknown_case, monkeypatch, boundary):
    case = unknown_case
    original_actor = Principal(uuid4(), "fixture", "original-execution", authentication_method="local_bootstrap")
    case.store.run.data["execution_actor"] = ProjectAccess.actor_snapshot(original_actor)
    api = service(case, monkeypatch)
    candidate = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    await api.request(case.run_id, case.unknown.id.hex, uuid4().hex, consent(candidate))
    await adopt_and_begin(case, candidate, monkeypatch)
    revoked = False

    async def actor_current(self, actor, *args, **kwargs):
        if revoked and actor["user_id"] == case.principal.id.hex:
            raise AccessDenied("Acknowledging operator revoked after allocation")
        return True

    async def original_current(self, run, permission="execute"):
        await self.authorize_actor(run["execution_actor"], run.get("project_id"), permission)

    monkeypatch.setattr(ProjectAccess, "authorize_actor", actor_current)
    monkeypatch.setattr(ProjectAccess, "authorize_execution", original_current)
    method = "sandbox_effect_receipt" if boundary == "create_receipt" else "sandbox_effect_intent"
    original = getattr(case.journal, method)

    async def changed(*args, **kwargs):
        nonlocal revoked
        result = await original(*args, **kwargs)
        if kwargs.get("effect") == ("create" if boundary == "create_receipt" else "start"):
            revoked = True
        return result

    monkeypatch.setattr(case.journal, method, changed)
    case.backend.run = case.successful_backend
    old = copy.deepcopy(case.unknown.data)
    with pytest.raises(AccessDenied, match="revoked after allocation"):
        await case.fresh().execute(
            case.run_id, case.workspace, case.workspace, ["fixture-check"], 2, "implement", ordinal=3
        )
    assert len(case.calls) == 1
    newest = case.store.sandboxes.rows[-1]
    assert newest.ordinal == 3 and newest.data["effects"]["cleanup"]["status"] == "done"
    assert newest.data["effects"]["start"] == {"status": "intent"}
    assert case.unknown.data == old


async def test_closed_before_start_can_be_retired_with_old_unknown_for_one_new_verification(unknown_case, monkeypatch):
    case = unknown_case
    api = service(case, monkeypatch)
    first = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    await api.request(case.run_id, case.unknown.id.hex, uuid4().hex, consent(first))
    intent = await adopt_and_begin(case, first, monkeypatch)
    original = case.journal.sandbox_effect_receipt

    async def revoked_create(run_id, task_id, execution_id, *, effect, evidence):
        result = await original(run_id, task_id, execution_id, effect=effect, evidence=evidence)
        if effect == "create":
            await case.fresh()._cleanup_host(run_id, task_id, execution_id, result["request"])
            raise AccessDenied("Controlled revocation before native start intent")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(case.journal, "sandbox_effect_receipt", revoked_create)
        with pytest.raises(AccessDenied, match="before native start"):
            await case.fresh().execute(
                case.run_id, case.workspace, case.workspace, ["fixture-check"], 2, "implement", ordinal=3
            )
    closed = case.store.sandboxes.rows[-1]
    assert closed.ordinal == 3 and set(closed.data["effects"]) == {"create", "cleanup"}
    await case.journal.verification_hold(case.run_id, intent["id"])
    policy = FactoryToolPolicyInput(automatic_verification={"enabled": True, "max_continuations": 4})
    case.store.configuration._write(
        case.store.configuration.directory / "tool-policy.json",
        {"revision": uuid4().hex, "settings": policy.model_dump()},
    )
    future = uuid4().hex
    old_before, closed_before = copy.deepcopy(case.unknown.data), copy.deepcopy(closed.data)
    unknown_candidate = await api.candidate(
        case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=future
    )
    await api.request(case.run_id, case.unknown.id.hex, uuid4().hex, consent(unknown_candidate))
    candidate = await api.candidate(case.run_id, closed.id.hex, agent="agentic", adoption_request_id=future)
    assert candidate["classification"] == "closed_before_start" and candidate["start_intent_digest"] is None
    receipt = await api.request(case.run_id, closed.id.hex, uuid4().hex, consent(candidate))
    assert receipt["payload"]["disposition"] == "closed_before_start_retired_for_verification"
    await adopt_and_begin(case, candidate, monkeypatch)
    case.backend.run = case.successful_backend
    result = await case.fresh().execute(
        case.run_id, case.workspace, case.workspace, ["fixture-check"], 2, "implement", ordinal=4
    )
    assert result.exit_code == 0 and len(case.calls) == 2
    assert case.unknown.data == old_before and closed.data == closed_before
    assert [row.ordinal for row in case.store.sandboxes.rows] == [1, 2, 3, 4]
