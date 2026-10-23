# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Same-task lease-session guards; actual PostgreSQL commit loss is a separate native gate."""

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pyfly.data.transaction import UnitOfWork

from firefly_forge import store as module


def context(monkeypatch):
    store = object.__new__(module.Store)
    store._ownership_lock = asyncio.Lock()
    store.execution_stopping = False
    store.schema = "public_fixture"
    sync = object()
    connection = SimpleNamespace(sync_connection=sync, closed=False, invalidated=False, commit=AsyncMock())
    session = SimpleNamespace(connection=AsyncMock(return_value=connection), scalar=AsyncMock(return_value=True))
    unit = UnitOfWork(SimpleNamespace(), "primary", session)
    store._ownership_connection, store._ownership_pid = connection, 271
    cut = SimpleNamespace(
        store=store, task=asyncio.current_task(), unit=unit, session=session, connection=connection, pid=271
    )
    variable = getattr(
        module, "_source_budget_admission", ContextVar("controlled_source_budget_admission", default=None)
    )
    monkeypatch.setattr(module, "_source_budget_admission", variable, raising=False)
    monkeypatch.setattr(module, "current_unit_of_work", lambda: unit)
    variable.set(cut)
    return store, cut, unit


async def test_nested_owned_check_uses_same_session_without_reacquiring_lock_or_committing(monkeypatch):
    store, cut, _ = context(monkeypatch)
    await store._ownership_lock.acquire()
    try:
        async with asyncio.timeout(0.2):
            await store.assert_ownership()
    finally:
        store._ownership_lock.release()
    assert cut.session.scalar.await_count == 1
    cut.connection.commit.assert_not_awaited()


@pytest.mark.parametrize("changed", ["store", "unit", "resource", "owner", "completed"])
async def test_foreign_native_provenance_is_rejected_before_any_sql(monkeypatch, changed):
    store, cut, unit = context(monkeypatch)
    if changed == "store":
        cut.store = object()
    elif changed == "unit":
        monkeypatch.setattr(module, "current_unit_of_work", lambda: object())
    elif changed == "resource":
        unit.resource = object()
    elif changed == "owner":
        unit.owner_task = object()
    else:
        from pyfly.data.transaction.unit_of_work import UnitStatus

        unit.status = UnitStatus.COMMITTED
    with pytest.raises(RuntimeError, match="tasks, stores or units"):
        await store.assert_ownership()
    cut.session.connection.assert_not_awaited()
    cut.session.scalar.assert_not_awaited()


async def test_copied_child_task_cannot_check_or_join_the_lease_unit(monkeypatch):
    store, cut, _ = context(monkeypatch)
    store.session_provider = SimpleNamespace(unit=lambda: pytest.fail("Foreign task reached provider"))
    store._initialized = True

    async def child():
        with pytest.raises(RuntimeError, match="tasks, stores or units"):
            await store.assert_ownership()
        with pytest.raises(RuntimeError, match="tasks, stores or units"):
            async with store._unit():
                pytest.fail("Foreign task entered lease unit")

    await asyncio.create_task(child())
    cut.session.connection.assert_not_awaited()
    cut.session.scalar.assert_not_awaited()


@pytest.mark.parametrize("changed", ["drain", "pid", "connection", "closed", "invalidated", "physical", "lease"])
async def test_lost_exact_lease_is_rejected(monkeypatch, changed):
    store, cut, _ = context(monkeypatch)
    if changed == "drain":
        store.execution_stopping = True
    elif changed == "pid":
        store._ownership_pid += 1
    elif changed == "connection":
        store._ownership_connection = object()
    elif changed in {"closed", "invalidated"}:
        setattr(cut.connection, changed, True)
    elif changed == "physical":
        cut.session.connection.return_value = SimpleNamespace(sync_connection=object())
    else:
        cut.session.scalar.return_value = False
    with pytest.raises(RuntimeError, match="lease"):
        await store.assert_ownership()
    cut.connection.commit.assert_not_awaited()


@pytest.mark.parametrize("changed", ["drain", "pid", "connection"])
async def test_authority_is_rechecked_after_ownership_query(monkeypatch, changed):
    store, cut, _ = context(monkeypatch)

    async def query(*args):
        if changed == "drain":
            store.execution_stopping = True
        elif changed == "pid":
            store._ownership_pid += 1
        else:
            store._ownership_connection = object()
        return True

    cut.session.scalar.side_effect = query
    with pytest.raises(RuntimeError, match="lease"):
        await store.assert_ownership()
    cut.connection.commit.assert_not_awaited()


async def test_cancellation_keeps_cut_and_lock_until_shielded_settlement_finishes(monkeypatch):
    from contextlib import asynccontextmanager

    from pyfly.data.transaction.template import shielded

    from firefly_forge import native_question_owned_unit

    current = ContextVar("controlled_native_unit", default=None)
    variable = ContextVar("controlled_budget_cut", default=None)
    monkeypatch.setattr(module, "current_unit_of_work", current.get)
    monkeypatch.setattr(module, "_source_budget_admission", variable)
    store = object.__new__(module.Store)
    store._ownership_lock = asyncio.Lock()
    store.execution_stopping = False
    store.schema = "public_fixture"
    store._engine = object()
    store._ownership_pid = 271
    connection = SimpleNamespace(sync_connection=object(), closed=False, invalidated=False)
    store._ownership_connection = connection
    store._assert_ownership = AsyncMock()
    session = SimpleNamespace(
        connection=AsyncMock(return_value=connection), scalar=AsyncMock(return_value=True), execute=AsyncMock()
    )
    entered, settle, released = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []

    class Provider:
        def __init__(self, registry):
            pass

        @asynccontextmanager
        async def unit(self):
            unit = UnitOfWork(SimpleNamespace(), "primary", session)
            token = current.set(unit)
            try:
                yield session
            finally:

                async def cleanup():
                    observed.append(variable.get())
                    assert store._ownership_lock.locked()
                    with pytest.raises(RuntimeError, match="tasks, stores or units"):
                        await store.assert_ownership()
                    entered.set()
                    await settle.wait()
                    assert variable.get() is observed[-1]
                    assert store._ownership_lock.locked()
                    released.set()

                try:
                    await shielded(cleanup())
                finally:
                    current.reset(token)

    monkeypatch.setattr(module, "SessionProvider", Provider)
    monkeypatch.setattr(
        native_question_owned_unit, "QuestionLeaseTransactionManager", lambda *a: SimpleNamespace(datasource="primary")
    )

    async def work():
        try:
            async with store.source_budget_admission_unit():
                await store.assert_ownership()
        finally:
            assert variable.get() is None

    task = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not released.is_set() and store._ownership_lock.locked()
    settle.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set() and not store._ownership_lock.locked()
    async with store._ownership_lock:
        assert variable.get() is None
    assert len(observed) == 1


@pytest.mark.parametrize("name", ["store.py", "native_question_owned_unit.py"])
def test_allowance_contract_binds_its_native_lease_implementation(monkeypatch, name):
    from pathlib import Path

    from firefly_forge.source_budget_allowances import admission_contract_digest

    before = admission_contract_digest()
    read = Path.read_bytes

    def changed(path):
        value = read(path)
        return value + b"\n# controlled changed lease contract\n" if path.name == name else value

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert admission_contract_digest() != before
