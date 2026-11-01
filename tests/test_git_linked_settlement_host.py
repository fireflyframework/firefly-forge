# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Full completed consumer, with explicit fictional build inputs and writer context."""

import copy
from types import SimpleNamespace

import pytest
from test_linked_verification_consumer_host import admit_and_execute

from firefly_forge.git_linked_verification import assert_linked_git_closed
from firefly_forge.project_access import CURRENT_PRINCIPAL

pytest_plugins = ["test_linked_verification_consumer_host"]


async def test_complete_git_settlement_revalidates_inputs_then_fences_exact_native_writer(linked_case, monkeypatch):
    from firefly_forge import agentic_checkpoint_invocation, git_linked_verification, linked_verifications

    c, env = linked_case, linked_case.env
    result = await admit_and_execute(c)
    assert result["status"] == "completed", result
    monkeypatch.setattr(
        agentic_checkpoint_invocation, "_runtime_material_digest", lambda: result["candidate"]["runtime_contract"]
    )
    monkeypatch.setattr(linked_verifications, "_policy_material_digest", lambda: result["candidate"]["policy_digest"])
    monkeypatch.setattr(linked_verifications, "profile_digest", lambda: result["candidate"]["profile_digest"])
    deliveries = SimpleNamespace(store=env.store, journal=env.journal, engine=env.engine)
    prepared = await assert_linked_git_closed(deliveries, env.run_id)
    assert prepared is not None
    before = copy.deepcopy((await env.store.get_run(env.run_id), await env.journal.entries(env.run_id)))
    c.inputs_state["value"] = "changed-after-completion"
    with pytest.raises(ValueError, match="inputs changed"):
        await assert_linked_git_closed(deliveries, env.run_id)
    c.inputs_state["value"] = "original"
    # This models a native writer context; physical PostgreSQL ownership is qualified separately.
    monkeypatch.setattr(git_linked_verification, "current_unit_of_work", lambda: object())
    monkeypatch.setattr(
        agentic_checkpoint_invocation, "_runtime_material_digest", lambda: result["candidate"]["runtime_contract"]
    )
    monkeypatch.setattr(linked_verifications, "_policy_material_digest", lambda: result["candidate"]["policy_digest"])
    monkeypatch.setattr(linked_verifications, "profile_digest", lambda: result["candidate"]["profile_digest"])
    with pytest.raises(ValueError, match="before its native Git writer"):
        await assert_linked_git_closed(deliveries, env.run_id)
    assert await assert_linked_git_closed(deliveries, env.run_id, prepared=prepared) is prepared
    c.inputs_state["value"] = "changed-inside-writer"
    with pytest.raises(ValueError, match="dependency input drift"):
        await assert_linked_git_closed(deliveries, env.run_id, prepared=prepared)
    c.inputs_state["value"] = "original"
    owner = env.store.execution_ownership_token
    env.store.execution_ownership_token = object()
    with pytest.raises(ValueError, match="before its native Git writer"):
        await assert_linked_git_closed(deliveries, env.run_id, prepared=prepared)
    env.store.execution_ownership_token = owner
    token = CURRENT_PRINCIPAL.set(None)
    try:
        with pytest.raises(ValueError, match="before its native Git writer"):
            await assert_linked_git_closed(deliveries, env.run_id, prepared=prepared)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    monkeypatch.setattr(linked_verifications, "_policy_material_digest", lambda: "0" * 64)
    with pytest.raises(ValueError, match="before its native Git writer"):
        await assert_linked_git_closed(deliveries, env.run_id, prepared=prepared)
    assert (await env.store.get_run(env.run_id), await env.journal.entries(env.run_id)) == before
    assert len(c.calls) == len(env.store.run.data["commands"])
