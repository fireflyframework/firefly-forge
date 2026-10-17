# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Original request ownership and cancellation with explicit host persistence doubles."""

import pytest
from test_run_creation_requests_host import REQUEST, request
from test_run_creation_requests_host import admission as admission
from test_run_creation_requests_host import creation as creation

from firefly_forge.journal import digest


def cancellation():
    from firefly_forge.run_creation_models import RunCreationCancelInput

    original = request().input.model_dump(mode="json", exclude_unset=True)
    return RunCreationCancelInput.model_validate(
        {
            "version": 1,
            "request_digest": digest({"version": 1, "input": original}),
            "repository_id": original["repository_id"],
            "project_id": None,
            "component_id": None,
        }
    )


async def test_explicit_cancel_before_original_post_prevents_any_future_admission(creation):
    manager, _, state, retained, _ = creation
    result, created = await manager.cancel(REQUEST, cancellation())
    assert created and result["outcome"] == "cancelled"
    assert result["cancellation"]["request_id"] == REQUEST
    assert await manager.decision(REQUEST) == result
    assert await manager.cancel(REQUEST, cancellation()) == (result, False)
    with pytest.raises(ValueError, match="cancelled"):
        await manager.admit(REQUEST, request())
    assert not state["rows"] and not state["events"] and not retained


async def test_explicit_cancel_during_original_preparation_fences_the_final_insert(creation, monkeypatch):
    manager, store, state, retained, _ = creation
    prepare = store.prepare_run
    cancelled = []

    async def cancel_during(request):
        assert state["depth"] == 0
        cancelled.append(await manager.cancel(REQUEST, cancellation()))
        return await prepare(request)

    monkeypatch.setattr(store, "prepare_run", cancel_during)
    with pytest.raises(ValueError, match="cancelled"):
        await manager.admit(REQUEST, request())
    assert cancelled[0][1] and cancelled[0][0]["outcome"] == "cancelled"
    assert not state["rows"] and not state["events"] and not retained


async def test_cancellation_after_admission_returns_exact_existing_v1_receipt(creation):
    manager, _, state, retained, _ = creation
    receipt, _ = await manager.admit(REQUEST, request())
    result, created = await manager.cancel(REQUEST, cancellation())
    assert not created and result == {"version": 1, "outcome": "admitted", "receipt": receipt}
    assert await manager.get(REQUEST) == receipt
    assert len(state["rows"]) == len(state["events"]) == len(retained) == 1


async def test_pending_quota_never_blocks_terminal_cancellation_and_excludes_resolved_ids(creation, monkeypatch):
    import firefly_forge.run_creation_control as control

    manager, store, state, _, _ = creation
    monkeypatch.setattr(control, "MAX_PENDING_RESERVATIONS", 2)

    async def refuse(*args, **kwargs):
        raise ValueError("Fictional preparation refusal")

    monkeypatch.setattr(store, "prepare_run", refuse)
    for key in ("1" * 32, "2" * 32):
        with pytest.raises(ValueError, match="preparation refusal"):
            await manager.admit(key, request())
    with pytest.raises(ValueError, match="Resolve"):
        await manager.admit("3" * 32, request())
    assert len(state["reservations"]) == 2
    assert (await manager.cancel("3" * 32, cancellation()))[0]["outcome"] == "cancelled"
    assert len(state["reservations"]) == 3
    await manager.cancel("1" * 32, cancellation())
    with pytest.raises(ValueError, match="preparation refusal"):
        await manager.admit("4" * 32, request())
    assert len(state["reservations"]) == 4 and len(state["cancellations"]) == 2
    assert not state["rows"] and not state["events"]


async def test_cancel_append_failure_rolls_back_its_new_reservation_too(creation, monkeypatch):
    manager, _, state, _, _ = creation
    save = manager.controls.cancellations.save

    async def fail(row):
        await save(row)
        raise ValueError("Fictional cancellation rollback")

    monkeypatch.setattr(manager.controls.cancellations, "save", fail)
    with pytest.raises(ValueError, match="rollback"):
        await manager.cancel(REQUEST, cancellation())
    assert not state["reservations"] and not state["cancellations"]
    with pytest.raises(KeyError):
        await manager.decision(REQUEST)


