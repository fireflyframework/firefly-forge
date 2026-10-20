# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Engine/Journal/SandboxExecutor; memory persistence and external backend only."""

from __future__ import annotations

import sys

import pytest
from test_bridge_inflight_cancel import RUN, MemoryStore, Rows

from firefly_forge.agent_roles import RoleRegistry
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.process import ProcessResult
from firefly_forge.providers import AgentResult
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.sandbox_models import capture_sandbox
from firefly_forge.tool_broker import tool_policy_snapshot
from firefly_forge.verification_handoffs import VerificationHandoffs, automatic_candidate


class SandboxRows(Rows):
    async def for_run(self, run):
        return self.rows

    async def slot(self, run, task, attempt, ordinal):
        return next(
            (
                row
                for row in self.rows
                if row.run_id == run
                and row.task_id == task
                and row.parent_attempt == attempt
                and row.ordinal == ordinal
            ),
            None,
        )


@pytest.mark.parametrize("queued_drift", [None, "source"])
async def test_repair_zero_gets_exactly_one_native_verification_continuation(tmp_path, monkeypatch, queued_drift):
    store = MemoryStore(tmp_path / "memory")
    store.schema = "host_automatic_verification"
    store.configuration = ConfigurationStore(tmp_path / "private")
    store.credential_registry = dict
    store.reject_credentials = lambda value: None
    store.run.data["id"] = RUN

    async def authorized(*args, **kwargs):
        pass

    store.assert_ownership = store.assert_campaign_current = authorized
    store.run.data.update(
        store.configuration.snapshot("codex"),
        agent="codex",
        commands=[{"argv": ["approved-check"], "timeout": 30}],
        sandbox_snapshot=capture_sandbox(None),
        tool_policy_snapshot=tool_policy_snapshot(store.configuration),
        role_snapshot=RoleRegistry(tmp_path / "roles").snapshot(),
    )
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=Rows(), sandboxes=SandboxRows())

    class Worker:
        calls = 0

        async def execute(self, workspace, brief, backend, frontend, emit):
            self.calls += 1
            return AgentResult(
                "Typed verification readiness",
                "host",
                usage={
                    "worker_feedback": {
                        "id": f"feedback-{self.calls}",
                        "role_id": "implementer",
                        "task_id": "implement",
                        "attempt": self.calls,
                        "disposition": "blocked",
                        "summary": "Needs configured verification evidence",
                        "request": {
                            "id": f"request-{self.calls}",
                            "kind": "verification",
                            "question": "Run the approved verification commands?",
                            "context": "Frozen approved commands",
                        },
                    }
                },
            )

    class Backend:
        calls = 0

        async def run(self, config, argv, cwd, timeout, limit, **kwargs):
            self.calls += 1
            assert argv == ["approved-check"]
            return ProcessResult(argv, 0, "Actual backend control result")

    worker, backend = Worker(), Backend()
    engine = Engine(store, tmp_path / "workspaces", lambda _: worker, journal=journal)
    workspace = engine.workspaces / RUN
    workspace.mkdir()
    (workspace / "service.py").write_text("VALUE = 1\n")
    engine.sandbox = SandboxExecutor(
        store, journal, backends=type("Backends", (), {"select": lambda self, config: backend})()
    )

    async def inspect(*args):
        return {"base_revision": "b" * 40, "head": "b" * 40, "source_tree": "c" * 40}

    async def checked(argv, *args, **kwargs):
        assert argv == ["git", "ls-files", "-z"]
        return ""

    monkeypatch.setattr(engine, "_inspect_workspace", inspect)
    monkeypatch.setattr("firefly_forge.engine.checked", checked)
    service = VerificationHandoffs(store, journal, engine)
    errors = []
    original_hold = journal.verification_hold
    original_refuse = journal.verification_refuse

    async def hold(*args):
        errors.append(str(sys.exception()))
        return await original_hold(*args)

    monkeypatch.setattr(journal, "verification_hold", hold)

    async def refuse(*args):
        errors.append(str(sys.exception()))
        return await original_refuse(*args)

    monkeypatch.setattr(journal, "verification_refuse", refuse)
    await engine.execute_run(RUN)
    first = await store.get_run(RUN)
    assert worker.calls == 1 and first["status"] == "paused", first.get("error")
    assert first["implementation_rounds"] == 1 and first["repair_budget"] == 0
    await service.execute(RUN)
    resumed = await store.get_run(RUN)
    assert resumed["status"] == "queued", errors or resumed.get("verification_refusal")
    assert backend.calls == 1
    assert await journal.verification_continuation_allowance(RUN) == 1
    assert (await journal.sandbox_executions(RUN))[0]["effects"]["start"]["status"] == "done"
    if queued_drift:
        (workspace / "late.py").write_text("VALUE = 2\n")
    await engine.execute_run(RUN)
    second = await store.get_run(RUN)
    if queued_drift:
        assert worker.calls == 1 and second["status"] == "needs_reconciliation"
        return
    assert worker.calls == 2 and second["status"] == "paused", second.get("error")
    assert second["implementation_rounds"] == 2 and second["repair_budget"] == 0
    await service.execute(RUN)
    refused = await store.get_run(RUN)
    assert refused["worker_request"]["status"] == "pending"
    assert refused["verification_refusal"]["status"] == "held"
    assert not automatic_candidate(refused)
    assert worker.calls == 2 and backend.calls == 1
