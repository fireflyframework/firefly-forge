# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Original creation identity controls using explicit host persistence/ACL doubles."""

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_run_admission_prepared_host import REPOSITORY
from test_run_admission_prepared_host import admission as admission

from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.run_creation_models import RunCreationInput
from firefly_forge.run_creation_requests import RunCreationRequests

REQUEST = "e" * 32


@pytest.fixture
async def creation(admission, monkeypatch):
    store, state, _, _ = admission
    actor = Principal(UUID("f" * 32), "local:bootstrap", "operator", authentication_method="local_bootstrap")
    identity = SimpleNamespace(id=actor.id, enabled=True, issuer=actor.issuer, subject=actor.subject)
    retained = {}
    parent_unit = store._unit

    @asynccontextmanager
    async def unit(**kwargs):
        before = copy.deepcopy(retained)
        try:
            async with parent_unit(**kwargs) as session:
                yield session
        except BaseException:
            retained.clear()
            retained.update(before)
            raise

    async def current_identity(value, **kwargs):
        return identity if value == identity.id else None

    async def administrator(access, principal):
        return identity.enabled

    async def require_repository(access, principal, repository, permission):
        assert principal == actor and repository == REPOSITORY and permission in {"read", "execute", "release"}

    async def find(value, **kwargs):
        return retained.get(value.hex)

    async def size(value):
        row = retained.get(value.hex)
        return len(json.dumps(row.data).encode()) if row is not None else None

    async def save(row):
        assert state["depth"] and len(state["rows"]) == len(state["events"]) == 1
        assert row.id.hex not in retained
        retained[row.id.hex] = row

    from firefly_forge.identity_persistence import ForgeIdentityUserRepository

    async def identity_find(records, value, **kwargs):
        return await current_identity(value, **kwargs)

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity_find)
    monkeypatch.setattr(ProjectAccess, "current_administrator", administrator)
    monkeypatch.setattr(ProjectAccess, "require_repository", require_repository)
    monkeypatch.setattr(store, "_unit", unit)
    records = SimpleNamespace(find_by_id=find, envelope_bytes=size, save=save)
    manager = RunCreationRequests(store, records=records)
    import firefly_forge.run_creation_requests as module

    monkeypatch.setattr(module, "current_unit_of_work", lambda: object() if state["depth"] else None)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        yield manager, store, state, retained, identity
    finally:
        CURRENT_PRINCIPAL.reset(token)


def request(**fields):
    return RunCreationInput.model_validate(
        {"version": 1, "input": {"repository_id": REPOSITORY, "brief": "Fictional stock", **fields}}
    )


async def test_lost_committed_creation_response_reads_exact_original_and_never_prepares_again(creation, monkeypatch):
    manager, store, state, retained, _ = creation
    original, created = await manager.admit(REQUEST, request())
    assert created and len(retained) == len(state["rows"]) == len(state["events"]) == 1
    state["rows"][original["run_id"]]["status"] = "succeeded"

    async def refuse(*args, **kwargs):
        raise AssertionError("Readback or replay must not prepare another run")

    monkeypatch.setattr(store, "prepare_run", refuse)
    assert await manager.get(REQUEST) == original
    repeated, created = await manager.admit(REQUEST, request())
    assert not created and repeated == original
    assert len(state["rows"]) == len(state["events"]) == 1
    assert original["original_actor"] == {"user_id": "f" * 32, "authentication_method": "local_bootstrap"}
    assert original["input"] == {"repository_id": REPOSITORY, "brief": "Fictional stock"}
    assert "execution_config" not in original and "status" not in original


@pytest.mark.parametrize("fields", [{"commands": []}, {"skills": []}, {"agent": "codex"}, {"brief": "Changed"}])
async def test_occupied_identity_refuses_changed_meaningful_input_without_second_run(creation, fields):
    manager, _, state, retained, _ = creation
    await manager.admit(REQUEST, request())
    with pytest.raises(ValueError, match="another|different"):
        await manager.admit(REQUEST, request(**fields))
    assert len(state["rows"]) == len(state["events"]) == len(retained) == 1


async def test_failed_receipt_append_rolls_back_run_event_and_receipt(creation, monkeypatch):
    manager, _, state, retained, _ = creation
    save = manager.records.save

    async def fail(row):
        await save(row)
        raise ValueError("Owned receipt failure")

    monkeypatch.setattr(manager.records, "save", fail)
    with pytest.raises(ValueError, match="receipt failure"):
        await manager.admit(REQUEST, request())
    assert state["rows"] == {} and state["events"] == [] and retained == {}


async def test_missing_original_is_read_only_and_disabled_owner_cannot_read_or_replay(creation):
    manager, _, state, retained, identity = creation
    with pytest.raises(KeyError):
        await manager.get(REQUEST)
    assert not retained and not state["rows"]
    await manager.admit(REQUEST, request())
    identity.enabled = False
    with pytest.raises(AccessDenied):
        await manager.get(REQUEST)
    with pytest.raises(AccessDenied):
        await manager.admit(REQUEST, request())
    assert len(state["rows"]) == len(retained) == 1


