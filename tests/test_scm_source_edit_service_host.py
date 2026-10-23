# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled observed GitHub trigger; native activation is tested independently."""

# ruff: noqa: F401, F811
import copy
import importlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_scm_polling import poll_case

from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess


async def prepare(c, monkeypatch):
    module = importlib.import_module("firefly_forge.scm_source_edit")
    monkeypatch.setattr(module, "current_unit_of_work", lambda: object() if c.state["depth"] else None)
    monkeypatch.setattr(module, "ForgeIdentityUserRepository", lambda: c.service.users)
    monkeypatch.setattr(
        module, "ForgeProjectRepository", importlib.import_module("firefly_forge.scm_polling").ForgeProjectRepository
    )
    c.state["activations"] = []
    c.db["runs"][c.run_id].data.update(project_id=c.project, component_id="api")
    native = {
        **ProjectAccess.actor_snapshot(c.owner),
        "identity_digest": digest({"issuer": c.owner.issuer, "subject": c.owner.subject}),
    }
    candidate = dict(
        actor=native,
        run_id=c.run_id,
        source_edit={"qualification": {"profile_id": "public-fixture", "contract": "3" * 64}},
        previous_task={"attempt": 2},
    )
    candidate["digest"] = digest(candidate)

    async def source_candidate(run):
        assert CURRENT_PRINCIPAL.get() == c.owner
        return copy.deepcopy(candidate)

    async def activation(admission):
        assert CURRENT_PRINCIPAL.get() == admission.caller
        assert admission.caller.authentication_method == "scm_mapping"
        await admission.verify()
        c.state["activations"].append(admission)
        hook = c.state.get("activation_hook")
        if hook:
            await hook()
        await admission.verify()
        if c.state.get("activation_unknown"):
            raise RuntimeError("Controlled native effect outcome unknown")
        return {"actor": admission.native_actor, "scm_source_activation": admission.evidence, "intent_digest": "4" * 64}

    workspace = SimpleNamespace(source_candidate=source_candidate, activate_source_edit_from_scm=activation)
    service = module.SCMSourceEditAuthority(c.store, c.service, workspace)
    c.state["source_service"] = service
    await c.ready()
    binding = await c.consent()
    body = dict(
        version=1,
        transport="github_rest_poll_v1",
        request_id=uuid4().hex,
        expected_previous_digest=None,
        expected_project_binding_digest=digest(c.definition),
        expected_binding_request_id=binding["request"]["request_id"],
        expected_binding_digest=binding["digest"],
        component_ids=["api"],
        issue_number=3,
        run_id=c.run_id,
        activation=dict(
            version=1,
            request_id=uuid4().hex,
            candidate_digest=candidate["digest"],
            acknowledge_environment_only=True,
            acknowledge_resume_existing_task=True,
            acknowledge_existing_read_scope=True,
            acknowledge_no_commands=True,
        ),
        expected_source_profile_digest=digest(candidate["source_edit"]["qualification"]),
        expires_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        active=True,
        acknowledge_same_run_source_only=True,
    )
    return module, service, body


async def observe(c, grant, **change):
    number = 100 + len(c.state["comments"])
    c.state["clock"] += 3600
    c.state["comments"].append(
        c.comment(
            number,
            "/forge source_edit "
            + json.dumps(dict(version=1, run_id=c.run_id, expected_grant_digest=grant["digest"], **change)),
        )
    )
    for _ in range(3):
        await c.service.step(c.repository)
    return next(copy.deepcopy(row.data) for row in c.db["inbox"].values() if row.source_key == f"comment:{number}")


async def test_separate_consent_dispatches_exact_sealed_request_once_and_keeps_actual_scm_actor(poll_case, monkeypatch):
    async with poll_case() as c:
        module, service, body = await prepare(c, monkeypatch)
        grant = await service.configure(c.project, c.repository, module.SCMSourceEditGrantInputV1(**body))
        assert await service.get(c.project, c.repository, body["request_id"]) == grant
        delivery = await observe(c, grant)
        before = copy.deepcopy(c.db["runs"][c.run_id].data)
        result = await service.process(delivery["id"])
        assert result["status"] == "processed"
        assert len(c.state["activations"]) == 1
        admission = c.state["activations"][0]
        assert admission.request.model_dump() == body["activation"]
        assert admission.native_actor == grant["admin_snapshot"]
        assert admission.evidence["receipt_id"] == delivery["id"]
        assert await service.process(delivery["id"]) == result
        other = await observe(c, grant)
        with pytest.raises((AccessDenied, ValueError), match="reserved|used"):
            await service.process(other["id"])
        assert len(c.state["activations"]) == 1
        assert c.db["runs"][c.run_id].data == before
        assert CURRENT_PRINCIPAL.get() == c.owner


