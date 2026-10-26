# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real command/verification/ACK producers with memory rows, not native SQL proof."""

import copy
from uuid import uuid4

import pytest
from test_checkpoint_mode_adoption_host import request
from test_sandbox_command_ordinals_host import command_case as command_case
from test_unknown_command_acknowledgement_host import (
    adopt_and_begin,
    consent,
    service,
)
from test_unknown_command_acknowledgement_host import (
    unknown_case as unknown_case,
)

from firefly_forge.checkpoint_mode_adoptions import CheckpointModeAdoptions
from firefly_forge.checkpoint_mode_runtime import mode_adoption_scope, prepare_current_mode
from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.process import ProcessResult
from firefly_forge.project_access import AccessDenied


async def test_old_ack_policy_and_original_unknown_custody_survive_only_explicit_mode_lineage(
    unknown_case, monkeypatch
):
    import firefly_forge.unknown_command_acknowledgements as acknowledgement

    c = unknown_case
    c.store.run.data.update(agent="bridge", repair_budget=2)
    c.store.run.data.update(c.store.configuration.snapshot("bridge"))
    old_snapshot = c.store.run.data["config_revision"]
    api = service(c, monkeypatch)
    with monkeypatch.context() as old_policy:
        old_policy.setattr(acknowledgement, "policy_digest", lambda: "a" * 64)
        earlier = await api.candidate(c.run_id, c.unknown.id.hex, agent="bridge", adoption_request_id=uuid4().hex)
        earlier_ack = await api.request(c.run_id, c.unknown.id.hex, uuid4().hex, consent(earlier))
        reviewed = await api.candidate(c.run_id, c.unknown.id.hex, agent="bridge", adoption_request_id=uuid4().hex)
        original_ack = await api.request(c.run_id, c.unknown.id.hex, uuid4().hex, consent(reviewed))
        assert earlier_ack["payload"]["actor"] == original_ack["payload"]["actor"]
        assert (
            earlier_ack["payload"]["candidate"]["policy_digest"]
            == original_ack["payload"]["candidate"]["policy_digest"]
        )
        intent = await adopt_and_begin(c, reviewed, monkeypatch)

        async def known_red(config, argv, *args):
            return ProcessResult(argv, 1, "Known controlled RED")

        c.backend.run = known_red
        await c.fresh().execute(c.run_id, c.workspace, c.workspace, ["fixture-check"], 2, "implement", ordinal=3)
        await c.journal.verification_command(c.run_id, intent, 0, check_current=lambda _: None)
        await c.journal.verification_finish(c.run_id, intent, check_current=lambda _: None)
        await c.journal._append(
            c.run_id,
            "handoff",
            uuid4().hex,
            "handoff",
            {
                "automatic": True,
                "verification_handoff_id": intent["id"],
                "agent": "bridge",
                "config_revision": old_snapshot,
            },
        )
        task = c.store.tasks.rows[0]
        task.status, task.attempt = "queued", 2
        c.store.run.data.update(attempt=2, implementation_pending=True)

        async def plan(*args):
            return {}

        monkeypatch.setattr(c.journal, "ensure_plan", plan)
        await c.journal.intent(c.run_id, "implement", 2, {"config_revision": old_snapshot})
        await c.journal.verification_provider_admission(c.run_id)
    c.store.run.data.update(status="failed", error="native_agentic_checkpoint_wal_required", active_task="implement")
    c.store.run.status = "failed"
    c.engine.active = {}
    c.engine.tool_broker = None
    c.store.configuration.update(
        "bridge", ProviderConfiguration(values={"checkpoints": "required", "completion_recovery": "required"})
    )
    adoption = CheckpointModeAdoptions(c.store, c.journal, c.engine)
    candidate = await adoption.candidate(c.run_id)
    assert len(candidate["acknowledgements"]) == 1
    assert candidate["acknowledgements"][0]["record_digest"] == original_ack["digest"]
    # This projection isolates grant selection; native callers also validate the whole WAL.
    wrong_grant_projection = [
        entry for entry in await c.journal.entries(c.run_id) if entry["digest"] != original_ack["digest"]
    ]
    with pytest.raises(ValueError, match="custody"):
        await adoption._command_custody(await c.store.get_run(c.run_id), {"attempt": 2}, wrong_grant_projection)
    before = copy.deepcopy((c.unknown.data, c.unknown.status))
    receipt = await adoption.request(c.run_id, uuid4().hex, request(candidate))
    prepared = await prepare_current_mode(c.journal, c.run_id)
    with mode_adoption_scope(prepared):
        claimed = await c.journal.intent(c.run_id, "implement", 2)
        assert claimed["disposition"] == "execute"
        await c.journal.verification_provider_admission(c.run_id)
        c.backend.run = c.successful_backend
        result = await c.fresh().execute(c.run_id, c.workspace, c.workspace, ["new-adopted-tool"], 2, "implement")
        assert result.exit_code == 0
        before_calls = len(c.calls)
        original_receipt = c.journal.sandbox_effect_receipt

        async def revoke_after_create(*args, **kwargs):
            value = await original_receipt(*args, **kwargs)
            if kwargs.get("effect") == "create":
                c.allowed = False
            return value

        monkeypatch.setattr(c.journal, "sandbox_effect_receipt", revoke_after_create)
        with pytest.raises((AccessDenied, ValueError)):
            await c.fresh().execute(c.run_id, c.workspace, c.workspace, ["refused-after-create"], 2, "implement")
        assert len(c.calls) == before_calls
        closed = c.store.sandboxes.rows[-1]
        assert closed.data["effects"]["cleanup"]["status"] == "done"
        assert closed.data["effects"].get("start") in (None, {"status": "intent"})
    assert (c.unknown.data, c.unknown.status) == before
    assert receipt["payload"]["candidate"]["proof"]["original_config_revision"] == old_snapshot
    assert c.store.run.data["config_revision"] == old_snapshot and c.store.tasks.rows[0].attempt == 2
    assert [(row.parent_attempt, row.ordinal) for row in c.store.sandboxes.rows] == [
        (1, 1),
        (1, 2),
        (1, 3),
        (2, 1),
        (2, 2),
    ]
