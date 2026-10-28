# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Preparation uses real retained mode/selection validation with controlled custody seams."""
# ruff: noqa: F811

import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_checkpoint_mode_adoption_host import mode_case  # noqa: F401
from test_original_selection_readmission_host import readmission_request, selection_case  # noqa: F401
from test_sandbox_command_ordinals_host import command_case  # noqa: F401

from firefly_forge.checkpoint_continuations import CheckpointContinuations
from firefly_forge.checkpoint_mode_runtime import effective_model_selection, prepared_mode
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
from firefly_forge.journal import digest
from firefly_forge.project_access import AccessDenied


async def preparation_case(c, monkeypatch, historical):
    import firefly_forge.checkpoint_continuations as module
    import firefly_forge.invalid_read_continuities as continuities
    import firefly_forge.invalid_read_epochs as epochs

    selected = await c.selection_service.candidate(c.run_id)
    record = await c.selection_service.request(c.run_id, uuid4().hex, readmission_request(selected))
    if historical:
        monkeypatch.setattr(epochs, "LEGACY_EPOCH", epochs.historical_policies(SimpleNamespace(**selected)))
        monkeypatch.setattr("firefly_forge.original_selection_runtime.policy_digest", lambda: "1" * 64)
        monkeypatch.setattr("firefly_forge.checkpoint_mode_runtime.policy_digest", lambda: "2" * 64)
        monkeypatch.setattr("firefly_forge.unknown_command_acknowledgements.policy_digest", lambda: "3" * 64)
    from firefly_forge.checkpoint_mode_runtime import effective_revision

    c.store.configuration._write(
        c.store.configuration.path,
        {"bridge": c.store.configuration.load_snapshot(effective_revision(c.store.run.data))},
    )
    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(version=1, enabled=True, allow_checkpoint_continuation=True, max_window_seconds=180),
    )
    monkeypatch.setattr(continuities, "current_profile_seal", lambda: {"profile_id": "controlled", "digest": "a" * 64})
    c.store.run.data["status"] = c.store.run.status = "needs_reconciliation"
    c.store.tasks.rows[0].status = "needs_reconciliation"
    run = await c.store.get_run(c.run_id)
    task = {"status": "needs_reconciliation", "attempt": 2}
    entries = await c.journal.entries(c.run_id)
    ticket = {
        "id": uuid4().hex,
        "status": "needs_reconciliation",
        "admission_seal": "a" * 64,
        "binding": {"contract": epochs.LEGACY_RUNTIME_CONTRACT},
    }
    payload = {
        "version": 2,
        "request_id": "fbr1_" + "c" * 32,
        "intent_digest": "b" * 64,
        "checkpoint_digest": "c" * 64,
        "snapshot": "controlled serializer seam",
        "invocation": {
            "invocation_id": "d" * 32,
            "ordinal": 1,
            "predecessor": None,
            "brief": "Controlled preparation",
            "backend": "python",
            "frontend": "react",
            "round": 2,
            "deadline_ms": 1,
            "grants": [],
            "contract": "e" * 64,
            "run_cut": "f" * 64,
            "workspace": {"path": "controlled", "head": "a", "source": "b", "metadata": "c"},
        },
    }
    from test_bridge_completion_records import snapshot

    descriptor, _ = snapshot()
    descriptor["endpoint"] = "https://example.test/v1"
    frontier = {
        "checkpoint": {"payload": payload, "digest": "c" * 64},
        "intent": {"digest": "b" * 64, "payload": {"descriptor": descriptor}},
        "response": {"digest": "d" * 64},
        "usage": {},
        "limits": {},
    }
    calls = []

    async def ticket_read(*args):
        return copy.deepcopy(ticket)

    async def native(*args):
        return copy.deepcopy((run, task, entries))

    async def no_effect(*args, **kwargs):
        return None

    async def operator(*args):
        return {}

    async def bundle(*args):
        return {}, {}, {}, b""

    async def source_guard(*args, **kwargs):
        return no_effect

    async def source(*args, **kwargs):
        return payload["invocation"]["workspace"]

    async def reconstruct(cp, descriptor, current):
        prepared = prepared_mode(current)
        assert prepared is not None and prepared.selection is not None
        assert prepared.selection.record == record
        assert effective_model_selection(current) == prepared.selection.model_selection
        assert epochs._active(current) is not None if historical else epochs._active(current) is None
        calls.append(digest(prepared.selection.model_selection))

    service = object.__new__(CheckpointContinuations)
    service.base = SimpleNamespace(
        _ticket=ticket_read,
        _operator=operator,
        _administrator=operator,
        _native=native,
        _admission=lambda *args: {},
        _custody=no_effect,
    )
    service.store, service.journal = c.store, c.journal
    service.engine = SimpleNamespace(stopping=False, active={}, workspaces=c.workspace.parent)
    service.checkpoints = SimpleNamespace(restore_bundle=bundle)
    service.continuities = SimpleNamespace(restores=SimpleNamespace(source_read_guard=source_guard), _actor=operator)

    async def events(*args):
        return []

    service._events = events
    monkeypatch.setattr(module, "_prior", lambda *args: None)
    monkeypatch.setattr(module, "pending_checkpoint", lambda *args: frontier)
    monkeypatch.setattr(module, "workspace_cut", source)
    monkeypatch.setattr("firefly_forge.pending_checkpoint_reconstruction.reconstruct_pending_request", reconstruct)
    return service, ticket, calls, copy.deepcopy((run, entries))


@pytest.mark.parametrize("historical", [False, True])
async def test_candidate_prepares_real_retained_selection_in_its_policy_epoch(selection_case, monkeypatch, historical):
    c = selection_case
    service, ticket, calls, before = await preparation_case(c, monkeypatch, historical)
    value = await service.candidate(c.run_id, ticket["id"])
    assert value["admission"] == "READY" and len(calls) == 1
    assert (await c.store.get_run(c.run_id), await c.journal.entries(c.run_id)) == before
    with pytest.raises(ValueError, match="must be prepared"):
        effective_model_selection(await c.store.get_run(c.run_id))


@pytest.mark.parametrize("fault", ["serializer", "authority", "configuration"])
async def test_inspection_failure_cannot_leak_mode_or_epoch_or_change_native_custody(
    selection_case, monkeypatch, fault
):
    import firefly_forge.invalid_read_epochs as epochs
    import firefly_forge.pending_checkpoint_reconstruction as reconstruction

    c = selection_case
    service, ticket, calls, before = await preparation_case(c, monkeypatch, True)
    inspect = reconstruction.reconstruct_pending_request

    async def fail(*args):
        await inspect(*args)
        if fault == "serializer":
            raise ValueError("Controlled serializer refusal")
        if fault == "authority":
            c.allowed = False
        else:
            c.store.configuration._write(c.store.configuration.path, {"bridge": {}})

    monkeypatch.setattr(reconstruction, "reconstruct_pending_request", fail)
    with pytest.raises(AccessDenied if fault == "authority" else ValueError):
        await service.candidate(c.run_id, ticket["id"])
    assert len(calls) == 1
    current = await c.store.get_run(c.run_id)
    assert (current, await c.journal.entries(c.run_id)) == before
    assert epochs._active(current) is None
    with pytest.raises(ValueError, match="must be prepared"):
        effective_model_selection(current)
