# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A native finite ticket binding is not an inherited run or source grant."""

import importlib
import importlib.util

import pytest
from pydantic import ValidationError
from test_scm_polling import poll_case as poll_case


def module():
    assert importlib.util.find_spec("firefly_forge.scm_maintain_ticket_models"), (
        "Native maintain-ticket contract is missing"
    )
    return importlib.import_module("firefly_forge.scm_maintain_ticket_models")


def persisted_cut(case):
    import copy

    return {
        kind: {
            key: copy.deepcopy({name: value for name, value in vars(row).items() if name != "_sa_instance_state"})
            for key, row in rows.items()
        }
        for kind, rows in case.db.items()
    }


def body():
    return dict(
        version=1,
        request_id="a" * 32,
        run_id="b" * 32,
        creation_request_id="c" * 32,
        expected_creation_digest="d" * 64,
        observed_receipt_id="e" * 32,
        expected_reference_digest="f" * 64,
        expected_previous_digest=None,
        lifetime_seconds=1800,
        max_notifications=8,
        allowed_actions=["status", "question", "answer"],
        acknowledge_native_communication=True,
    )


def test_explicit_finite_native_communication_contract():
    value = module().MaintainTicketInput.model_validate(body())
    assert value.model_dump(mode="json") == body()


@pytest.mark.parametrize(
    "change",
    [
        {"issue_context": {}},
        {"actor": "owner"},
        {"lifetime_seconds": 0},
        {"lifetime_seconds": 3601},
        {"max_notifications": 33},
        {"allowed_actions": ["resume"]},
        {"allowed_actions": ["status", "status"]},
        {"acknowledge_native_communication": False},
        {"run_id": "foreign"},
        {"expected_previous_digest": "bad"},
        {"version": True},
        {"acknowledge_native_communication": 1},
        {"allowed_actions": ["status"]},
    ],
)
def test_no_untyped_authority_or_unbounded_consent(change):
    m = module()
    with pytest.raises(ValidationError):
        m.MaintainTicketInput.model_validate({**body(), **change})


def test_exact_request_digest_and_remote_identity_are_not_type_coerced():
    m = module()
    request = m.MaintainTicketInput.model_validate(body())
    assert m.request_digest(request) == m.request_digest(request.model_copy(deep=True))
    expected = "https://github.com/team/product/issues/4#issuecomment-51"
    assert m.confirmed_remote({"id": 51, "url": expected}, "https://github.com/team/product/issues/4") == {
        "id": "51",
        "url": expected,
    }
    for value in [True, 0, "051", None]:
        with pytest.raises(ValueError):
            m.confirmed_remote({"id": value, "url": expected}, "https://github.com/team/product/issues/4")
    with pytest.raises(ValueError):
        m.confirmed_remote(
            {"id": 51, "url": expected.replace("issues/4", "issues/5")}, "https://github.com/team/product/issues/4"
        )


