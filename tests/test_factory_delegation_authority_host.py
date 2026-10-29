# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A mutable parent phase cannot invent or reopen a native implementation intent."""

from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from test_factory_delegation_ledger_host import ROOT, ledger

from firefly_forge.factory_delegations import FactoryDelegations
from firefly_forge.project_access import ProjectAccess


async def seed(tmp_path, monkeypatch):
    _, native = ledger()
    native.run.update(status="developing", active_task="implement", attempt=1)
    key = uuid5(NAMESPACE_URL, f"forge:{ROOT}:implement:1").hex
    task = SimpleNamespace(status="running", attempt=1, data={"idempotency_key": key, "intent": {}})

    async def current(*args):
        return task

    native._task = current

    async def permitted(*args):
        pass

    monkeypatch.setattr(ProjectAccess, "authorize_execution", permitted)
    await native._append(ROOT, "implement", key, "intent", {})

    async def ready(run):
        return {"status": "ready"}

    manager = FactoryDelegations(native.store, native, sources=tmp_path / "origins", readiness=ready)
    return manager, manager.bind(ROOT, key, tmp_path), native, task


async def test_exact_current_unfinished_native_intent_preserves_parent_work_phase(tmp_path, monkeypatch):
    manager, session, native, _ = await seed(tmp_path, monkeypatch)
    assert (await manager.authority(session))["status"] == "developing"
    assert len(native.rows) == 1


@pytest.mark.parametrize(
    "case", ["absent_intent", "duplicate_intent", "already_receipted", "boolean_attempt", "intent_body_drift"]
)
async def test_row_flags_cannot_invent_authority_or_reopen_completed_attempt(tmp_path, monkeypatch, case):
    manager, session, native, task = await seed(tmp_path, monkeypatch)
    if case == "absent_intent":
        native.rows.clear()
    elif case == "duplicate_intent":
        await native._append(ROOT, "implement", session.task_key, "intent", {})
    elif case == "already_receipted":
        await native._append(ROOT, "implement", session.task_key, "receipt", {})
    elif case == "boolean_attempt":
        native.run["attempt"] = True
    else:
        task.data["intent"] = {"different": True}
    with pytest.raises(ValueError):
        await manager.authority(session)
