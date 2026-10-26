# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A later resume inherits the admitted modes without replacing the frozen provider."""
# ruff: noqa: F811

import copy
from uuid import uuid4

import pytest
from test_checkpoint_mode_adoption_host import mode_case, request  # noqa: F401
from test_sandbox_command_ordinals_host import command_case  # noqa: F401


async def paused_adopted(case):
    candidate = await case.service.candidate(case.run_id)
    record = await case.service.request(case.run_id, uuid4().hex, request(candidate))
    case.store.run.status = "paused"
    case.store.run.data.update(status="paused", pause={"kind": "rate_limit", "automatic": True, "retry_at": 1.0})
    case.store.tasks.rows[0].status = "paused"
    return record


@pytest.mark.parametrize("agent", [None, "bridge"])
async def test_resume_keeps_original_revision_and_mode_receipt_without_a_new_snapshot(mode_case, monkeypatch, agent):
    c = mode_case
    record = await paused_adopted(c)
    before = copy.deepcopy(c.store.run.data)
    task = copy.deepcopy(c.store.tasks.rows[0])
    revisions = sorted(c.store.configuration.revisions.iterdir())

    def forbidden_snapshot(*args, **kwargs):
        raise AssertionError("An adopted resume must not replace its original configuration")

    monkeypatch.setattr(c.store.configuration, "snapshot", forbidden_snapshot)
    result = await c.journal.resume(c.run_id, agent=agent)
    assert result["status"] == "queued"
    assert result["config_revision"] == before["config_revision"]
    assert result["checkpoint_mode_adoption"] == before["checkpoint_mode_adoption"]
    assert result["checkpoint_mode_adoption"]["record_digest"] == record["digest"]
    assert result.get("model_selection") == before.get("model_selection")
    for key in ["repair_budget", "implementation_rounds", "sandbox_snapshot", "tool_policy_snapshot", "commands"]:
        assert result.get(key) == before.get(key)
    assert sorted(c.store.configuration.revisions.iterdir()) == revisions
    assert c.store.tasks.rows[0].attempt == task.attempt + 1


@pytest.mark.parametrize(
    "agent,profile",
    [("codex", None), ("claude", None), ("agentic", None), ("bridge", "another-profile"), (None, "another-profile")],
)
async def test_adopted_resume_cannot_switch_provider_or_profile(mode_case, agent, profile):
    c = mode_case
    await paused_adopted(c)
    before = copy.deepcopy(c.store.run.data)
    prior = await c.journal.entries(c.run_id)
    with pytest.raises(ValueError, match="mode adoption"):
        await c.journal.resume(c.run_id, agent=agent, model_profile_id=profile)
    assert c.store.run.data == before
    assert await c.journal.entries(c.run_id) == prior


async def test_later_resume_rechecks_the_adopting_native_identity(mode_case, monkeypatch):
    from types import SimpleNamespace

    from firefly_forge.bridge_completions import BridgeCompletions

    c = mode_case
    await paused_adopted(c)
    before = copy.deepcopy(c.store.run.data)

    async def changed_identity(self, user_id):
        return SimpleNamespace(issuer=c.actor.issuer, subject="different-native-identity")

    monkeypatch.setattr(BridgeCompletions, "_identity", changed_identity)
    with pytest.raises(ValueError, match="identity changed"):
        await c.journal.resume(c.run_id)
    assert c.store.run.data == before


async def test_automatic_retry_inherits_modes_and_only_increments_its_existing_retry_counter(mode_case):
    c = mode_case
    await paused_adopted(c)
    before = copy.deepcopy(c.store.run.data)
    result = await c.journal.resume(c.run_id, auto=True)
    assert result["retry_count"] == int(before.get("retry_count", 0)) + 1
    assert result["config_revision"] == before["config_revision"]
    assert result["checkpoint_mode_adoption"] == before["checkpoint_mode_adoption"]
    assert result["implementation_rounds"] == before["implementation_rounds"]
    assert result["sandbox_snapshot"] == before["sandbox_snapshot"]


async def test_inherited_provider_digest_is_checked_before_any_resume_write(mode_case):
    c = mode_case
    await paused_adopted(c)
    before = copy.deepcopy(c.store.run.data)
    prior = await c.journal.entries(c.run_id)
    with pytest.raises(ValueError, match="provider snapshot changed"):
        await c.journal.resume(c.run_id, agent="bridge", expected_provider_digest="0" * 64)
    assert c.store.run.data == before
    assert await c.journal.entries(c.run_id) == prior


async def test_inherited_resume_does_not_read_configuration_inside_the_writer(mode_case, monkeypatch):
    from firefly_forge.journal import digest

    c = mode_case
    record = await paused_adopted(c)
    selected = c.store.configuration.load_snapshot(record["payload"]["execution_config_revision"])
    expected = digest({"agent": "bridge", "settings": selected})
    original = c.store.configuration.load_snapshot

    def outside_writer(revision):
        assert not c.store.depth.get(), "Configuration I/O inside a native writer"
        return original(revision)

    monkeypatch.setattr(c.store.configuration, "load_snapshot", outside_writer)
    result = await c.journal.resume(c.run_id, agent="bridge", expected_provider_digest=expected)
    assert result["status"] == "queued"
