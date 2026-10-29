# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Continuity authority is part of the installed runtime and qualified admission closure."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_sandbox_command_ordinals_host import command

pytest_plugins = ["test_sandbox_command_ordinals_host"]
MODULES = (
    "framework_knowledge.py",
    "framework_knowledge_bootstrap.py",
    "framework_knowledge_conformance.py",
    "framework_knowledge_controllers.py",
    "framework_knowledge_evidence.py",
    "framework_knowledge_generation.py",
    "framework_knowledge_models.py",
    "framework_knowledge_persistence.py",
    "framework_knowledge_requirements.py",
    "framework_knowledge_sync.py",
    "framework_knowledge_targets.py",
    "framework_knowledge_upstream.py",
    "framework_knowledge_worker.py",
    "knowledge.py",
    "knowledge_first.py",
    "learning_context.py",
    "profiles.py",
    "api.py",
    "access_routes.py",
    "migrations.py",
    "invalid_read_contract.py",
    "invalid_read_epochs.py",
    "invalid_read_continuity.py",
    "invalid_read_continuities.py",
    "invalid_read_continuity_models.py",
    "invalid_read_continuity_controllers.py",
    "invalid_read_reconstruction.py",
    "invalid_read_lease.py",
    "invalid_read_tickets.py",
    "invalid_read_settlements.py",
)


@pytest.mark.parametrize("module", MODULES)
def test_each_continuity_module_is_covered_by_both_installed_closures(monkeypatch, module):
    from firefly_forge.agentic_checkpoint_invocation import runtime_contract
    from firefly_forge.source_budget_allowances import admission_contract_digest

    original = Path.read_bytes
    before = runtime_contract(), admission_contract_digest()
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: original(self) + (b"# mutation" if self.name == module else b"")
    )
    after = runtime_contract(), admission_contract_digest()
    assert all(left != right for left, right in zip(before, after, strict=True))


async def test_two_new_completed_native_commands_preserve_retained_custody_without_replay(command_case):
    from firefly_forge.invalid_read_continuities import InvalidReadContinuities

    case = command_case
    service = InvalidReadContinuities(case.store, case.journal, SimpleNamespace(journal=case.journal))
    run = await case.store.get_run(case.run_id)
    task = {"idempotency_key": "a" * 32, "attempt": 1}
    before = await service._command_cut(run, task, [])
    await command(case, ["first-new-command"])
    first = [row.data for row in case.store.records.rows]
    assert await service._command_cut(run, task, first) == before
    await command(case, ["second-new-command"])
    second = [row.data for row in case.store.records.rows]
    assert await service._command_cut(run, task, second) == before
    assert case.calls == [["first-new-command"], ["second-new-command"]]
    assert [row.ordinal for row in case.store.sandboxes.rows] == [1, 2]
    row = case.store.sandboxes.rows[-1]
    row.data["effects"]["start"] = {"status": "intent"}
    row.status = "running"
    from firefly_forge.sandbox import SandboxReconciliationRequired

    with pytest.raises(SandboxReconciliationRequired):
        await service._command_cut(run, task, second)
    assert len(case.calls) == 2


async def test_real_retained_command_budget_charges_new_commands_once_and_never_resets(command_case):
    import copy

    from firefly_forge.sandbox_models import SandboxInput, capture_sandbox

    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(SandboxInput(command_limit=22))
    task = case.store.tasks.rows[0]
    task.task_id, task.attempt = "implement", 2

    async def invoke(argv, ordinal=None):
        return await case.fresh().execute(
            case.run_id, case.workspace, case.workspace, argv, 2, "implement", ordinal=ordinal
        )

    for index in range(11):
        await invoke(["retained-" + str(index)])
    retained = copy.deepcopy([row.data for row in case.store.sandboxes.rows])
    first = await invoke(["new-one"])
    second = await invoke(["new-two"])
    assert len(case.store.sandboxes.rows) == len(case.calls) == 13
    assert [row.ordinal for row in case.store.sandboxes.rows] == list(range(1, 14))
    assert all(row.parent_attempt == 2 for row in case.store.sandboxes.rows)
    assert [row.data for row in case.store.sandboxes.rows][:11] == retained
    assert await invoke(["new-one"], 12) == first
    assert await invoke(["new-two"], 13) == second
    assert len(case.store.sandboxes.rows) == len(case.calls) == 13
    for index in range(9):
        await invoke(["remaining-" + str(index)])
    assert len(case.store.sandboxes.rows) == len(case.calls) == 22
    with pytest.raises(ValueError, match="budget"):
        await invoke(["must-not-run"])
    assert len(case.store.sandboxes.rows) == len(case.calls) == 22
    assert await invoke(["new-one"], 12) == first
    assert len(case.store.sandboxes.rows) == len(case.calls) == 22