@pytest.mark.parametrize("change", ["grant", "source", "actor", "admin", "expiry", "candidate", "profile"])
async def test_current_admission_fences_refuse_before_native_activation(poll_case, monkeypatch, change):
    async with poll_case() as c:
        module, service, body = await prepare(c, monkeypatch)
        if change in {"candidate", "profile"}:
            body["activation"]["candidate_digest"] = (
                "0" * 64 if change == "candidate" else body["activation"]["candidate_digest"]
            )
            body["expected_source_profile_digest"] = (
                "0" * 64 if change == "profile" else body["expected_source_profile_digest"]
            )
            with pytest.raises((AccessDenied, ValueError)):
                await service.configure(c.project, c.repository, module.SCMSourceEditGrantInputV1(**body))
            assert c.state["activations"] == []
            return
        grant = await service.configure(c.project, c.repository, module.SCMSourceEditGrantInputV1(**body))
        delivery = await observe(c, grant)
        if change == "grant":
            await service.configure(
                c.project,
                c.repository,
                module.SCMSourceEditGrantInputV1(
                    **{**body, "request_id": uuid4().hex, "expected_previous_digest": grant["digest"], "active": False}
                ),
            )
        elif change == "source":
            c.state["edited"] = True
        elif change == "actor":
            c.db["memberships"]["member"].active = False
        elif change == "admin":
            c.db["users"][c.owner.id.hex].enabled = False
        else:
            monkeypatch.setattr(module, "utc_now", lambda: datetime.now(UTC) + timedelta(hours=2))
        with pytest.raises((AccessDenied, ValueError)):
            await service.process(delivery["id"])
        assert c.state["activations"] == []


async def test_native_unknown_outcome_is_reserved_held_and_never_replayed(poll_case, monkeypatch):
    async with poll_case() as c:
        module, service, body = await prepare(c, monkeypatch)
        grant = await service.configure(c.project, c.repository, module.SCMSourceEditGrantInputV1(**body))
        delivery = await observe(c, grant)
        c.state["activation_unknown"] = True
        with pytest.raises(RuntimeError, match="unknown"):
            await service.process(delivery["id"])
        held = await service.process(delivery["id"])
        assert held["status"] == "needs_reconciliation" and held["action_status"] == "needs_reconciliation"
        assert len(c.state["activations"]) == 1


async def test_missing_durable_reservation_index_never_replenishes_source_trigger(poll_case, monkeypatch):
    async with poll_case() as c:
        module, service, body = await prepare(c, monkeypatch)
        grant = await service.configure(c.project, c.repository, module.SCMSourceEditGrantInputV1(**body))
        delivery = await observe(c, grant)
        c.db["settings"][c.repository].data.pop(module.USED_KEY)
        with pytest.raises((AccessDenied, ValueError), match="ledger|reservation"):
            await service.process(delivery["id"])
        assert c.state["activations"] == []


async def test_operator_reconciliation_survives_returning_source_activation(poll_case, monkeypatch):
    async with poll_case() as c:
        module, service, body = await prepare(c, monkeypatch)
        grant = await service.configure(c.project, c.repository, module.SCMSourceEditGrantInputV1(**body))
        delivery = await observe(c, grant)
        captured = {}

        async def reconcile():
            token = CURRENT_PRINCIPAL.set(c.owner)
            try:
                captured["value"] = await c.collaboration.reconcile(
                    delivery["id"], "not_completed", "Public source activation outcome held"
                )
            finally:
                CURRENT_PRINCIPAL.reset(token)

        c.state["activation_hook"] = reconcile
        with pytest.raises(AccessDenied, match="custody"):
            await service.process(delivery["id"])
        assert await c.collaboration.receipt(delivery["id"]) == captured["value"]
        assert len(c.state["activations"]) == 1


