# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native child scope yields parent compute while retaining its repository fence."""

import asyncio
from types import SimpleNamespace

import pytest

from firefly_forge.delegation_models import FamilyScheduling
from firefly_forge.engine import Engine

ROOT, FIRST, SECOND, ORDINARY, REPO = (value * 32 for value in "abcdf")


async def fixture(tmp_path, *, waiting=True, enabled=True):
    release = asyncio.Event()
    parent_release = asyncio.Event()
    started = []
    runs = {identity: {"id": identity, "repository_id": REPO} for identity in [ROOT, FIRST, SECOND, ORDINARY]}
    for identity in [FIRST, SECOND]:
        runs[identity]["factory_context"] = {"parent_run_id": ROOT, "family_id": ROOT}

    async def get_run(identity):
        return runs[identity]

    async def queued():
        return [ORDINARY]

    store = SimpleNamespace(path=tmp_path / "factory.db", runtime_roles=None, get_run=get_run, queued=queued)
    engine = Engine(store, tmp_path / "workspaces", concurrency=2)

    async def execute(identity):
        started.append(identity)
        await release.wait()

    engine.execute_run = execute

    async def scheduling_scope():
        return FamilyScheduling(
            waiting_parents=frozenset([ROOT]) if waiting else frozenset(), ready_children=(FIRST, SECOND)
        )

    async def execute_child(identity, callback):
        await callback(identity)

    engine.factory_delegations = (
        SimpleNamespace(scheduling_scope=scheduling_scope, execute_child=execute_child) if enabled else None
    )
    engine.active[ROOT] = asyncio.create_task(parent_release.wait())
    engine.active_projects[ROOT] = REPO
    return engine, started, release, parent_release


@pytest.mark.parametrize("waiting,expected", [(True, 2), (False, 1)])
async def test_child_workspaces_run_in_parallel_without_parent_repository_deadlock(tmp_path, waiting, expected):
    engine, started, release, parent_release = await fixture(tmp_path, waiting=waiting)
    try:
        await engine.dispatch_queued()
        await asyncio.sleep(0)
        assert len(started) == expected
        assert set(started) <= {FIRST, SECOND}
        assert ORDINARY not in started
        assert engine.active_projects[ROOT] == REPO
        assert all(engine.active_projects[identity] == REPO for identity in started)
    finally:
        release.set()
        parent_release.set()
        await asyncio.gather(*engine.active.values())


async def test_absent_team_runtime_keeps_ordinary_same_repository_exclusion(tmp_path):
    engine, started, release, parent_release = await fixture(tmp_path, enabled=False)
    try:
        await engine.dispatch_queued()
        assert started == [] and list(engine.active) == [ROOT]
    finally:
        release.set()
        parent_release.set()
        await asyncio.gather(*engine.active.values())
