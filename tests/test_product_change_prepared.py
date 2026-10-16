# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared/locked primitive with explicit host persistence seams, never native acceptance."""

import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_collaboration import product

from firefly_forge.product_lifecycle import FeatureChangeInput, ProductLifecycle
from firefly_forge.product_provenance import content_digest
from firefly_forge.store import Store


@pytest.fixture
async def lifecycle_harness(monkeypatch):
    state = {"locked": False, "admissions": 0, "rows": [], "drift": False}
    import pyfly.data.transaction

    monkeypatch.setattr(pyfly.data.transaction, "current_unit_of_work", lambda: object() if state["locked"] else None)
    original = dict(
        repository_id="a" * 32,
        revision=1,
        definition=product().model_dump(mode="json"),
        created_at="original",
        origin={"kind": "manual"},
    )
    original["digest"] = content_digest(original)
    previous = SimpleNamespace(revision=1, data=copy.deepcopy(original))

    @asynccontextmanager
    async def unit():
        prior = state["locked"]
        before = copy.deepcopy(state["rows"])
        state["locked"] = True
        try:
            yield
        except BaseException:
            state["rows"] = before
            raise
        finally:
            state["locked"] = prior

    async def admit(value):
        assert not state["locked"], "Credential I/O must be outside all native transactions"
        state["admissions"] += 1

    async def current(repo):
        return previous

    async def replay(repo, request):
        return next((row for row in state["rows"] if row.data["change_receipt"]["request_id"] == request), None)

    async def save(row):
        assert state["locked"]
        state["rows"].append(row)

    async def product_read(repo):
        return copy.deepcopy(previous.data)

    async def lock(repo):
        assert state["locked"]

    def reject(value):
        pass

    store = SimpleNamespace(_unit=unit, admit_credentials=admit, reject_credentials=reject)
    collaboration = SimpleNamespace(
        store=store,
        product=product_read,
        _repo_lock=lock,
        products=SimpleNamespace(find_current=current, find_change=replay, save=save),
    )
    request = FeatureChangeInput(
        request_id="b" * 32, base_revision=1, base_digest=original["digest"], append_journeys=["Escalate ticket"]
    )
    return ProductLifecycle(collaboration), state, previous, request, original


async def test_prepared_change_admits_credentials_before_locked_atomic_write(lifecycle_harness):
    lifecycle, state, previous, request, original = lifecycle_harness
    prepared = await lifecycle.prepare("a" * 32, request)
    assert state["rows"] == [] and state["admissions"] >= 1
    async with lifecycle.store._unit():
        result = await lifecycle.apply_prepared_locked("a" * 32, request, prepared, actor_id=UUID(int=1))
    assert result["revision"] == 2 and len(state["rows"]) == 1
    assert result["change_receipt"]["actor_id"] == UUID(int=1).hex
    assert result["change_receipt"]["base_digest"] == original["digest"]
    assert result["definition"]["journey"] == [*original["definition"]["journey"], "Escalate ticket"]
    assert not result["definition"]["functional_reviewed"]
    assert result["digest"] == content_digest({key: value for key, value in result.items() if key != "digest"})


async def test_prepared_drift_or_mutation_cannot_write(lifecycle_harness):
    lifecycle, state, previous, request, _ = lifecycle_harness
    prepared = await lifecycle.prepare("a" * 32, request)
    previous.data["digest"] = "f" * 64
    async with lifecycle.store._unit():
        with pytest.raises(ValueError, match="base|changed"):
            await lifecycle.apply_prepared_locked("a" * 32, request, prepared, actor_id=UUID(int=1))
    assert state["rows"] == []


async def test_prepared_replay_requires_same_native_actor(lifecycle_harness):
    lifecycle, state, _, request, _ = lifecycle_harness
    prepared = await lifecycle.prepare("a" * 32, request)
    async with lifecycle.store._unit():
        one = await lifecycle.apply_prepared_locked("a" * 32, request, prepared, actor_id=UUID(int=1))
        assert await lifecycle.apply_prepared_locked("a" * 32, request, prepared, actor_id=UUID(int=1)) == one
        with pytest.raises(ValueError, match="actor|replay"):
            await lifecycle.apply_prepared_locked("a" * 32, request, prepared, actor_id=UUID(int=2))
    assert len(state["rows"]) == 1


async def test_prepared_write_rechecks_real_credentials_registered_during_save(lifecycle_harness, monkeypatch):
    lifecycle, state, previous, request, original_product = lifecycle_harness
    prepared = await lifecycle.prepare("a" * 32, request)
    admissions = state["admissions"]
    registry = {}
    store = SimpleNamespace(
        credential_registry=lambda: registry, configuration=SimpleNamespace(resolve=lambda provider: {})
    )
    monkeypatch.setattr(lifecycle.store, "reject_credentials", lambda value: Store.reject_credentials(store, value))
    original = lifecycle._save_prepared

    async def save_then_register(*args, **kwargs):
        result = await original(*args, **kwargs)
        await asyncio.sleep(0)
        registry["OWNED_FIXTURE_API_KEY"] = "Escalate ticket"
        return result

    monkeypatch.setattr(lifecycle, "_save_prepared", save_then_register)
    with pytest.raises(ValueError, match="configured credentials"):
        async with lifecycle.store._unit():
            await lifecycle.apply_prepared_locked("a" * 32, request, prepared, actor_id=UUID(int=1))
    assert state["admissions"] == admissions
    assert state["rows"] == [] and previous.data == original_product
    assert "change_receipt" not in previous.data
