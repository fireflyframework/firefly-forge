# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Ambiguous Bridge outcomes refuse before any provider or transaction preparation."""

import copy
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from test_bridge_outcomes import MemoryStore

from firefly_forge.journal import Journal


@pytest.mark.parametrize("boundary", ["provider_outcome_unknown", "bridge_admission_refused", "completion"])
@pytest.mark.parametrize("selection", ["handoff", "profile", "verification", "mode"])
async def test_unknown_resume_never_reads_credentials_routes_or_mode_authority(boundary, selection):
    effects = []

    class Store(MemoryStore):
        @property
        def configuration(self):
            effects.append("configuration")
            raise AssertionError("Provider configuration must remain unread")

        @asynccontextmanager
        async def _unit(self):
            effects.append("transaction")
            raise AssertionError("Resume writer must remain unopened")
            yield

    class Router:
        async def resolve(self, *args):
            effects.append("router")
            raise AssertionError("Model routing must remain unread")

    store = Store()
    store.task_router = Router()
    store.run.status = "needs_reconciliation"
    store.run.data = {"status": "needs_reconciliation", "pause": {"kind": boundary}}
    if boundary == "completion":
        store.run.data["bridge_completion_hold"] = {"digest": "unresolved"}
    kwargs = (
        {"agent": "claude"}
        if selection == "handoff"
        else {"model_profile_id": "maintenance"}
        if selection == "profile"
        else {"verification_handoff_id": "frozen", "agent": "bridge"}
        if selection == "verification"
        else {}
    )
    if selection == "mode":
        store.run.data["checkpoint_mode_adoption"] = {"unread": "retained-mode"}
    before = copy.deepcopy(store.run.data)
    expected = "Bridge completion history" if boundary == "completion" else "Bridge.*reconcil"
    with pytest.raises(ValueError, match=expected):
        await Journal(store).resume(UUID(int=1).hex, **kwargs)
    assert store.run.data == before and effects == [] and store.events == [] and store.finished == []