async def test_reservation_commits_before_preparation_and_does_not_store_original_input(creation, monkeypatch):
    manager, store, state, _, _ = creation
    state["guard"] = True

    async def refuse(*args, **kwargs):
        assert state["depth"] == 0
        row = state["reservations"][REQUEST]
        assert row.data["request_digest"] == cancellation().request_digest
        assert "brief" not in str(row.data) and "Fictional stock" not in str(row.data)
        raise ValueError("Fictional preparation refusal")

    monkeypatch.setattr(store, "prepare_run", refuse)
    with pytest.raises(ValueError, match="preparation refusal"):
        await manager.admit(REQUEST, request())
    assert len(state["reservations"]) == 1 and not state["rows"]
    with pytest.raises(KeyError):
        await manager.decision(REQUEST)
    assert (await manager.cancel(REQUEST, cancellation()))[0]["outcome"] == "cancelled"


async def test_reservation_owner_and_digest_prevent_foreign_or_changed_cancellation(creation, monkeypatch):
    from dataclasses import replace

    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

    manager, store, state, _, identity = creation

    async def refuse(*args, **kwargs):
        raise ValueError("Fictional preparation refusal")

    monkeypatch.setattr(store, "prepare_run", refuse)
    with pytest.raises(ValueError, match="preparation refusal"):
        await manager.admit(REQUEST, request())
    with pytest.raises(ValueError, match="different"):
        await manager.cancel(REQUEST, cancellation().model_copy(update={"request_digest": "0" * 64}))
    actor = CURRENT_PRINCIPAL.get()
    foreign = replace(actor, id=__import__("uuid").UUID("d" * 32), subject="other-owner")

    async def native_user(records, value, **kwargs):
        return type(identity)(id=foreign.id, issuer=foreign.issuer, subject=foreign.subject, enabled=True)

    async def read_scope(*args, **kwargs):
        pass

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", native_user)
    monkeypatch.setattr(ProjectAccess, "require_repository", read_scope)
    token = CURRENT_PRINCIPAL.set(foreign)
    try:
        with pytest.raises(AccessDenied, match="another user"):
            await manager.cancel(REQUEST, cancellation())
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert len(state["reservations"]) == 1 and not state["cancellations"] and not state["rows"]


async def test_oversized_control_is_rejected_before_loading_its_native_json(creation, monkeypatch):
    manager, _, _, _, _ = creation

    async def size(value):
        return 4097

    async def refuse(*args, **kwargs):
        raise AssertionError("Oversized control must not be materialized")

    monkeypatch.setattr(manager.controls.reservations, "envelope_bytes", size)
    monkeypatch.setattr(manager.controls.reservations, "find_by_id", refuse)
    with pytest.raises(ValueError, match="bounded read"):
        await manager.decision(REQUEST)


@pytest.mark.parametrize("field", ["request_digest", "receipt_digest", "authentication_method", "component_id"])
async def test_control_scalar_or_digest_tampering_never_returns_a_cancellation_decision(creation, field):
    manager, _, state, _, _ = creation
    await manager.cancel(REQUEST, cancellation())
    setattr(state["cancellations"][REQUEST], field, "tampered")
    with pytest.raises(ValueError, match="integrity"):
        await manager.decision(REQUEST)


@pytest.mark.parametrize("change", [{"version": True}, {"extra": 1}, {"component_id": "api"}, {"brief": "secret"}])
def test_cancel_input_refuses_unversioned_or_unnecessary_original_data(change):
    from firefly_forge.run_creation_models import RunCreationCancelInput

    with pytest.raises(ValueError):
        RunCreationCancelInput.model_validate({**cancellation().model_dump(), **change})