async def test_consent_revocation_during_native_activation_wait_holds_feedback(poll_case, monkeypatch):
    async with poll_case() as c:
        module, service, body = await prepare(c, monkeypatch)
        grant = await service.configure(c.project, c.repository, module.SCMSourceEditGrantInputV1(**body))
        delivery = await observe(c, grant)

        async def revoke():
            token = CURRENT_PRINCIPAL.set(c.owner)
            try:
                await service.configure(
                    c.project,
                    c.repository,
                    module.SCMSourceEditGrantInputV1(
                        **{
                            **body,
                            "request_id": uuid4().hex,
                            "expected_previous_digest": grant["digest"],
                            "active": False,
                        }
                    ),
                )
            finally:
                CURRENT_PRINCIPAL.reset(token)

        c.state["activation_hook"] = revoke
        with pytest.raises(AccessDenied):
            await service.process(delivery["id"])
        result = await c.collaboration.receipt(delivery["id"])
        assert result["status"] == "needs_reconciliation" and result["feedback_status"] == "held"
        assert result.get("action_result") is None


async def test_real_collaboration_dispatch_uses_only_the_separate_source_service(poll_case, monkeypatch):
    async with poll_case() as c:
        module, service, body = await prepare(c, monkeypatch)
        grant = await service.configure(c.project, c.repository, module.SCMSourceEditGrantInputV1(**body))
        delivery = await observe(c, grant)
        c.collaboration.source_edit_authority = service
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "processed"
        assert len(c.state["activations"]) == 1
        assert c.state["posts"] == []
        assert result["feedback_status"] == "held"


async def test_expiration_during_activation_wait_never_reports_queued(poll_case, monkeypatch):
    async with poll_case() as c:
        module, service, body = await prepare(c, monkeypatch)
        grant = await service.configure(c.project, c.repository, module.SCMSourceEditGrantInputV1(**body))
        delivery = await observe(c, grant)

        async def expire():
            monkeypatch.setattr(module, "utc_now", lambda: datetime.now(UTC) + timedelta(hours=2))

        c.state["activation_hook"] = expire
        with pytest.raises(AccessDenied, match="expired"):
            await service.process(delivery["id"])
        result = await c.collaboration.receipt(delivery["id"])
        assert result.get("action_result") is None
        assert result["feedback_status"] == "held"


async def test_lost_execution_owner_never_writes_a_late_source_hold(poll_case, monkeypatch):
    async with poll_case() as c:
        module, service, body = await prepare(c, monkeypatch)
        grant = await service.configure(c.project, c.repository, module.SCMSourceEditGrantInputV1(**body))
        delivery = await observe(c, grant)
        captured = {}

        async def takeover():
            captured["value"] = await c.collaboration.receipt(delivery["id"])
            c.state["ownership_lost"] = True

        c.state["activation_hook"] = takeover
        with pytest.raises(RuntimeError, match="ownership"):
            await service.process(delivery["id"])
        assert await c.collaboration.receipt(delivery["id"]) == captured["value"]


async def test_mapped_suspension_during_verification_cannot_cross_effect_gate(poll_case, monkeypatch):
    async with poll_case() as c:
        module, service, body = await prepare(c, monkeypatch)
        grant = await service.configure(c.project, c.repository, module.SCMSourceEditGrantInputV1(**body))
        delivery = await observe(c, grant)
        state = {"armed": False, "gate_crossed": False, "suspended": False, "identity_locked": False}
        original = c.store.assert_ownership
        find = c.service.users.find_by_id

        async def locked_identity(value, **kwargs):
            if (
                state["armed"]
                and c.state["depth"] >= 2
                and value.hex == c.user_id
                and kwargs.get("lock") == module.LockMode.PESSIMISTIC_WRITE
            ):
                state["identity_locked"] = True
            return await find(value, **kwargs)

        c.service.users.find_by_id = locked_identity

        async def ownership():
            await original()
            if state["armed"] and c.state["depth"] > 0 and not state["suspended"]:
                c.db["users"][c.user_id].enabled = False
                state["suspended"] = True

        async def effect(admission):
            state["armed"] = True
            async with c.store._unit():
                await admission.verify()
                state["gate_crossed"] = True
            return {"intent_digest": "4" * 64}

        c.store.assert_ownership = ownership
        service.workspaces.activate_source_edit_from_scm = effect
        with pytest.raises(AccessDenied):
            await service.process(delivery["id"])
        assert state["suspended"]
        assert state["identity_locked"], "The distinct mapped identity must be locked in the native effect unit"
        assert not state["gate_crossed"], (
            "Verifier returned successfully after mapped identity suspension during its ownership await"
        )
