# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional consumed output survives a real selected-workspace native transfer."""

import copy
import hashlib
import os
import shutil
from functools import wraps
from pathlib import Path
from uuid import uuid4

import pytest
from final_outcome_native_fixtures import enable_final, final_input, seed_consumed_final

from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.engine import Engine
from firefly_forge.native_location_maintenance import NativeLocationMaintenance
from firefly_forge.native_location_models import (
    NativeLocationAcknowledgmentV1,
    NativeLocationFilesV1,
    NativeLocationPlanV1,
)
from firefly_forge.native_location_proof import required_native_files
from firefly_forge.source_budget_allowances import SourceBudgetPolicy, write_policy
from firefly_forge.source_catalog import SourceCatalogBudget
from firefly_forge.verification_handoffs import VerificationHandoffs

pytest_plugins = ["test_invalid_read_continuity_native"]


@pytest.fixture(autouse=True)
def standard_native_workspace_layout(monkeypatch):
    original = Engine.__init__

    @wraps(original)
    def initialize(self, store, workspaces, *args, **kwargs):
        return original(self, store, store.path.parent / "workspaces", *args, **kwargs)

    monkeypatch.setattr(Engine, "__init__", initialize)


async def test_native_relocated_known_final_accepts_once_and_runs_original_check(
    native_continuity, monkeypatch, tmp_path
):
    c = native_continuity
    old_tool, new_tool = tmp_path / "old-runtime", tmp_path / "new-runtime"
    (old_tool / "bin").mkdir(parents=True, mode=0o700)
    old_tool.chmod(0o700)
    script = old_tool / "bin" / "approved-check"
    script.write_text("#!/bin/sh\nprintf 'relocated native verification\\n'\n")
    script.chmod(0o700)
    shutil.copytree(old_tool, new_tool)
    await c.store.update(c.run_id, commands=[dict(argv=[str(script)], timeout=2)])
    failed = await seed_consumed_final(c, monkeypatch)
    enable_final(c)
    write_policy(c.store.configuration, SourceBudgetPolicy())
    before_run = copy.deepcopy(await c.store.get_run(c.run_id))
    before_entries = await c.journal.entries(c.run_id)
    before_commands = await c.journal.sandbox_executions(c.run_id)
    old = c.store.path.parent
    target = tmp_path / "relocated-native"
    await c.store.release_ownership()
    shutil.copytree(old, target)
    c.store.path = target / "factory.db"
    c.store.configuration = ConfigurationStore(target, existing_only=True)
    c.engine.workspaces = target / "workspaces"
    from firefly_forge.verification_dependencies import prepare_dependency_capture

    async def unchanged():
        pass

    runtime = await prepare_dependency_capture(
        [new_tool],
        tmp_path / "runtime-scratch",
        SourceCatalogBudget(min_free_bytes=0),
        unchanged,
        portable_root=new_tool,
    )
    plan = NativeLocationPlanV1.model_validate(
        {
            "version": 1,
            "source_storage": str(old),
            "target_storage": str(target),
            "workspaces": [
                {
                    "run_id": c.run_id,
                    "source": str(old / "workspaces" / c.run_id),
                    "target": str(target / "workspaces" / c.run_id),
                }
            ],
            "verification_runtimes": [
                {
                    "run_id": c.run_id,
                    "source": str(old_tool),
                    "target": str(new_tool),
                    "mode": "byte_identical",
                    "source_content_digest": runtime.digest,
                    "target_content_digest": runtime.digest,
                    "approved_baseline_sha256": None,
                }
            ],
        }
    )
    manifest = NativeLocationFilesV1.model_validate(
        {
            "version": 1,
            "required_files": [
                {"path": name, "sha256": hashlib.sha256((target / name).read_bytes()).hexdigest()}
                for name in sorted(required_native_files([before_run]))
            ],
            "unavailable_files": [{"path": "old/unavailable.json", "sha256": None}],
        }
    )
    service = NativeLocationMaintenance(c.store)
    prepared = await service.prepare(
        plan,
        manifest,
        Path(os.environ["FORGE_TEST_POSTGRES_DATA"]),
        tmp_path / "location-scratch",
        SourceCatalogBudget(min_free_bytes=0),
    )
    receipt = await service.commit(
        prepared,
        NativeLocationAcknowledgmentV1(
            version=1,
            request_id=uuid4().hex,
            candidate_digest=prepared.candidate.digest,
            acknowledge_stopped_runtime=True,
            acknowledge_history_preserved=True,
            acknowledge_no_execution_grant=True,
        ),
    )
    await c.requests.runtime.start()
    await c.store.acquire_ownership()
    assert c.store.native_location_state.receipt_digest == receipt["digest"]
    assert await c.journal.entries(c.run_id) == before_entries
    assert await c.journal.sandbox_executions(c.run_id) == before_commands
    assert await c.store.get_run(c.run_id) == before_run
    candidate = await c.service.final_outcomes.candidate(c.run_id, failed["id"])
    assert candidate["admission"] == "READY", candidate["holds"]
    await c.service.final_outcomes.request(c.run_id, failed["id"], uuid4().hex, final_input(candidate))

    async def forbidden(*args, **kwargs):
        raise AssertionError("Relocation cannot replay an already-consumed model response")

    monkeypatch.setattr(c.engine.provider_factory("bridge"), "execute", forbidden)
    monkeypatch.setattr(AgentCheckpoints, "_consume", forbidden)
    await c.service.execute(failed["id"])
    accepted = await c.store.get_run(c.run_id)
    assert accepted["status"] == "paused", accepted.get("error")
    assert accepted["usage"] == before_run["usage"]
    from types import SimpleNamespace

    from firefly_forge.process import execute

    old_tool.rename(tmp_path / "quarantined-old-runtime")

    class PhysicalBackend:
        async def run(self, config, argv, cwd, timeout, output_limit):
            assert argv == [str(new_tool / "bin" / "approved-check")]
            c.command_calls.append(argv)
            return await execute(argv, cwd, timeout)

    c.engine.sandbox.backends = SimpleNamespace(select=lambda _: PhysicalBackend())
    await VerificationHandoffs(c.store, c.journal, c.engine).execute(c.run_id)
    checked = await c.store.get_run(c.run_id)
    assert checked["status"] == "queued" and checked["verification_handoff"]["status"] == "completed"
    assert len(c.posts) == 4 and c.command_calls[-1] == [str(new_tool / "bin" / "approved-check")]
    assert checked["usage"] == before_run["usage"]
    assert len(await c.journal.sandbox_executions(c.run_id)) == len(before_commands) + 1
    assert [entry for entry in await c.journal.entries(c.run_id) if entry["kind"] == "agent_consume"] == [
        entry for entry in before_entries if entry["kind"] == "agent_consume"
    ]