async def test_credential_refusal_after_commit_preserves_original_receipt(creation, monkeypatch):
    manager, store, state, retained, _ = creation
    admit = store.admit_credentials

    async def late(value):
        await admit(value)
        if not state["depth"] and isinstance(value, dict) and value.get("request_id") == REQUEST:
            raise ValueError("Current credential readback refusal")

    monkeypatch.setattr(store, "admit_credentials", late)
    with pytest.raises(ValueError, match="readback refusal"):
        await manager.admit(REQUEST, request())
    assert len(state["rows"]) == len(state["events"]) == len(retained) == 1
    monkeypatch.setattr(store, "admit_credentials", admit)
    assert (await manager.get(REQUEST))["run_id"] in state["rows"]


@pytest.mark.parametrize("version", [True, False, "1", 2])
def test_creation_envelope_requires_exact_version(version):
    with pytest.raises(ValueError):
        RunCreationInput.model_validate({"version": version, "input": {"repository_id": REPOSITORY, "brief": "Stock"}})


async def test_new_admission_refuses_ambient_unit_before_preparation(creation, monkeypatch):
    manager, store, state, retained, _ = creation

    async def refuse(*args, **kwargs):
        raise AssertionError("Ambient admission must fail before preparing settings")

    monkeypatch.setattr(store, "prepare_run", refuse)
    async with store._unit():
        with pytest.raises(ValueError, match="caller transaction"):
            await manager.admit(REQUEST, request())
    assert not state["rows"] and not retained


async def test_authless_or_foreign_owner_cannot_create_or_read_original(creation):
    manager, _, state, retained, _ = creation
    original, _ = await manager.admit(REQUEST, request())
    for actor in (None, Principal(UUID("d" * 32), "local:bootstrap", "other", authentication_method="local_bootstrap")):
        token = CURRENT_PRINCIPAL.set(actor)
        try:
            with pytest.raises(AccessDenied):
                await manager.get(REQUEST)
            with pytest.raises(AccessDenied):
                await manager.admit(REQUEST, request())
        finally:
            CURRENT_PRINCIPAL.reset(token)
    assert len(state["rows"]) == len(retained) == 1
    assert await manager.get(REQUEST) == original


async def test_reader_context_switch_inside_identity_lookup_cannot_expose_receipt(creation, monkeypatch):
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository

    manager, _, _, _, _ = creation
    await manager.admit(REQUEST, request())
    find = ForgeIdentityUserRepository.find_by_id
    tokens = []

    async def switched(records, value, **kwargs):
        row = await find(records, value, **kwargs)
        tokens.append(CURRENT_PRINCIPAL.set(None))
        return row

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", switched)
    try:
        with pytest.raises(AccessDenied, match="identity changed"):
            await manager.get(REQUEST)
    finally:
        for token in reversed(tokens):
            CURRENT_PRINCIPAL.reset(token)


async def test_oversized_stored_envelope_is_refused_before_loading_json(creation, monkeypatch):
    manager, _, _, _, _ = creation

    async def oversized(value):
        return 256 * 1024 + 1

    async def refuse(*args, **kwargs):
        raise AssertionError("Oversized JSON must not be materialized")

    monkeypatch.setattr(manager.records, "envelope_bytes", oversized)
    monkeypatch.setattr(manager.records, "find_by_id", refuse)
    with pytest.raises(ValueError, match="bounded read"):
        await manager.get(REQUEST)


async def test_scalar_or_digest_tampering_does_not_return_a_receipt(creation):
    manager, _, _, retained, _ = creation
    await manager.admit(REQUEST, request())
    retained[REQUEST].receipt_digest = "0" * 64
    with pytest.raises(ValueError, match="integrity"):
        await manager.get(REQUEST)


async def test_input_is_detached_before_preparation_await(creation, monkeypatch):
    manager, store, state, _, _ = creation
    submitted = request()
    prepare = store.prepare_run

    async def mutated(body, **kwargs):
        submitted.input.brief = "Changed caller object"
        return await prepare(body, **kwargs)

    monkeypatch.setattr(store, "prepare_run", mutated)
    receipt, _ = await manager.admit(REQUEST, submitted)
    assert receipt["input"]["brief"] == "Fictional stock"
    assert state["rows"][receipt["run_id"]]["brief"] == "Fictional stock"


async def test_last_credential_await_is_followed_by_current_authority_before_commit(creation, monkeypatch):
    manager, store, state, retained, _ = creation
    admit, scope = store.admit_credentials, manager._scope
    cut = {"receipt_admissions": 0, "revoked": False}

    async def credentials(value):
        await admit(value)
        if state["depth"] and isinstance(value, dict) and value.get("request_id") == REQUEST:
            cut["receipt_admissions"] += 1
            if cut["receipt_admissions"] == 2:
                cut["revoked"] = True

    async def authority(*args, **kwargs):
        if cut["revoked"]:
            raise AccessDenied("Current execution authority expired during credential admission")
        await scope(*args, **kwargs)

    monkeypatch.setattr(store, "admit_credentials", credentials)
    monkeypatch.setattr(manager, "_scope", authority)
    with pytest.raises(AccessDenied, match="expired"):
        await manager.admit(REQUEST, request())
    assert not state["rows"] and not state["events"] and not retained
