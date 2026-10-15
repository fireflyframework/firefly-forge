# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native Factory WAL preserves unknown submissions and publishes only a retained result."""

import hashlib
import importlib
from uuid import uuid4

import pytest
from test_runner_factory_authority import seed

from firefly_forge.runner_factory_models import FactoryPoolSelection, capture_factory_pool
from firefly_forge.runner_pool_models import RunnerPoolProfile
from firefly_forge.sandbox import pack_workspace


class ObservedController:
    def __init__(self, profile, returned):
        self.profile_value, self.returned = profile, returned
        self.output = b"completed\n"
        self.submissions = 0
        self.lost_response = False
        self.before_submit = None
        self.request = None

    async def profile(self):
        return {
            "profile": self.profile_value.model_dump(mode="json"),
            "revision": self.profile_value.revision,
            "status": "ready",
            "workspace_protocols": ["forge-project-bundle-v1"],
        }

    async def submit(self, invocation, fence, archive, stdin=b""):
        self.submissions += 1
        if self.before_submit:
            await self.before_submit()
        self.request = invocation.model_dump(mode="json")
        if self.lost_response:
            raise TimeoutError("fixture response lost after dispatch")
        return await self.observe(invocation)

    async def observe(self, invocation):
        return {
            "status": "finished",
            "request": self.request,
            "receipt": {
                "drained": True,
                "exit_code": 0,
                "outcome": "completed",
                **{
                    kind: {"kind": kind, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    for kind, data in {"result": self.returned, "output": self.output}.items()
                },
            },
        }

    async def artifact(self, invocation, kind, receipt):
        return self.returned if kind == "result" else self.output


async def fixture(store, tmp_path):
    from pathlib import Path

    from firefly_forge.runner_factory_authority import RunnerFactoryAuthority
    from firefly_forge.runner_git_snapshot import RunnerGitSnapshot

    module = importlib.import_module("firefly_forge.runner_pool_worker")
    run, journal = await seed(store)
    image = "example/worker@sha256:" + "a" * 64
    profile = RunnerPoolProfile(
        profile_id="owned", daemon_id="actual-owned-engine", approved_images=(image,), transfer_image=image
    )
    selection = FactoryPoolSelection(profile=profile, image=image)
    await store.update(run["id"], sandbox_snapshot=capture_factory_pool(selection))
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir(mode=0o700)
    workspace = workspaces / run["id"]
    workspace.mkdir()
    (workspace / "main.txt").write_text("original\n")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "retained").write_text("original host metadata")
    result = tmp_path / "result"
    result.mkdir()
    (result / "main.txt").write_text("runner result\n")
    returned, _ = pack_workspace(result, selection.workspace_max_bytes)
    controller = ObservedController(profile, returned)
    from firefly_forge.runner_controller_config import RunnerWorkerTransport

    controller.transport = RunnerWorkerTransport(
        active=True,
        endpoint="https://127.0.0.1:9917",
        certificate=tmp_path / "client.pem",
        private_key=tmp_path / "client.key",
        ca=tmp_path / "ca.pem",
        token="private-fixture-worker-token-32-characters",
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
    )
    state = tmp_path / "pool-artifacts"
    state.mkdir(mode=0o700)
    worker = module.RunnerPoolWorker(
        store,
        journal,
        RunnerFactoryAuthority(store, journal),
        controller,
        RunnerGitSnapshot(Path("/usr/bin/git")),
        workspaces,
        state,
    )
    return worker, controller, run, journal, workspace


async def test_start_wal_precedes_single_submit_and_original_git_survives_publication(store, tmp_path):
    worker, controller, run, journal, workspace = await fixture(store, tmp_path)

    async def before_submit():
        records = await journal.sandbox_executions(run["id"])
        assert records[0]["effects"]["start"]["status"] == "intent"
        assert records[0]["effects"]["create"]["evidence"]["scope"] == "factory_request_prepared"

    controller.before_submit = before_submit
    result = await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert result.exit_code == 0 and result.output == "completed\n"
    assert (workspace / "main.txt").read_text() == "runner result\n"
    assert (workspace / ".git" / "retained").read_text() == "original host metadata"
    again = await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert again == result and controller.submissions == 1
    assert (await journal.sandbox_executions(run["id"]))[0]["status"] == "removed"


