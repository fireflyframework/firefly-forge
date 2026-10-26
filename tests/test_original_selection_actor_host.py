# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The retained mode administrator is independently locked during native admission."""
# ruff: noqa: F811

import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_checkpoint_mode_adoption_host import mode_case, request  # noqa: F401
from test_sandbox_command_ordinals_host import command_case  # noqa: F401


@pytest.mark.parametrize("distinct", [False, True])
async def test_retained_mode_actor_is_locked_inside_the_native_writer(mode_case, monkeypatch, distinct):
    from pyfly.data.relational.sqlalchemy import LockMode

    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.journal import digest
    from firefly_forge.original_selection_runtime import assert_retained_mode_actor

    c = mode_case
    candidate = await c.service.candidate(c.run_id)
    record = await c.service.request(c.run_id, uuid4().hex, request(candidate))
    expected = c.actor.id.hex
    subject = c.actor.subject
    if distinct:
        record = copy.deepcopy(record)
        payload = record["payload"]
        expected, subject = uuid4().hex, "distinct-original-mode-operator"
        payload["candidate"]["actor"].update(
            user_id=expected,
            identity_digest=digest(
                {
                    "issuer": c.actor.issuer,
                    "subject": subject,
                }
            ),
        )
        payload["candidate"]["digest"] = digest({k: v for k, v in payload["candidate"].items() if k != "digest"})
        payload["request"]["candidate_digest"] = payload["candidate"]["digest"]
    calls = []

    async def locked_identity(self, user_id, *, lock=None):
        calls.append((user_id.hex, lock))
        return SimpleNamespace(enabled=True, issuer=c.actor.issuer, subject=subject)

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", locked_identity)
    async with c.store._unit():
        await assert_retained_mode_actor(c.journal, c.store.run.data, record, lock=True)
    assert calls == [(expected, LockMode.PESSIMISTIC_WRITE)]
