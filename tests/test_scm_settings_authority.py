# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host data-preservation control, not native transaction qualification."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace


async def test_settings_update_preserves_private_mapping_head_without_public_reflection():
    from firefly_forge.collaboration import Collaboration, CollaborationSettings

    authority = {"version": 1, "heads": {"head": {"digest": "a" * 64}}, "requests": {}}
    resume_grants = {"version": 2, "heads": {"resume-head": {"digest": "b" * 64}}, "requests": {}}
    row = SimpleNamespace(
        data={
            **CollaborationSettings().model_dump(exclude={"webhook_secret"}),
            "_scm_authority_v1": authority,
            "_scm_resume_authority_v2": copy.deepcopy(resume_grants),
        }
    )

    @asynccontextmanager
    async def unit():
        yield None

    async def repository(_):
        return {"connection_provider": "github", "remote_repository_id": "42"}

    async def find(_):
        return row

    async def save(value):
        assert value is row

    async def repo_lock(_):
        pass

    service = object.__new__(Collaboration)
    service.store = SimpleNamespace(repository=repository, _unit=unit)
    service.settings_records = SimpleNamespace(find_for_repository=find, save=save)
    service._repo_lock = repo_lock
    service._secret = lambda _: ""
    before = copy.deepcopy(authority)
    result = await service.configure("a" * 32, CollaborationSettings(enabled=True))
    assert row.data["_scm_authority_v1"] == before
    assert "_scm_authority_v1" not in result
    assert row.data["_scm_resume_authority_v2"] == resume_grants
    assert "_scm_resume_authority_v2" not in result
    assert result["enabled"] is True