@pytest.mark.parametrize("escaped", [False, True])
async def test_cached_output_rechecks_new_credentials_without_replay_or_receipt_changes(store, tmp_path, escaped):
    import json

    credentials = {}
    store.credential_registry = lambda: credentials
    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    canary = 'newly-registered-factory-canary-"quoted"'
    payload = (json.dumps(canary)[1:-1] if escaped else canary).encode()
    controller.output = payload
    first = await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert first.output == payload.decode() and controller.submissions == 1
    credentials["FORGE_UNRELATED_API_TOKEN"] = "unrelated-registered-canary"
    assert await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1) == first
    before = await journal.sandbox_executions(run["id"])
    output = worker.artifacts.directory / before[0]["execution_id"] / "output.bin"
    original = output.stat()

    credentials["FORGE_TEST_API_TOKEN"] = canary
    for _ in range(2):
        with pytest.raises(RuntimeError, match="credential.*reconciliation") as failure:
            await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
        assert canary not in str(failure.value)

    assert controller.submissions == 1
    assert await journal.sandbox_executions(run["id"]) == before
    assert output.read_bytes() == payload
    assert (output.stat().st_ino, output.stat().st_mtime_ns) == (original.st_ino, original.st_mtime_ns)


@pytest.mark.parametrize("escaped", [False, True])
async def test_first_output_rechecks_credentials_after_final_receipt_without_replaying(
    store, tmp_path, monkeypatch, escaped
):
    import json

    credentials = {}
    store.credential_registry = lambda: credentials
    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    canary = 'registered-at-receipt-canary-"quoted"'
    payload = (json.dumps(canary)[1:-1] if escaped else canary).encode()
    controller.output = payload
    original = journal.sandbox_effect_receipt

    async def register_after_receipt(*args, **kwargs):
        result = await original(*args, **kwargs)
        if kwargs.get("effect") == "cleanup":
            credentials["FORGE_TEST_API_TOKEN"] = canary
        return result

    monkeypatch.setattr(journal, "sandbox_effect_receipt", register_after_receipt)
    with pytest.raises(RuntimeError, match="credential.*reconciliation") as failure:
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert canary not in str(failure.value)
    before = await journal.sandbox_executions(run["id"])
    assert before[0]["status"] == "removed"
    assert before[0]["effects"]["start"]["evidence"]["workspace_import"] == "complete"
    with pytest.raises(RuntimeError, match="credential.*reconciliation"):
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert controller.submissions == 1
    assert await journal.sandbox_executions(run["id"]) == before


async def test_unknown_submit_is_never_replayed_and_keeps_durable_start_intent(store, tmp_path):
    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    controller.lost_response = True
    for _ in range(2):
        with pytest.raises(RuntimeError, match="reconciliation"):
            await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert controller.submissions == 1 and (workspace / "main.txt").read_text() == "original\n"
    assert (await journal.sandbox_executions(run["id"]))[0]["effects"]["start"]["status"] == "intent"


async def test_stale_host_source_retains_returned_archive_without_partial_publication_or_resubmit(store, tmp_path):
    worker, controller, run, journal, workspace = await fixture(store, tmp_path)

    async def change_host():
        (workspace / "main.txt").write_text("concurrent host change\n")

    controller.before_submit = change_host
    with pytest.raises(RuntimeError, match="publication.*reconciliation"):
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert (workspace / "main.txt").read_text() == "concurrent host change\n"
    records = await journal.sandbox_executions(run["id"])
    evidence = records[0]["effects"]["start"]["evidence"]
    assert evidence["workspace_import"] == "requires_reconciliation" and evidence["result"]["bytes"] > 0
    with pytest.raises(RuntimeError, match="reconciliation"):
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert controller.submissions == 1


async def test_foreign_workspace_and_owner_loss_cannot_submit(store, tmp_path):
    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    other = workspace.parent / uuid4().hex
    other.mkdir()
    with pytest.raises(ValueError, match="retained.*workspace"):
        await worker.execute(run["id"], other, other, ["git", "diff"], 5, "verify", ordinal=1)
    await store.release_ownership()
    with pytest.raises(RuntimeError, match="owner"):
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert controller.submissions == 0


async def test_sandbox_pool_facade_fails_closed_without_worker_and_dispatches_explicit_adapter(store, tmp_path):
    from firefly_forge.sandbox import SandboxExecutor

    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    sandbox = SandboxExecutor(store, journal)
    with pytest.raises(RuntimeError, match="pool.*configured"):
        await sandbox.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    sandbox.pool_worker = worker
    result = await sandbox.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert result.exit_code == 0 and controller.submissions == 1


async def test_owner_loss_during_bundle_preparation_does_not_write_start_or_submit(store, tmp_path):
    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    original = worker.git_snapshot.build

    async def lose_owner(*args):
        metadata = await original(*args)
        await store.release_ownership()
        return metadata

    worker.git_snapshot.build = lose_owner
    with pytest.raises(RuntimeError, match="ownership|owner"):
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert controller.submissions == 0 and await journal.sandbox_executions(run["id"]) == []


