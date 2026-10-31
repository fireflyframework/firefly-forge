# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Every retained public response rechecks current credential-history admission."""

from types import SimpleNamespace

import pytest
from test_github_projects_models import connection

from firefly_forge.github_projects import GitHubProjects


async def test_configuration_replay_rechecks_current_credential_guard():
    manager = object.__new__(GitHubProjects)
    called = []

    async def refuse(value):
        called.append(value)
        raise ValueError("Current credential history refuses this response")

    manager.store = SimpleNamespace(admit_credentials=refuse)
    incoming = connection()
    public = {
        key: incoming[key]
        for key in (
            "enabled",
            "connection_name",
            "owner_kind",
            "owner",
            "number",
            "board_id",
            "field_id",
            "state_options",
            "include_existing",
        )
    }
    public.update(digest="a" * 64, revision=1, board_url="https://github.com/users/octocat/projects/2")
    with pytest.raises(ValueError, match="Current credential history"):
        await manager.configuration_result(
            {"project_id": "1" * 32, "repository_id": "2" * 32, "public": public}, object()
        )
    assert len(called) == 1


async def test_view_rechecks_native_actor_after_awaited_credential_admission():
    from contextlib import asynccontextmanager

    from firefly_forge.project_access import AccessDenied

    manager = object.__new__(GitHubProjects)
    revoked = False

    async def authority(*args, **kwargs):
        if revoked:
            raise AccessDenied("Actor revoked during response admission")

    async def binding(*args):
        return None

    async def administrator(actor):
        return True

    async def scalar(query):
        return None

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace(scalar=scalar)

    async def credentials(value):
        nonlocal revoked
        revoked = True

    manager.authority, manager.binding = authority, binding
    manager.access = SimpleNamespace(current_administrator=administrator)
    manager.store = SimpleNamespace(_unit=unit, admit_credentials=credentials)
    with pytest.raises(AccessDenied, match="revoked during response"):
        await manager.view("1" * 32, "2" * 32, object())


async def test_configuration_replay_rechecks_actor_after_credential_admission():
    from contextlib import asynccontextmanager

    from firefly_forge.github_projects_models import BoardConnectionInputV1
    from firefly_forge.project_access import AccessDenied

    manager = object.__new__(GitHubProjects)
    revoked = False
    incoming = connection()
    actor = object()
    public = {
        key: incoming[key]
        for key in (
            "enabled",
            "connection_name",
            "owner_kind",
            "owner",
            "number",
            "board_id",
            "field_id",
            "state_options",
            "include_existing",
        )
    }
    public.update(digest="a" * 64, revision=1, board_url="https://github.com/users/octocat/projects/2")
    row = SimpleNamespace(
        data={
            "input": incoming,
            "actor": {"id": "actor"},
            "project_id": "1" * 32,
            "repository_id": "2" * 32,
            "public": public,
        }
    )

    async def authority(*args, **kwargs):
        if revoked:
            raise AccessDenied("Actor revoked during response admission")

    async def scalar(query):
        return row

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace(scalar=scalar)

    async def credentials(value):
        nonlocal revoked
        revoked = True

    manager.authority = authority
    manager.access = SimpleNamespace(actor_snapshot=lambda current: {"id": "actor"})
    manager.store = SimpleNamespace(_unit=unit, admit_credentials=credentials)
    with pytest.raises(AccessDenied, match="revoked during response"):
        await manager.configure("1" * 32, "2" * 32, BoardConnectionInputV1.model_validate(incoming), actor)
