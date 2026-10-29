# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Worker/WAL coordination renews only its exact current run without replay."""

import asyncio
import hashlib
from collections import defaultdict
from types import SimpleNamespace

import pytest
from test_run_owned_authority_host import case

from firefly_forge import runner_pool_worker as module
from firefly_forge.runner_bundle_models import ProjectBundleIdentity
from firefly_forge.runner_run_liveness import RunLease, RunRenewalV1, run_digest


@pytest.mark.parametrize("failure", [None, "revoked", "foreign_ack", "lost_renew"])
async def test_worker_seals_run_binding_and_renews_once_without_reset_or_resubmission(tmp_path, monkeypatch, failure):
    _, fence, run, *_ = case()
    workspace = tmp_path / run["id"]
    workspace.mkdir()
    scope = {"run": run, "attempt": 2, "fence": fence}
    events, ticks, state = [], {"now": 0.0}, {}
    source, archive = b"source-fixture", b"framed-fixture"
    bundle = ProjectBundleIdentity(
        archive_digest=run_digest_bytes(archive),
        archive_bytes=len(archive),
        source_digest="d" * 64,
        source_archive_digest=run_digest_bytes(source),
        source_archive_bytes=len(source),
        git_archive_bytes=11,
    )

    async def authorize(*args):
        if state.get("submitted") and failure == "revoked":
            raise ValueError("Controlled specific run revoked")
        return scope

    async def exclusive():
        return fence

    async def submit(invocation, selected, data):
        assert invocation.run_liveness is not None
        assert invocation.run_liveness.task_id == "verify" and invocation.run_liveness.attempt == 2
        assert invocation.timeout == 864000
        events.append(("submit", invocation.execution_id))
        state["submitted"] = invocation
        state["lease"] = RunLease(
            run_digest(invocation.model_dump(mode="json")),
            invocation.run_liveness,
            "e" * 32,
            30,
            clock=lambda: ticks["now"],
        )
        ack = state["lease"].ack()
        if failure == "foreign_ack":
            ack["worker_epoch"] = "f" * 32
        return {"run_lease": ack}

    async def renew(invocation, selected, ack):
        events.append(("renew", invocation.execution_id))
        reply = state["lease"].renew(
            RunRenewalV1.model_validate({**ack, "protocol": "forge-run-renewal-v1", "sequence": ack["sequence"] + 1})
        )
        if failure == "lost_renew":
            raise OSError("Controlled unknown renewal acknowledgement")
        return reply

    async def observe(invocation):
        done = any(kind == "renew" for kind, _ in events)
        return {
            "request": invocation.model_dump(mode="json"),
            "status": "finished" if done else "running",
            "receipt": {"outcome": "completed", "drained": False},
        }

    async def cancel(invocation, selected):
        events.append(("cancel", invocation.execution_id))

    async def intent(run_id, task, attempt, snapshot, prepare, **kwargs):
        payload = prepare(1, "b" * 32, None)
        state["wal"] = payload
        return {"fresh": True, "execution": {"execution_id": "b" * 32, "request": payload}}

    async def effect(*args, **kwargs):
        return {"disposition": "execute"}

    async def metadata(*args):
        return b"git-fixture"

    async def probe(*args):
        return {"available": True}

    def clock():
        before = ticks["now"]
        ticks["now"] += 5
        return before

    monkeypatch.setattr(module, "pack_workspace", lambda *args, **kwargs: (source, "fixture"))
    monkeypatch.setattr(module, "frame_project_archives", lambda *args: (archive, bundle))
    worker = object.__new__(module.RunnerPoolWorker)
    worker.workspaces, worker.locks, worker.waiting, worker.run_clock = tmp_path, defaultdict(asyncio.Lock), {}, clock
    worker.authority = SimpleNamespace(authorize=authorize, exclusive_fence=exclusive)
    worker.client = SimpleNamespace(submit=submit, observe=observe, cancel=cancel, renew_run=renew)
    worker.journal = SimpleNamespace(
        sandbox_command_intent=intent, sandbox_effect_receipt=effect, sandbox_effect_intent=effect
    )
    worker.artifacts = SimpleNamespace(directory=tmp_path, credential_registry=lambda: {}, retain=lambda *args: None)
    worker.git_snapshot, worker.probe = SimpleNamespace(build=metadata), probe
    with pytest.raises(RuntimeError, match="reconciliation"):
        await worker.execute(run["id"], workspace, workspace, ["fixture"], 864000, "verify")
    assert events.count(("submit", "b" * 32)) == 1
    assert events.count(("renew", "b" * 32)) == (0 if failure in {"revoked", "foreign_ack"} else 1)
    assert events.count(("cancel", "b" * 32)) == 1
    assert state["wal"]["request"]["run_liveness"]["worker_epoch"]
    assert worker.waiting == {}


def run_digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


async def test_probe_requires_exact_controller_renewal_capability_without_enabling_uncapped_pool():
    _, _, run, *_ = case()
    snapshot = run["sandbox_snapshot"]
    profile = snapshot["configuration"]["profile"]
    response = {
        "profile": profile,
        "revision": run_digest(profile),
        "status": "ready",
        "workspace_protocols": ["forge-project-bundle-v1"],
    }
    calls = []

    async def read():
        calls.append(1)
        return response

    worker = object.__new__(module.RunnerPoolWorker)
    worker.client = SimpleNamespace(
        transport=SimpleNamespace(
            require_active=lambda: None, profile_id=profile["profile_id"], profile_revision=run_digest(profile)
        ),
        profile=read,
    )
    with pytest.raises(RuntimeError, match="ownership|liveness|unsupported"):
        await worker.probe(snapshot)
    response["run_liveness_protocols"] = ["forge-run-owned-liveness-v1"]
    assert (await worker.probe(snapshot))["available"] is True
