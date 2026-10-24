# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Checkpoint requests keep AI interpretation separate from native Git authority."""

import copy
import importlib
import importlib.util
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_scm_natural_intake_host import prepared
from test_scm_polling import poll_case as poll_case

from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm_natural_models import NaturalDecision
from firefly_forge.scm_poll_models import SCMPollActorBindingInputV2


async def enable_checkpoint(case, natural, receipt):
    old = case.state["consent_request"]
    previous = await case.service._head(
        case.project,
        case.repository,
        (await natural.conversations.scopes(await case.collaboration.receipt(receipt), remote=False))[0]["binding_key"],
    )
    case.db["memberships"]["member"].role = "maintainer"
    case.db["settings"][case.repository].data["channel_commands"] = ["respond", "publish"]
    request = SCMPollActorBindingInputV2.model_validate(
        {
            **old.model_dump(mode="json"),
            "request_id": uuid4().hex,
            "expected_previous_digest": previous["digest"],
            "allowed_actions": ["status", "respond", "checkpoint"],
        }
    )
    await case.service.configure_binding(case.project, case.repository, request)


async def fresh_checkpoint(case, natural):
    case.state["clock"] += 3600
    case.state["comments"].append(case.comment(100, "@forge, guarda el avance como pull request borrador"))
    for _ in range(3):
        await case.service.step(case.repository)
    row = next(r for r in case.db["inbox"].values() if r.source_key == "comment:100")
    return await natural.process(row.data["id"])


async def test_natural_checkpoint_selects_only_the_authenticated_ticket_run(poll_case, monkeypatch):
    async with poll_case() as case:
        natural, receipt = await prepared(case, monkeypatch, intent="checkpoint")
        result = await natural.process(receipt)
        assert result["status"] == "processed"
        assert result["natural_turn"]["resolution"]["state"] == "checkpoint"
        assert result["natural_turn"]["resolution"]["run_id"] == case.run_id
        assert case.state["natural_calls"] == 1
        assert not case.db["journal"]
        assert await natural.process(receipt) == result


async def test_checkpoint_does_not_inherit_read_or_answer_consent(poll_case, monkeypatch):
    async with poll_case() as case:
        natural, receipt = await prepared(case, monkeypatch, intent="checkpoint")
        await natural.process(receipt)
        assert importlib.util.find_spec("firefly_forge.scm_natural_checkpoints"), (
            "Natural checkpoint admission is missing"
        )
        service = importlib.import_module("firefly_forge.scm_natural_checkpoints").SCMNaturalCheckpoints(natural)
        result = await service.apply(receipt)
        assert result["natural_turn"]["state"] == "checkpoint_pending_permission"
        assert "natural_checkpoint" not in result
        assert case.state["natural_calls"] == 1 and not case.db["journal"]


async def test_current_checkpoint_principal_is_scoped_and_cannot_administer(poll_case, monkeypatch):
    async with poll_case() as case:
        natural, receipt = await prepared(case, monkeypatch, intent="checkpoint")
        await natural.process(receipt)
        await enable_checkpoint(case, natural, receipt)
        # Changing consent invalidates an already interpreted message; a new comment must use the new binding.
        before = copy.deepcopy(await case.collaboration.receipt(receipt))
        with pytest.raises(AccessDenied):
            await case.service.resolve_checkpoint(before, remote=False)
        current = await fresh_checkpoint(case, natural)
        actor = await case.service.resolve_checkpoint(current, remote=False)
        assert actor.id.hex == case.user_id
        assert actor.scm_delegation["run_id"] == case.run_id
        assert not ProjectAccess.is_admin(actor)
        case.db["memberships"]["member"].role = "developer"
        with pytest.raises(AccessDenied):
            await case.service.resolve_checkpoint(current, remote=False)
        assert not case.db["journal"]
        assert ProjectAccess.is_admin(case.owner)


@pytest.mark.parametrize("unknown", [False, True])
async def test_native_queue_admission_is_retained_once_even_when_its_acknowledgement_is_lost(
    poll_case, monkeypatch, unknown
):
    async with poll_case() as case:
        natural, receipt = await prepared(case, monkeypatch, intent="checkpoint")
        await natural.process(receipt)
        await enable_checkpoint(case, natural, receipt)
        current = await fresh_checkpoint(case, natural)
        calls = []
        run = case.db["runs"][case.run_id].data
        run.update(status="failed", git_delivery_policy={"enabled": True, "remote": "draft_pr"}, source_tree="b" * 40)

        async def candidate(run_id):
            assert run_id == case.run_id
            return {"expected_head": "a" * 40, "expected_source_digest": "d" * 64}

        async def request(run_id, body):
            retained = await case.collaboration.receipt(current["id"])
            assert retained["natural_checkpoint"]["state"] == "admitting"
            assert retained["natural_checkpoint"]["request"] == body.model_dump(mode="json")
            assert CURRENT_PRINCIPAL.get().authentication_method == "scm_mapping"
            assert not ProjectAccess.is_admin(CURRENT_PRINCIPAL.get())
            assert run_id == case.run_id and body.expected_source_digest == "d" * 64
            assert body.expected_source_tree is None
            calls.append(body)
            if unknown:
                raise RuntimeError("The native queue acknowledgement was lost")
            return {"request_digest": "c" * 64}

        case.store.execution_requests = SimpleNamespace(
            git_deliveries=SimpleNamespace(deliveries=SimpleNamespace(candidate=candidate), request=request)
        )
        service = importlib.import_module("firefly_forge.scm_natural_checkpoints").SCMNaturalCheckpoints(natural)
        result = await service.apply(current["id"])
        assert result["natural_turn"]["state"] == ("needs_reconciliation" if unknown else "checkpoint_queued")
        assert await service.apply(current["id"]) == result
        assert len(calls) == 1 and CURRENT_PRINCIPAL.get() == case.owner
        assert run["status"] == "failed"


def test_checkpoint_classifier_cannot_supply_a_run_or_native_adoption():
    for field in ("run_id", "explicit_adoption", "expected_head"):
        with pytest.raises(ValueError):
            NaturalDecision.model_validate(
                {"intent": "checkpoint", "message": "Save the current progress.", field: "a" * 32}
            )