async def prepared_binding(case, monkeypatch):
    import copy
    from types import SimpleNamespace
    from uuid import UUID, uuid4

    from firefly_forge.journal import digest

    service_module = importlib.import_module("firefly_forge.scm_maintain_tickets")
    await case.ready()
    await case.consent()
    parent_status = await case.new_command("status")
    await case.collaboration.process(parent_status["id"])
    case.state["clock"] += 3600
    case.state["comments"].append(case.comment(99, "@forge, keep me informed about the new maintenance unit"))
    for _ in range(3):
        await case.service.step(case.repository)
    observed = next(row.data for row in case.db["inbox"].values() if row.source_key == "comment:99")
    child = uuid4().hex
    reference = dict(
        version=1,
        run_id=case.run_id,
        request_id=uuid4().hex,
        expected_checkpoint_digest="1" * 64,
        expected_revision="2" * 40,
    )
    source = dict(
        version=1,
        operation="maintain",
        reference=reference,
        repository_id=case.repository,
        project_id=case.project,
        component_id="api",
        revision="2" * 40,
        source_tree="3" * 40,
        branch="forge/retained",
        remote_receipt_digest="4" * 64,
        journal_head="5" * 64,
        parent_scope_digest="6" * 64,
    )
    source["digest"] = digest(source)
    run = {
        **copy.deepcopy(case.db["runs"][case.run_id].data),
        "id": child,
        "operation": "maintain",
        "publish": False,
        "status": "queued",
        "source_checkpoint": reference,
        "source_checkpoint_snapshot": source,
    }
    for name in ("issue_context", "worker_request", "worker_response", "pause", "active_task"):
        run.pop(name, None)
    case.db["runs"][child] = SimpleNamespace(
        id=UUID(child), repository_id=UUID(case.repository), status="queued", data=run
    )
    creation = dict(
        request_id=uuid4().hex,
        run_id=child,
        repository_id=case.repository,
        project_id=case.project,
        component_id="api",
        input=dict(operation="maintain", publish=False, source_checkpoint=reference),
        source_checkpoint=source,
    )
    creation["digest"] = digest(creation)
    service = service_module.MaintainTickets(case.collaboration)

    async def retained_creation(self, request_id):
        assert request_id == creation["request_id"]
        return copy.deepcopy(creation)

    monkeypatch.setattr(service_module.MaintainTickets, "_creation", retained_creation)
    request = module().MaintainTicketInput.model_validate(
        {
            **body(),
            "run_id": child,
            "creation_request_id": creation["request_id"],
            "expected_creation_digest": creation["digest"],
            "observed_receipt_id": observed["id"],
            "expected_reference_digest": digest(observed["verification"]),
        }
    )
    return service, request


async def test_native_configuration_queues_one_server_owned_feedback_anchor(poll_case, monkeypatch):
    import copy

    assert importlib.util.find_spec("firefly_forge.scm_maintain_tickets"), (
        "Native maintain communication service is missing"
    )
    async with poll_case() as case:
        service, request = await prepared_binding(case, monkeypatch)
        parent = copy.deepcopy(case.db["runs"][case.run_id].data)
        previous_posts = copy.deepcopy(case.state["posts"])
        result = await service.configure(case.project, case.repository, request)
        assert result["request"] == request.model_dump(mode="json")
        assert result["creation_digest"] == request.expected_creation_digest
        assert result["parent_reference"]["run_id"] == case.run_id
        anchor = await case.collaboration.receipt(result["outbox_id"])
        assert anchor["maintain_ticket"]["binding_digest"] == result["digest"]
        assert anchor["run_id"] == request.run_id and anchor["feedback_status"] == "pending"
        assert "native_question" not in anchor and "issue_context" not in case.db["runs"][request.run_id].data
        assert case.db["runs"][case.run_id].data == parent and case.state["posts"] == previous_posts
        assert await service.configure(case.project, case.repository, request) == result


async def test_configured_native_owner_loop_posts_once_without_a_writer_transaction(poll_case, monkeypatch):
    async with poll_case() as case:
        service, request = await prepared_binding(case, monkeypatch)
        result = await service.configure(case.project, case.repository, request)
        before = len(case.state["posts"])
        await case.collaboration.feedback(result["outbox_id"])
        sent = await case.collaboration.receipt(result["outbox_id"])
        assert sent["feedback_status"] == "sent", sent
        assert len(case.state["posts"]) == before + 1
        assert case.state["depth"] == 0
        await case.collaboration.feedback(result["outbox_id"])
        assert len(case.state["posts"]) == before + 1


async def test_retained_binding_and_marker_have_exact_versioned_serialization(poll_case, monkeypatch):
    import copy

    m = module()
    async with poll_case() as case:
        service, request = await prepared_binding(case, monkeypatch)
        kept = await service.configure(case.project, case.repository, request)
        assert m.MaintainTicketReceipt.model_validate(kept).model_dump(mode="json") == kept
        anchor = await case.collaboration.receipt(kept["outbox_id"])
        assert (
            m.MaintainTicketMarker.model_validate(anchor["maintain_ticket"]).model_dump(mode="json")
            == anchor["maintain_ticket"]
        )
        for change in [
            {"version": True},
            {"revision": True},
            {"expires_at_ms": str(kept["expires_at_ms"])},
            {"creation_digest": "f" * 64},
            {"request_digest": "f" * 64},
            {"extra": "unrecognized"},
        ]:
            with pytest.raises(ValidationError):
                m.MaintainTicketReceipt.model_validate({**copy.deepcopy(kept), **change})


