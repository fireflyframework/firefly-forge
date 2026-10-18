# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host service controls with unavailable persistence/provider boundaries replaced."""

import copy
import importlib
import importlib.util
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_scm_resume_cut import answered_cut  # noqa: F401

from firefly_forge.collaboration import CollaborationSettings, RepositoryCommand
from firefly_forge.journal import digest
from firefly_forge.project_access import AccessDenied


def implementation():
    assert importlib.util.find_spec("firefly_forge.scm_resume_service") is not None, (
        "Native scoped resume service is missing"
    )
    return importlib.import_module("firefly_forge.scm_resume_service")


@pytest.fixture
async def resume_service(answered_cut):  # noqa: F811
    module = implementation()
    scope = answered_cut
    state = {
        "revoked": False,
        "permission": True,
        "original_permission": True,
        "external": 0,
        "resumes": 0,
        "locked": False,
        "delivery": None,
        "change_during_preflight": False,
    }
    grant = {
        "digest": scope["grant_digest"],
        "native_user_id": scope["native_user_id"],
        "request": {"active": True, "component_ids": ["api"], "expected_binding_digest": scope["binding_digest"]},
    }
    row = SimpleNamespace(
        status="received",
        data={
            "id": "a" * 32,
            "repository_id": scope["run"]["repository_id"],
            "status": "received",
            "action_status": "not_dispatched",
        },
    )

    async def get_run(run_id):
        assert run_id == scope["run"]["id"]
        return copy.deepcopy(scope["run"])

    async def admit(value):
        assert not state["locked"], "Credential admission must close the transaction"

    completions = {}

    class Harness(module.SCMResumeAuthority):
        async def _save_completion(self, result):
            completions[result["receipt_id"]] = copy.deepcopy(result)

        async def _verify_completion(self, result):
            if completions.get(result["receipt_id"]) != result:
                raise AccessDenied("The mutable outcome does not bind its original WAL completion")

        async def _context(self, delivery):
            if state["revoked"] or not state["permission"]:
                raise AccessDenied("Current mapping or native project permission revoked")
            return {
                "grant": copy.deepcopy(grant),
                "binding": {"digest": scope["binding_digest"]},
                "run": copy.deepcopy(scope["run"]),
                "user_id": scope["native_user_id"],
            }

        async def _preflight(self, context):
            assert not state["locked"]
            state["external"] += 1
            if state["change_during_preflight"]:
                state["revoked"] = True

        async def _cut(self, context):
            return module.resume_cut(**scope)

        async def _original(self, run):
            assert run["execution_actor"] == scope["run"]["execution_actor"]
            if not state["original_permission"]:
                raise AccessDenied("Original execution authority revoked")

        @asynccontextmanager
        async def _locked(self, delivery, context):
            state["locked"] = True
            before = copy.deepcopy(row.data)
            try:
                yield row
            except BaseException:
                row.data = before
                row.status = before["status"]
                raise
            finally:
                state["locked"] = False

        async def _delivery(self, receipt_id):
            assert receipt_id == row.data["id"]
            return copy.deepcopy(row.data)

        async def _write(self, current, **changes):
            current.data = {**current.data, **changes}
            current.status = current.data["status"]

    async def resume(run_id):
        assert state["locked"]
        assert row.data["action_status"] == "writing", "Persist intent before the admitted resume"
        state["resumes"] += 1
        scope["run"]["status"] = "queued"
        return copy.deepcopy(scope["run"])

    service = Harness(
        SimpleNamespace(get_run=get_run, admit_credentials=admit, reject_credentials=lambda _: None),
        SimpleNamespace(),
        SimpleNamespace(resume=resume),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    cut = module.resume_cut(**scope)
    row.data["command"] = {
        "version": 2,
        "action": "resume",
        "run_id": scope["run"]["id"],
        "expected_grant_digest": grant["digest"],
        "expected_question_id": cut["question_id"],
        "expected_response_digest": cut["response_digest"],
        "expected_cut_digest": cut["digest"],
    }
    return service, row, scope, state


def test_v2_status_and_resume_are_explicit_and_v1_commands_remain_distinct():
    request = {"version": 2, "run_id": "1" * 32, "expected_grant_digest": "2" * 64}
    parsed = RepositoryCommand.parse("/forge status " + __import__("json").dumps(request)).model_dump()
    assert parsed == {"action": "status", **request}
    assert RepositoryCommand.parse('/forge resume {"run_id":"' + "1" * 32 + '"}').model_dump(exclude_none=True) == {
        "action": "resume",
        "run_id": "1" * 32,
    }


async def test_same_delivery_resumes_once_with_original_actor_and_inert_exact_replay(resume_service):
    service, row, scope, state = resume_service
    original = copy.deepcopy(scope["run"]["execution_actor"])
    result = await service.process(row.data["id"])
    assert result["action_status"] == "completed" and result["action_result"]["status"] == "queued"
    assert result["action_result"]["resume_cut_digest"] == row.data["command"]["expected_cut_digest"]
    assert scope["run"]["execution_actor"] == original and state["resumes"] == 1
    assert await service.process(row.data["id"]) == result
    assert state["resumes"] == 1


@pytest.mark.parametrize(
    "field", ["expected_cut_digest", "expected_response_digest", "expected_question_id", "expected_grant_digest"]
)
async def test_changed_review_never_resumes(resume_service, field):
    service, row, _, state = resume_service
    row.data["command"][field] = "b" * (64 if field != "expected_question_id" else 16)
    with pytest.raises((AccessDenied, ValueError)):
        await service.process(row.data["id"])
    assert state["resumes"] == 0 and row.data["status"] == "received"


@pytest.mark.parametrize("denial", ["permission", "original_permission", "change_during_preflight"])
async def test_current_requester_and_original_authority_are_independently_required(resume_service, denial):
    service, row, _, state = resume_service
    state[denial] = denial == "change_during_preflight"
    with pytest.raises(AccessDenied):
        await service.process(row.data["id"])
    assert state["resumes"] == 0


async def test_revocation_blocks_private_completed_readback_without_second_resume(resume_service):
    service, row, _, state = resume_service
    await service.process(row.data["id"])
    state["revoked"] = True
    with pytest.raises(AccessDenied):
        await service.process(row.data["id"])
    assert state["resumes"] == 1


async def test_unknown_intent_is_held_not_replayed(resume_service):
    service, row, _, state = resume_service
    row.data.update(status="needs_reconciliation", action_status="writing")
    with pytest.raises(AccessDenied):
        await service.process(row.data["id"])
    assert state["resumes"] == 0


def test_native_scoped_resume_service_is_available_without_widening_v1():
    assert importlib.util.find_spec("firefly_forge.scm_resume_service") is not None, (
        "Native scoped resume service is missing"
    )


async def test_validly_resigned_mutable_outcome_cannot_replace_the_original_completion(resume_service):
    service, row, _, state = resume_service
    await service.process(row.data["id"])
    row.data["action_result"]["status"] = "succeeded"
    row.data["action_result"]["digest"] = digest({k: v for k, v in row.data["action_result"].items() if k != "digest"})
    with pytest.raises(AccessDenied):
        await service.process(row.data["id"])
    assert state["resumes"] == 1


@pytest.fixture
async def current_context(tmp_path, monkeypatch):
    """Exercise the real authority checks; replace persistence/V1 record retrieval only."""
    import json
    from pathlib import Path
    from uuid import UUID

    from firefly_forge.access_persistence import ForgeMembershipRepository
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.scm_resume_models import SCMResumeGrantInputV2

    module = implementation()
    binding = json.loads(Path("studio/tests/scm-delegation-contract-fixture.json").read_text())["receipt"]
    project = {"definition": {"components": [{"id": "api", "repository_id": binding["repository_id"]}]}}
    binding["project_binding_digest"] = digest(project)
    binding["request"]["expected_project_binding_digest"] = digest(project)
    binding["request_digest"] = digest(binding["request"])
    binding["digest"] = digest({k: v for k, v in binding.items() if k != "digest"})
    request = SCMResumeGrantInputV2(
        version=2,
        request_id="a" * 32,
        expected_previous_digest=None,
        expected_project_binding_digest=digest(project),
        expected_binding_request_id=binding["request"]["request_id"],
        expected_binding_digest=binding["digest"],
        component_ids=["api"],
        allowed_actions=["resume"],
        active=True,
        acknowledge_same_run_resume=True,
    )
    grant = {
        "digest": "b" * 64,
        "native_user_id": binding["request"]["native_user_id"],
        "request": request.model_dump(mode="json"),
    }
    state = {"enabled": True, "role": "developer", "connection_current": True}
    run = {
        "id": "1" * 32,
        "project_id": binding["project_id"],
        "repository_id": binding["repository_id"],
        "component_id": "api",
    }
    reference = copy.deepcopy(binding["verified_reference"])
    delivery = {
        "id": "2" * 32,
        "repository_id": run["repository_id"],
        "provider": reference["provider"],
        "actor": reference["external_actor_id"],
        "remote_id": reference["remote_repository_id"],
        "digest": reference["payload_digest"],
        "verification": reference,
        "command": {"version": 2, "action": "resume", "run_id": run["id"], "expected_grant_digest": grant["digest"]},
    }
    settings = CollaborationSettings(
        enabled=True, trusted_actors=[delivery["actor"]], allowed_commands=["status"]
    ).model_dump(mode="json", exclude={"webhook_secret"})

    @asynccontextmanager
    async def unit():
        yield None

    async def binding_record(*args):
        return copy.deepcopy(binding)

    async def project_record(*args):
        return copy.deepcopy(project)

    async def get_run(*args):
        return copy.deepcopy(run)

    async def repository(*args):
        return {"id": run["repository_id"], "url": "https://github.com/fixture/product.git"}

    async def find_settings(*args):
        return SimpleNamespace(data=copy.deepcopy(settings))

    async def identity(*args):
        if state.get("missing_user"):
            return None
        return SimpleNamespace(id=UUID(grant["native_user_id"]), enabled=state["enabled"])

    async def memberships(*args):
        return [SimpleNamespace(project_id=UUID(run["project_id"]), role=state["role"])]

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    monkeypatch.setattr(ForgeMembershipRepository, "for_user", memberships)
    authority = SimpleNamespace(
        _existing=binding_record,
        _head=binding_record,
        _project=project_record,
        _key_current=lambda *args: state["connection_current"],
        scm=SimpleNamespace(delegation_scope=lambda _: {"provider": reference["provider"]}),
    )
    store = SimpleNamespace(
        _unit=unit,
        get_run=get_run,
        repository=repository,
        configuration=SimpleNamespace(directory=tmp_path, _read=lambda _: {}),
    )
    service = module.SCMResumeAuthority(
        store,
        authority,
        SimpleNamespace(),
        SimpleNamespace(find_for_repository=find_settings),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    async def grant_record(*args, **kwargs):
        return copy.deepcopy(grant)

    service._grant = grant_record
    service._run_data = get_run
    return service, delivery, binding, grant, settings, state


async def test_current_v2_context_uses_registered_user_and_current_project_execute_permission(current_context):
    service, delivery, binding, _, settings, _ = current_context
    assert settings["channel_commands"] == []
    context = await service._context(delivery)
    assert context["user_id"] == binding["request"]["native_user_id"]
    assert context["binding"]["digest"] == binding["digest"]


@pytest.mark.parametrize("allowed", [True, False])
async def test_v2_status_still_requires_the_current_status_policy(current_context, allowed):
    service, delivery, _, _, settings, _ = current_context
    delivery["command"]["action"] = "status"
    settings["allowed_commands"] = ["status"] if allowed else []
    if allowed:
        await service._context(delivery)
    else:
        with pytest.raises(AccessDenied):
            await service._context(delivery)


@pytest.mark.parametrize(
    "mutation",
    [
        "v1_inactive",
        "v1_changed",
        "v2_inactive",
        "v2_missing",
        "native_disabled",
        "native_missing",
        "viewer",
        "settings_disabled",
        "untrusted_actor",
        "connection_changed",
    ],
)
async def test_real_context_checks_reject_revocation_without_authority_from_command_text(current_context, mutation):
    service, delivery, binding, grant, settings, state = current_context
    if mutation == "v1_inactive":
        binding["request"]["active"] = False
    elif mutation == "v1_changed":
        binding["digest"] = "c" * 64
    elif mutation == "v2_inactive":
        grant["request"]["active"] = False
    elif mutation == "v2_missing":

        async def missing(*args, **kwargs):
            return None

        service._grant = missing
    elif mutation == "native_disabled":
        state["enabled"] = False
    elif mutation == "native_missing":
        state["missing_user"] = True
    elif mutation == "viewer":
        state["role"] = "viewer"
    elif mutation == "settings_disabled":
        settings["enabled"] = False
    elif mutation == "untrusted_actor":
        settings["trusted_actors"] = []
    else:
        state["connection_current"] = False
    with pytest.raises(AccessDenied):
        await service._context(delivery)


@pytest.mark.parametrize("mutation", [None, "outcome", "handoff", "predecessor"])
async def test_native_completion_reader_checks_real_journal_envelopes(answered_cut, mutation):  # noqa: F811
    """Only the SQL row retrieval is replaced; append, hashes and verifier are real."""
    from uuid import UUID

    from firefly_forge.journal import Journal

    module = implementation()
    rows = [
        SimpleNamespace(
            **{k: v for k, v in item.items() if k != "run_id"}, run_id=UUID(item["run_id"]), data=copy.deepcopy(item)
        )
        for item in answered_cut["entries"]
    ]

    async def find(_):
        return rows

    async def save(row):
        rows.append(row)

    async def get_run(run_id):
        assert run_id == answered_cut["run"]["id"]
        return copy.deepcopy(answered_cut["run"])

    journal = Journal(SimpleNamespace(get_run=get_run), records=SimpleNamespace(find_for_run=find, save=save))
    cut = module.resume_cut(**answered_cut)
    result = {
        "version": 2,
        "run_id": answered_cut["run"]["id"],
        "receipt_id": "a" * 32,
        "status": "queued",
        "review": cut,
    }
    result["digest"] = digest(result)
    await journal._append(
        result["run_id"], "resume", "b" * 32, "resume", {"selection_mode": "frozen", "automatic": False}
    )
    await journal._append(result["run_id"], "resume", result["receipt_id"], "scm_resume_receipt", result)
    selected = copy.deepcopy(rows[-2:])
    if mutation == "outcome":
        result["status"] = "succeeded"
        result["digest"] = digest({k: v for k, v in result.items() if k != "digest"})
    elif mutation == "handoff":
        selected[0].kind = selected[0].data["kind"] = "handoff"
    elif mutation == "predecessor":
        selected[0].previous_hash = selected[0].data["previous_hash"] = "c" * 64
    if mutation in {"handoff", "predecessor"}:
        for position, row in enumerate(selected):
            if position:
                row.previous_hash = row.data["previous_hash"] = selected[position - 1].digest
            row.digest = row.data["digest"] = digest({k: v for k, v in row.data.items() if k != "digest"})

    async def scalar(_):
        return 4096

    async def scalars(_):
        return selected

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace(scalar=scalar, scalars=scalars)

    service = module.SCMResumeAuthority(SimpleNamespace(_unit=unit), None, None, None, None, None)
    if mutation:
        with pytest.raises(AccessDenied):
            await service._verify_completion(result)
    else:
        await service._verify_completion(result)


@pytest.fixture
async def consent_service(monkeypatch):
    from uuid import UUID

    from firefly_forge.identity import Principal
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_access import CURRENT_PRINCIPAL
    from firefly_forge.project_persistence import ForgeProjectRepository
    from firefly_forge.scm_resume_models import SCMResumeGrantInputV2

    module = implementation()
    project, repository, user, admin = (x * 32 for x in "1234")
    binding = {"binding_key": "5" * 64, "request": {"native_user_id": user}}
    row = SimpleNamespace(data={"enabled": True, "_scm_authority_v1": {"preserve": "literal"}})
    records, state = [], {"admin": True, "locked": False}
    actor = Principal(UUID(admin), "host-fixture", "admin", authentication_method="local_bootstrap")

    async def reader(current, p, r, *, admin):
        assert current is actor and CURRENT_PRINCIPAL.get() is actor and (p, r) == (project, repository)
        if not state["admin"]:
            raise AccessDenied("Current administrator authority revoked")

    async def noop(*args, **kwargs):
        return None

    async def scalar(_):
        return 0

    @asynccontextmanager
    async def unit():
        before = state["locked"]
        state["locked"] = True
        try:
            yield SimpleNamespace(execute=noop, scalar=scalar)
        finally:
            state["locked"] = before

    async def identity(*args, **kwargs):
        return SimpleNamespace(enabled=True)

    async def settings(*args):
        return row

    async def save(value):
        if value is not row:
            records.append(copy.deepcopy(value.data["receipt"]))

    async def admit(_):
        assert not state["locked"]

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    monkeypatch.setattr(ForgeProjectRepository, "find_by_id", noop)

    class Harness(module.SCMResumeAuthority):
        async def _binding(self, p, r, request):
            assert request.expected_binding_digest == "6" * 64
            return copy.deepcopy(binding)

        async def _preflight(self, context):
            assert not state["locked"]

        async def _grant(self, p, r, *, request_id=None, key=None):
            selected = [
                item
                for item in records
                if (key and item["binding_key"] == key) or (request_id and item["request"]["request_id"] == request_id)
            ]
            return copy.deepcopy(selected[-1]) if selected else None

    service = Harness(
        SimpleNamespace(
            schema="host-only",
            _unit=unit,
            admit_credentials=admit,
            reject_credentials=lambda _: None,
            repository_records=SimpleNamespace(find_by_id=noop),
        ),
        SimpleNamespace(_reader=reader),
        None,
        SimpleNamespace(find_for_repository=settings, save=save),
        None,
        SimpleNamespace(save=save),
    )
    request = SCMResumeGrantInputV2(
        version=2,
        request_id="a" * 32,
        expected_previous_digest=None,
        expected_project_binding_digest="b" * 64,
        expected_binding_request_id="c" * 32,
        expected_binding_digest="6" * 64,
        component_ids=["api"],
        allowed_actions=["resume"],
        active=True,
        acknowledge_same_run_resume=True,
    )
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        yield service, project, repository, request, records, row, state
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_fresh_consent_saves_once_and_exact_same_request_replay_preserves_v1(consent_service):
    service, project, repository, request, records, row, _ = consent_service
    retained = await service.configure(project, repository, request)
    assert retained["native_user_id"] == "3" * 32
    assert retained["request_digest"] == digest(request.model_dump(mode="json"))
    assert retained["digest"] == digest({k: v for k, v in retained.items() if k != "digest"})
    assert await service.configure(project, repository, request) == retained
    assert len(records) == 1 and row.data["_scm_authority_v1"] == {"preserve": "literal"}
    assert row.data["_scm_resume_authority_v2"]["heads"][retained["binding_key"]]["digest"] == retained["digest"]


async def test_same_consent_id_cannot_change_input_or_bypass_new_revision_cas(consent_service):
    service, project, repository, request, records, _, _ = consent_service
    await service.configure(project, repository, request)
    for changes in [{"active": False}, {"request_id": "d" * 32}]:
        with pytest.raises(AccessDenied):
            await service.configure(project, repository, request.model_copy(update=changes))
    assert len(records) == 1


async def test_consent_does_not_infer_current_administrator_from_an_old_receipt(consent_service):
    service, project, repository, request, records, _, state = consent_service
    await service.configure(project, repository, request)
    state["admin"] = False
    with pytest.raises(AccessDenied):
        await service.configure(project, repository, request)
    assert len(records) == 1


@pytest.mark.parametrize("status", ["received", "processed"])
async def test_v2_denial_does_not_stop_delivery_poller_or_return_private_completion(status):
    from firefly_forge.collaboration import Collaboration

    row = SimpleNamespace(
        status=status,
        data={
            "id": "1" * 32,
            "status": status,
            "command": {"version": 2},
            "action_result": {"private": "must remain withheld"},
        },
    )

    async def receipt(_):
        return copy.deepcopy(row.data)

    async def denied(_):
        raise AccessDenied("The original mapping was revoked")

    async def find(*args, **kwargs):
        return row

    async def save(_):
        pass

    @asynccontextmanager
    async def unit():
        yield None

    service = object.__new__(Collaboration)
    service.store = SimpleNamespace(_unit=unit, configuration=SimpleNamespace(settings_generation=0))
    service.inbox = SimpleNamespace(find_by_id=find, save=save)
    service.receipt = receipt
    service.resume_authority = SimpleNamespace(process=denied)
    result = await service.process(row.data["id"])
    assert result["id"] == row.data["id"]
    assert "private" not in str(result) and "action_result" not in result
    assert row.status == ("denied" if status == "received" else status)


async def test_revoked_v2_feedback_authority_is_checked_before_outbound_intent():
    from firefly_forge.collaboration import Collaboration

    row = SimpleNamespace(
        data={"id": "1" * 32, "status": "processed", "command": {"version": 2}, "feedback_status": "pending"}
    )

    async def receipt(_):
        return copy.deepcopy(row.data)

    async def denied(_):
        raise AccessDenied("The V2 native user no longer has permission")

    @asynccontextmanager
    async def unit():
        yield None

    async def find(*args, **kwargs):
        return row

    async def save(_):
        pass

    service = object.__new__(Collaboration)
    service.store = SimpleNamespace(_unit=unit)
    service.inbox = SimpleNamespace(find_by_id=find, save=save)
    service._active_writes = set()
    service.receipt = receipt
    service.resume_authority = SimpleNamespace(authorize_feedback=denied)
    await service.feedback(row.data["id"])
    assert row.data["feedback_status"] == "pending"


@pytest.mark.parametrize("original", ["missing", "disabled", "enabled"])
async def test_v2_locks_require_the_original_bootstrap_identity_without_changing_legacy_policy(monkeypatch, original):
    from uuid import UUID

    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_persistence import ForgeProjectRepository

    module = implementation()
    run = {
        "id": "1" * 32,
        "repository_id": "2" * 32,
        "project_id": "3" * 32,
        "status": "paused",
        "agent": "bridge",
        "execution_actor": {"user_id": "4" * 32, "authentication_method": "local_bootstrap"},
    }
    delivery = {"id": "6" * 32, "command": {"version": 2}, "verification": {}, "digest": "7" * 64}
    row = SimpleNamespace(data=delivery)

    async def noop(*args, **kwargs):
        return None

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace(execute=noop)

    async def identity(_self, key, **kwargs):
        if key.hex == run["execution_actor"]["user_id"]:
            return None if original == "missing" else SimpleNamespace(enabled=original == "enabled")
        assert key.hex == "5" * 32
        return SimpleNamespace(enabled=True)

    async def run_record(*args, **kwargs):
        return SimpleNamespace(status="paused", data=run, repository_id=UUID(run["repository_id"]))

    async def inbox(*args, **kwargs):
        return row

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    monkeypatch.setattr(ForgeProjectRepository, "find_by_id", noop)
    service = module.SCMResumeAuthority(
        SimpleNamespace(
            schema="host-only",
            _unit=unit,
            repository_records=SimpleNamespace(find_by_id=noop),
            run_records=SimpleNamespace(find_by_id=run_record),
            assert_campaign_current=noop,
        ),
        None,
        None,
        None,
        SimpleNamespace(find_by_id=inbox),
        None,
    )
    if original == "missing":
        # The pre-identity bootstrap exception remains a global legacy contract.
        await service._original(run)
    if original != "enabled":
        with pytest.raises(AccessDenied):
            async with service._locked(delivery, {"run": run, "user_id": "5" * 32}):
                await service._original(run)
    else:
        async with service._locked(delivery, {"run": run, "user_id": "5" * 32}) as locked:
            assert locked is row