async def test_relogin_method_can_cancel_same_native_user_but_cannot_rebind_admission(creation, monkeypatch):
    from dataclasses import replace

    from firefly_forge.project_access import CURRENT_PRINCIPAL

    manager, store, state, _, _ = creation

    async def refuse(*args, **kwargs):
        raise ValueError("Fictional preparation refusal")

    monkeypatch.setattr(store, "prepare_run", refuse)
    with pytest.raises(ValueError, match="preparation refusal"):
        await manager.admit(REQUEST, request())
    actor = CURRENT_PRINCIPAL.get()
    token = CURRENT_PRINCIPAL.set(replace(actor, authentication_method="oidc_session"))
    try:
        with pytest.raises(ValueError, match="authentication method"):
            await manager.admit(REQUEST, request())
        outcome, _ = await manager.cancel(REQUEST, cancellation())
        assert outcome["cancellation"]["original_actor"]["authentication_method"] == "local_bootstrap"
        assert outcome["cancellation"]["cancelled_by"]["authentication_method"] == "oidc_session"
        assert await manager.decision(REQUEST) == outcome
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert len(state["reservations"]) == len(state["cancellations"]) == 1 and not state["rows"]


async def test_read_authority_allows_cancel_after_execute_loss_but_read_loss_does_not(creation, monkeypatch):
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    manager, _, state, _, _ = creation
    reads = []

    async def read_only(access, actor, repository, permission):
        reads.append(permission)
        if permission != "read":
            raise AccessDenied("Fictional execute loss")

    monkeypatch.setattr(ProjectAccess, "require_repository", read_only)
    with pytest.raises(AccessDenied, match="execute loss"):
        await manager.admit(REQUEST, request())
    assert len(state["reservations"]) == 1
    outcome, _ = await manager.cancel(REQUEST, cancellation())
    assert outcome["outcome"] == "cancelled"
    assert reads.count("execute") == 1

    async def denied(*args, **kwargs):
        raise AccessDenied("Fictional read loss")

    monkeypatch.setattr(ProjectAccess, "require_repository", denied)
    with pytest.raises(AccessDenied, match="read loss"):
        await manager.decision(REQUEST)
    with pytest.raises(AccessDenied, match="read loss"):
        await manager.cancel("1" * 32, cancellation())
    assert len(state["reservations"]) == len(state["cancellations"]) == 1


async def test_disabled_or_switched_native_owner_cannot_cancel_or_clear_an_original(creation, monkeypatch):
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

    manager, _, state, _, identity = creation
    identity.enabled = False
    with pytest.raises(AccessDenied):
        await manager.cancel(REQUEST, cancellation())
    assert not state["reservations"] and not state["cancellations"]
    identity.enabled = True
    find = ForgeIdentityUserRepository.find_by_id
    tokens = []

    async def switch(records, value, **kwargs):
        native = await find(records, value, **kwargs)
        tokens.append(CURRENT_PRINCIPAL.set(None))
        return native

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", switch)
    try:
        with pytest.raises(AccessDenied, match="identity changed"):
            await manager.cancel(REQUEST, cancellation())
    finally:
        for token in reversed(tokens):
            CURRENT_PRINCIPAL.reset(token)
    assert not state["reservations"] and not state["cancellations"]


async def test_read_refusal_after_cancel_commit_preserves_the_exact_terminal_decision(creation, monkeypatch):
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    manager, store, state, _, _ = creation
    admit = store.admit_credentials
    original_read = ProjectAccess.require_repository

    async def deny(*args, **kwargs):
        raise AccessDenied("Fictional late read revocation")

    async def late(value):
        await admit(value)
        if isinstance(value, dict) and value.get("request_id") == REQUEST and not state["depth"]:
            monkeypatch.setattr(ProjectAccess, "require_repository", deny)

    monkeypatch.setattr(store, "admit_credentials", late)
    with pytest.raises(AccessDenied, match="late read revocation"):
        await manager.cancel(REQUEST, cancellation())
    assert len(state["reservations"]) == len(state["cancellations"]) == 1
    assert not state["rows"] and not state["events"]
    monkeypatch.setattr(ProjectAccess, "require_repository", original_read)
    monkeypatch.setattr(store, "admit_credentials", admit)
    outcome = await manager.decision(REQUEST)
    assert outcome["outcome"] == "cancelled"
    assert outcome["cancellation"] == state["cancellations"][REQUEST].data