async def test_old_controller_without_framed_capability_cannot_receive_project_bytes(store, tmp_path):
    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    original = controller.profile

    async def old_profile():
        value = await original()
        value.pop("workspace_protocols")
        return value

    controller.profile = old_profile
    with pytest.raises(RuntimeError, match="unsupported"):
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert controller.submissions == 0 and await journal.sandbox_executions(run["id"]) == []


async def test_recovery_after_factory_restart_observes_and_closes_unknown_without_import_or_replay(store, tmp_path):
    from firefly_forge.sandbox import SandboxExecutor

    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    controller.lost_response = True
    with pytest.raises(RuntimeError, match="reconciliation"):
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    await journal.recover()
    assert (await store.get_run(run["id"]))["status"] == "needs_reconciliation"
    sandbox = SandboxExecutor(store, journal)
    sandbox.pool_worker = worker
    counts = await sandbox.recover()
    assert counts["interrupted"] == 1 and counts["removed"] == 1
    row = (await journal.sandbox_executions(run["id"]))[0]
    assert row["effects"]["start"]["evidence"]["outcome"] == "interrupted"
    assert row["effects"]["start"]["evidence"]["workspace_import"] == "not_attempted"
    assert controller.submissions == 1 and (workspace / "main.txt").read_text() == "original\n"


async def test_private_controller_token_is_excluded_before_source_or_command_transfer(store, tmp_path):
    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    token = controller.transport.token.get_secret_value()
    with pytest.raises(ValueError, match="credential"):
        await worker.execute(run["id"], workspace, workspace, ["echo", token], 5, "verify", ordinal=1)
    (workspace / "leaked-token.txt").write_text(token)
    with pytest.raises(ValueError, match="credential"):
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert controller.submissions == 0 and await journal.sandbox_executions(run["id"]) == []


async def test_cancellation_requests_only_drain_of_original_identity_and_preserves_start_intent(store, tmp_path):
    import asyncio

    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    submitted = asyncio.Event()
    cancelled = []

    async def waiting_submit(invocation, fence, archive, stdin=b""):
        controller.submissions += 1
        controller.request = invocation.model_dump(mode="json")
        submitted.set()
        await asyncio.Event().wait()

    async def cancel(invocation, fence):
        cancelled.append(invocation.execution_id)
        return await controller.observe(invocation)

    controller.submit = waiting_submit
    controller.cancel = cancel
    running = asyncio.create_task(
        worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    )
    await asyncio.wait_for(submitted.wait(), 10)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert len(cancelled) == 1 and controller.submissions == 1
    assert (await journal.sandbox_executions(run["id"]))[0]["effects"]["start"]["status"] == "intent"
    assert (workspace / "main.txt").read_text() == "original\n"


@pytest.mark.parametrize("loss", ["owner", "cancellation", "actor"])
async def test_cached_completion_rechecks_current_authority_after_journal_read(store, tmp_path, loss):
    from firefly_forge.identity import Principal
    from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    actor = Principal(uuid4(), "local:bootstrap", "fixture-operator", authentication_method="local_bootstrap")
    if loss == "actor":
        async with store._unit():
            await ForgeIdentityUserRepository().save(
                ForgeIdentityUserEntity(id=actor.id, issuer=actor.issuer, subject=actor.subject)
            )
        await store.update(run["id"], execution_actor=ProjectAccess.actor_snapshot(actor))
    result = await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert result.exit_code == 0 and controller.submissions == 1
    original_read = journal.sandbox_executions
    retained = await original_read(run["id"])
    entries = await journal.entries(run["id"])
    artifacts = {path.name: path.read_bytes() for path in worker.artifacts.directory.iterdir() if path.is_file()}
    changed = False

    async def read_then_lose_authority(run_id):
        nonlocal changed
        rows = await original_read(run_id)
        if not changed:
            if loss == "owner":
                await store.release_ownership()
            elif loss == "cancellation":
                await store.update(run_id, cancel_request={"status": "requested"})
            else:
                async with store._unit():
                    users = ForgeIdentityUserRepository()
                    user = await users.find_by_id(actor.id)
                    user.enabled = False
                    await users.save(user)
            changed = True
        return rows

    journal.sandbox_executions = read_then_lose_authority
    with pytest.raises((RuntimeError, AccessDenied), match="owner|cancel|suspended"):
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert changed and controller.submissions == 1
    assert await original_read(run["id"]) == retained
    assert await journal.entries(run["id"]) == entries
    assert (workspace / "main.txt").read_text() == "runner result\n"
    assert (workspace / ".git" / "retained").read_text() == "original host metadata"
    assert {
        path.name: path.read_bytes() for path in worker.artifacts.directory.iterdir() if path.is_file()
    } == artifacts