@pytest.mark.parametrize(
    "change", [{"operation": "test"}, {"publish": True}, {"component_id": "foreign"}, {"repository_id": "f" * 32}]
)
async def test_binding_cannot_retarget_or_upgrade_the_frozen_native_creation(poll_case, monkeypatch, change):
    from firefly_forge.project_access import AccessDenied

    async with poll_case() as c:
        service, request = await prepared_binding(c, monkeypatch)
        c.db["runs"][request.run_id].data.update(change)
        before = len(c.db["inbox"]), len(c.db["events"])
        with pytest.raises(AccessDenied):
            await service.configure(c.project, c.repository, request)
        assert (len(c.db["inbox"]), len(c.db["events"])) == before


async def test_failed_atomic_configuration_retains_no_binding_event_or_outbox(poll_case, monkeypatch):
    async with poll_case() as c:
        service, request = await prepared_binding(c, monkeypatch)
        before = persisted_cut(c)
        c.state["fail_save"] = True
        with pytest.raises(ValueError, match="persistence"):
            await service.configure(c.project, c.repository, request)
        assert persisted_cut(c) == before


async def test_idempotent_request_does_not_extend_expired_consent_and_unknown_cannot_be_regranted(
    poll_case, monkeypatch
):
    from uuid import uuid4

    import firefly_forge.scm_maintain_tickets as m
    from firefly_forge.project_access import AccessDenied

    async with poll_case() as c:
        service, request = await prepared_binding(c, monkeypatch)
        kept = await service.configure(c.project, c.repository, request)
        monkeypatch.setattr(m, "clock_ms", lambda: kept["expires_at_ms"])
        assert await service.configure(c.project, c.repository, request) == kept
        anchor = await c.collaboration.receipt(kept["outbox_id"])
        with pytest.raises(AccessDenied):
            await service.current(anchor)
        c.db["inbox"][kept["outbox_id"]].data["feedback_status"] = "needs_reconciliation"
        renewal = request.model_copy(update={"request_id": uuid4().hex, "expected_previous_digest": kept["digest"]})
        before = persisted_cut(c)
        with pytest.raises(AccessDenied, match="reconciliation"):
            await service.configure(c.project, c.repository, renewal)
        assert persisted_cut(c) == before


@pytest.mark.parametrize("change", ["extra_pointer", "wrong_digest", "foreign_marker", "wrong_owner"])
async def test_current_binding_rejects_changed_server_pointer_marker_or_owner(poll_case, monkeypatch, change):
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.scm_maintain_tickets import STATE_KEY

    async with poll_case() as c:
        service, request = await prepared_binding(c, monkeypatch)
        kept = await service.configure(c.project, c.repository, request)
        anchor = await c.collaboration.receipt(kept["outbox_id"])
        if change == "extra_pointer":
            c.db["settings"][c.repository].data[STATE_KEY]["heads"][request.run_id]["extra"] = True
        elif change == "wrong_digest":
            c.db["settings"][c.repository].data[STATE_KEY]["heads"][request.run_id]["digest"] = "f" * 64
        elif change == "foreign_marker":
            anchor["maintain_ticket"]["run_id"] = "f" * 32
        else:
            anchor["actor"] = "8"
        with pytest.raises((AccessDenied, ValueError)):
            await service.current(anchor)


async def test_expiration_during_final_binding_save_rolls_back_the_complete_native_binding(poll_case, monkeypatch):
    import firefly_forge.scm_maintain_tickets as module
    from firefly_forge.project_access import AccessDenied

    async with poll_case() as c:
        service, request = await prepared_binding(c, monkeypatch)
        tick = {"value": 1000}
        monkeypatch.setattr(module, "clock_ms", lambda: tick["value"])
        original = service.polling.settings.save

        async def save(row):
            result = await original(row)
            tick["value"] = 1000 + request.lifetime_seconds * 1000
            return result

        monkeypatch.setattr(service.polling.settings, "save", save)
        before = persisted_cut(c)
        with pytest.raises(AccessDenied, match="finite consent expired"):
            await service.configure(c.project, c.repository, request)
        assert persisted_cut(c) == before
