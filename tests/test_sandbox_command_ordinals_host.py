# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real executor/Journal with transactional memory rows, not a PostgreSQL lock proof."""

import asyncio
import copy
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_forge.journal import Journal
from firefly_forge.process import ProcessResult
from firefly_forge.sandbox import SandboxExecutor, SandboxReconciliationRequired
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox


class Rows:
    def __init__(self):
        self.rows = []

    async def find_by_id(self, key, **kwargs):
        return next((row for row in self.rows if row.id == key), None)

    async def save(self, row):
        if row.id is None:
            row.id = uuid4()
        self.rows = [item for item in self.rows if item.id != row.id] + [row]
        return row

    async def find_for_run(self, key):
        return [row for row in self.rows if row.run_id == key]

    for_run = find_for_run

    async def slot(self, run_id, task, attempt, ordinal):
        return next(
            (
                row
                for row in self.rows
                if (row.run_id, row.task_id, row.parent_attempt, row.ordinal) == (run_id, task, attempt, ordinal)
            ),
            None,
        )


@pytest.fixture
def command_case(tmp_path):
    class Store:
        path = tmp_path / "state"
        schema = "controlled_command_test"

        def __init__(self):
            self.run_records, self.sandboxes, self.records, self.tasks = [Rows() for _ in range(4)]
            self.lock = asyncio.Lock()
            self.depth = ContextVar("command_fixture_unit", default=False)
            self.owned = True
            self.run = SimpleNamespace(id=uuid4(), data={"sandbox_snapshot": capture_sandbox(SandboxInput())})
            self.run_records.rows = [self.run]
            self.tasks.rows = [
                SimpleNamespace(id=uuid4(), run_id=self.run.id, task_id="verify", attempt=1, status="running", data={})
            ]

        @asynccontextmanager
        async def _unit(self):
            if self.depth.get():
                yield None
                return
            async with self.lock:
                token = self.depth.set(True)
                before = copy.deepcopy((self.sandboxes.rows, self.records.rows))
                try:
                    yield None
                except BaseException:
                    self.sandboxes.rows, self.records.rows = before
                    raise
                finally:
                    self.depth.reset(token)

        async def assert_ownership(self):
            if not self.owned:
                raise RuntimeError("Controlled ownership lost")

        async def get_run(self, run_id):
            assert run_id == self.run.id.hex
            return {"id": run_id, **copy.deepcopy(self.run.data)}

    store = Store()
    journal = Journal(store, tasks=store.tasks, records=store.records, sandboxes=store.sandboxes)

    async def plan(run_id):
        return {"tasks": [{"id": row.task_id, "attempt": row.attempt} for row in store.tasks.rows]}

    journal.plan = plan
    calls = []

    class Backend:
        async def run(self, config, argv, cwd, timeout, output_limit):
            assert not store.depth.get(), "Process crossed an uncommitted command allocation"
            calls.append(argv)
            return ProcessResult(argv, 0, str(len(calls)))

    backend = Backend()
    backends = SimpleNamespace(select=lambda config: backend)

    def fresh():
        return SandboxExecutor(store, journal, backends)

    return SimpleNamespace(
        store=store,
        journal=journal,
        fresh=fresh,
        calls=calls,
        workspace=tmp_path,
        run_id=store.run.id.hex,
        backend=backend,
    )


async def command(case, argv=None, *, ordinal=None):
    return await case.fresh().execute(
        case.run_id, case.workspace, case.workspace, argv or ["fixture"], 2, "verify", ordinal=ordinal
    )


async def test_new_service_same_argv_is_new_not_a_cached_result(command_case):
    case = command_case
    first = await command(case)
    second = await command(case)
    assert [first.output, second.output] == ["1", "2"]
    assert [r["ordinal"] for r in await case.journal.sandbox_executions(case.run_id)] == [1, 2]
    assert Journal.verify_chain([row.data for row in case.store.records.rows])


async def test_new_service_different_argv_uses_next_durable_slot(command_case):
    case = command_case
    await command(case, ["one"])
    await command(case, ["two"])
    assert case.calls == [["one"], ["two"]]


async def test_explicit_ordinal_preserves_exact_replay_and_refuses_changed_command(command_case):
    case = command_case
    first = await command(case)
    assert await command(case, ordinal=1) == first
    with pytest.raises((ValueError, SandboxReconciliationRequired), match="differs|changed"):
        await command(case, ["other"], ordinal=1)
    assert len(case.calls) == 1


async def test_fresh_service_does_not_reset_global_budget(command_case):
    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(SandboxInput(command_limit=1))
    await command(case)
    with pytest.raises(ValueError, match="budget"):
        await command(case)
    assert len(case.calls) == 1


async def test_unknown_prior_effect_blocks_a_different_new_slot(command_case):
    case = command_case
    await command(case)
    row = case.store.sandboxes.rows[0]
    row.data["effects"]["start"] = {"status": "intent"}
    row.status = "running"
    with pytest.raises(SandboxReconciliationRequired, match="reconcil"):
        await command(case, ["other"], ordinal=2)
    assert len(case.calls) == 1


async def test_concurrent_services_cannot_dispatch_past_unfinished_command(command_case):
    case = command_case
    reached, release = asyncio.Event(), asyncio.Event()
    original = case.backend.run

    async def waiting(*args):
        reached.set()
        await release.wait()
        return await original(*args)

    case.backend.run = waiting
    first = asyncio.create_task(command(case))
    await asyncio.wait_for(reached.wait(), 2)
    try:
        with pytest.raises(SandboxReconciliationRequired, match="reconcil"):
            await command(case)
    finally:
        release.set()
        await first
    assert len(case.calls) == 1


async def test_missing_effect_projection_cannot_reopen_an_already_executed_explicit_slot(command_case):
    case = command_case
    await command(case)
    case.store.sandboxes.rows[0].data["effects"].pop("start")
    case.store.sandboxes.rows[0].data["effects"].pop("cleanup")
    with pytest.raises(SandboxReconciliationRequired, match="reconcil"):
        await command(case, ordinal=1)
    assert len(case.calls) == 1


@pytest.mark.parametrize("field", ["ordinal", "parent_attempt", "execution_id", "run_id"])
async def test_json_overlays_never_change_the_canonical_cursor(command_case, field):
    case = command_case
    await command(case)
    case.store.sandboxes.rows[0].data[field] = 0
    with pytest.raises(SandboxReconciliationRequired, match="reconcil"):
        await command(case)
    assert len(case.calls) == 1


@pytest.mark.parametrize("field", ["ordinal", "parent_attempt", "container_name", "snapshot_digest"])
async def test_request_identity_must_equal_scalar_columns_and_native_intent(command_case, field):
    case = command_case
    await command(case)
    case.store.sandboxes.rows[0].data["request"][field] = "different"
    with pytest.raises(SandboxReconciliationRequired, match="reconcil"):
        await command(case)
    assert len(case.calls) == 1


async def test_wal_failure_rolls_back_slot_and_create_intent_together(command_case, monkeypatch):
    case = command_case
    original = case.store.records.save

    async def fail(row):
        await original(row)
        raise ValueError("Controlled WAL failure")

    monkeypatch.setattr(case.store.records, "save", fail)
    with pytest.raises(ValueError, match="WAL failure"):
        await command(case)
    assert case.store.sandboxes.rows == [] and case.store.records.rows == [] and case.calls == []
    monkeypatch.setattr(case.store.records, "save", original)
    await command(case)
    assert case.store.sandboxes.rows[0].ordinal == 1


@pytest.mark.parametrize("change", ["attempt", "snapshot", "ownership", "cancel"])
async def test_queued_allocation_rechecks_current_scope_before_intent(command_case, change):
    case = command_case
    entered = asyncio.Event()
    original = case.journal.sandbox_command_intent

    async def queued(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    release = asyncio.Event()
    case.journal.sandbox_command_intent = queued
    pending = asyncio.create_task(command(case))
    await asyncio.wait_for(entered.wait(), 2)
    if change == "attempt":
        case.store.tasks.rows[0].attempt = 2
    elif change == "snapshot":
        case.store.run.data["sandbox_snapshot"] = capture_sandbox(SandboxInput(command_limit=1))
    elif change == "ownership":
        case.store.owned = False
    else:
        case.store.run.data["cancel_request"] = {"status": "requested"}
    release.set()
    with pytest.raises((ValueError, RuntimeError, asyncio.CancelledError)):
        await pending
    assert not case.store.sandboxes.rows and not case.calls


async def test_cancelled_process_leaves_wal_and_never_allows_automatic_replacement(command_case):
    case = command_case

    async def cancel(*args):
        raise asyncio.CancelledError()

    case.backend.run = cancel
    with pytest.raises(asyncio.CancelledError):
        await command(case)
    rows = await case.journal.sandbox_executions(case.run_id)
    assert rows[0]["effects"]["start"]["status"] == "intent"
    assert rows[0]["effects"]["cleanup"]["status"] == "done"
    with pytest.raises(SandboxReconciliationRequired, match="reconcil"):
        await command(case, ordinal=2)
    assert len(case.store.sandboxes.rows) == 1


async def test_completed_prior_attempts_keep_the_global_budget(command_case):
    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(SandboxInput(command_limit=2))
    await command(case)
    case.store.tasks.rows[0].attempt = 2
    await command(case)
    case.store.tasks.rows[0].attempt = 3
    with pytest.raises(ValueError, match="total command budget"):
        await command(case)
    assert len(case.calls) == 2
    assert [row.ordinal for row in case.store.sandboxes.rows] == [1, 1]


async def test_reconciled_known_interruption_requires_native_task_retry_before_fresh_dispatch(command_case):
    from uuid import NAMESPACE_URL, uuid5

    case = command_case
    original = case.backend.run

    async def interrupted(*args):
        raise asyncio.CancelledError()

    case.backend.run = interrupted
    with pytest.raises(asyncio.CancelledError):
        await command(case)
    execution = case.store.sandboxes.rows[0].id.hex
    await case.journal.sandbox_effect_reconcile(
        case.run_id, "verify", execution, effect="start", evidence={"observed_state": "absent"}, outcome="interrupted"
    )
    task = case.store.tasks.rows[0]
    key = uuid5(NAMESPACE_URL, f"forge:{case.run_id}:verify:1").hex
    task.data["idempotency_key"] = key
    task.status = "needs_reconciliation"
    task.attempt = 2
    task.status = "running"
    with pytest.raises(SandboxReconciliationRequired, match="reconcil"):
        await command(case)
    task.attempt, task.status = 1, "needs_reconciliation"
    await case.journal.reconcile(case.run_id, key, evidence={"observed_command_drain": execution}, outcome="retry")
    task.status = "running"
    case.backend.run = original
    await command(case)
    assert len(case.calls) == 1
    assert [row.parent_attempt for row in case.store.sandboxes.rows] == [1, 2]


async def test_checkpoint_runtime_contract_changes_when_executor_semantics_change(monkeypatch):
    from pathlib import Path

    from firefly_forge.agentic_checkpoint_invocation import runtime_contract

    before = runtime_contract()
    original = Path.read_bytes

    def changed(path):
        value = original(path)
        return value + b"\n# changed executor contract\n" if path.name == "sandbox_command_slots.py" else value

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert runtime_contract() != before


@pytest.fixture
def pool_command_case(command_case, tmp_path):
    from datetime import UTC, datetime
    from pathlib import Path

    from test_runner_pool_worker import ObservedController

    from firefly_forge.runner_controller_config import RunnerWorkerTransport
    from firefly_forge.runner_factory_models import FactoryPoolSelection, capture_factory_pool
    from firefly_forge.runner_git_snapshot import RunnerGitSnapshot
    from firefly_forge.runner_pool_models import FactoryFence, RunnerPoolProfile
    from firefly_forge.runner_pool_worker import RunnerPoolWorker
    from firefly_forge.sandbox import pack_workspace

    case = command_case
    image = "fixture/worker@sha256:" + "a" * 64
    profile = RunnerPoolProfile(
        profile_id="owned", daemon_id="controlled", approved_images=(image,), transfer_image=image
    )
    policy = FactoryPoolSelection(profile=profile, image=image)
    case.store.run.data.update(repository_id=uuid4().hex, sandbox_snapshot=capture_factory_pool(policy))
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    workspace = workspaces / case.run_id
    workspace.mkdir()
    (workspace / "main.txt").write_text("original\n")
    result = tmp_path / "returned"
    result.mkdir()
    (result / "main.txt").write_text("controlled result\n")
    returned, _ = pack_workspace(result, policy.workspace_max_bytes)
    controller = ObservedController(profile, returned)
    controller.transport = RunnerWorkerTransport(
        active=True,
        endpoint="https://127.0.0.1:9917",
        certificate=tmp_path / "unused-cert.pem",
        private_key=tmp_path / "unused-key.pem",
        ca=tmp_path / "unused-ca.pem",
        token="controlled-private-test-token-32-characters",
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
    )
    fence = FactoryFence(factory_schema=case.store.schema, pid=1, backend_started_at=datetime.now(UTC))

    class Authority:
        async def authorize(self, run_id, task_id):
            return {
                "run": await case.store.get_run(run_id),
                "attempt": case.store.tasks.rows[0].attempt,
                "fence": fence,
            }

    case.store.credential_registry = dict
    state = tmp_path / "pool-artifacts"
    state.mkdir(mode=0o700)

    def fresh():
        executor = case.fresh()
        executor.pool_worker = RunnerPoolWorker(
            case.store,
            case.journal,
            Authority(),
            controller,
            RunnerGitSnapshot(Path("/usr/bin/git")),
            workspaces,
            state,
        )
        return executor

    return SimpleNamespace(case=case, controller=controller, workspace=workspace, fresh=fresh)


@pytest.mark.parametrize("second", [["fixture"], ["different"]])
async def test_pool_fresh_service_uses_new_slot_without_controller_resubmission(pool_command_case, second):
    pool = pool_command_case
    for argv in (["fixture"], second):
        result = await pool.fresh().execute(pool.case.run_id, pool.workspace, pool.workspace, argv, 2, "verify")
        assert result.exit_code == 0
    assert pool.controller.submissions == 2
    assert sorted(row.ordinal for row in pool.case.store.sandboxes.rows) == [1, 2]


async def test_pool_explicit_replay_never_repackages_or_calls_the_controller(pool_command_case):
    pool = pool_command_case
    executor = pool.fresh()
    result = await executor.execute(pool.case.run_id, pool.workspace, pool.workspace, ["fixture"], 2, "verify")
    replay = pool.fresh()

    async def forbidden(*args):
        raise AssertionError("Explicit known-result replay reached a controller or source preparation")

    replay.pool_worker.probe = forbidden
    replay.pool_worker.git_snapshot.build = forbidden
    assert (
        await replay.execute(pool.case.run_id, pool.workspace, pool.workspace, ["fixture"], 2, "verify", ordinal=1)
        == result
    )
    assert pool.controller.submissions == 1


async def test_pool_unknown_submission_blocks_a_new_automatic_identity(pool_command_case):
    pool = pool_command_case
    pool.controller.lost_response = True
    with pytest.raises(RuntimeError, match="reconcil"):
        await pool.fresh().execute(pool.case.run_id, pool.workspace, pool.workspace, ["fixture"], 2, "verify")
    with pytest.raises(SandboxReconciliationRequired, match="reconcil"):
        await pool.fresh().execute(pool.case.run_id, pool.workspace, pool.workspace, ["different"], 2, "verify")
    assert pool.controller.submissions == 1 and len(pool.case.store.sandboxes.rows) == 1


async def test_missing_scalar_projection_cannot_reuse_a_slot_retained_in_wal(command_case):
    case = command_case
    await command(case)
    case.store.sandboxes.rows.clear()
    with pytest.raises(SandboxReconciliationRequired, match="reconcil"):
        await command(case)
    assert len(case.calls) == 1


async def test_allocator_refuses_ambient_native_transaction_before_any_intent(command_case):
    from pyfly.data.transaction import TransactionTemplate
    from test_agentic_checkpoint_storage_host import MemoryManager

    case = command_case
    case.journal.plans = Rows()
    manager = MemoryManager(case.store, case.journal)
    async with TransactionTemplate(manager).transaction():
        with pytest.raises(ValueError, match="transaction"):
            await command(case)
    assert case.store.sandboxes.rows == [] and case.calls == []


@pytest.fixture
def docker_command_case(command_case):
    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(
        SandboxInput(mode="docker", image="fixture/image@sha256:" + "a" * 64, context="controlled-only")
    )
    objects, dispatches = {}, []

    def fresh():
        executor = case.fresh()

        async def docker(config, argv, *args, **kwargs):
            assert not case.store.depth.get()
            if argv[0] == "create":
                name = argv[argv.index("--name") + 1]
                objects[name] = "created"
                return ProcessResult(argv, 0, name)
            if argv[0] == "start":
                name = argv[-1]
                objects[name] = "exited"
                dispatches.append(name)
                return ProcessResult(argv, 0, str(len(dispatches)))
            assert argv[:4] == ["container", "rm", "--force", "--volumes"]
            objects.pop(argv[-1], None)
            return ProcessResult(argv, 0, "")

        async def inspect(config, name, labels):
            return {"Id": name, "State": {"Status": objects[name], "ExitCode": 0}} if name in objects else None

        executor._docker, executor._inspect = docker, inspect
        return executor

    return SimpleNamespace(case=case, fresh=fresh, dispatches=dispatches)


async def test_docker_fresh_service_uses_durable_new_identity_and_explicit_replay(docker_command_case):
    docker = docker_command_case
    case = docker.case
    first = await docker.fresh().execute(case.run_id, case.workspace, case.workspace, ["fixture"], 2, "verify")
    second = await docker.fresh().execute(case.run_id, case.workspace, case.workspace, ["fixture"], 2, "verify")
    assert [first.output, second.output] == ["1", "2"]
    assert (
        await docker.fresh().execute(case.run_id, case.workspace, case.workspace, ["fixture"], 2, "verify", ordinal=1)
        == first
    )
    assert len(docker.dispatches) == 2


async def test_lost_create_commit_acknowledgement_does_not_dispatch_or_reuse_slot(command_case):
    case = command_case
    original = case.store._unit
    lost = True

    @asynccontextmanager
    async def commit_unknown():
        nonlocal lost
        outer = not case.store.depth.get()
        async with original() as current:
            yield current
        if outer and lost and case.store.sandboxes.rows:
            lost = False
            raise TimeoutError("Controlled acknowledgement lost after durable commit")

    case.store._unit = commit_unknown
    with pytest.raises(TimeoutError, match="acknowledgement"):
        await command(case)
    assert case.calls == [] and len(case.store.sandboxes.rows) == 1
    with pytest.raises(SandboxReconciliationRequired, match="reconcil"):
        await command(case)
    assert case.calls == [] and len(case.store.sandboxes.rows) == 1


async def test_closed_preparation_advances_only_after_explicit_native_task_reconciliation(command_case):
    from uuid import NAMESPACE_URL, uuid5

    from firefly_forge.journal import digest

    case = command_case
    snapshot = case.store.run.data["sandbox_snapshot"]

    def payload(number, identity, previous):
        request = {"argv": ["never-dispatched"], "configuration": snapshot["configuration"]}
        return {
            "parent_attempt": 1,
            "ordinal": number,
            "container_name": "forge-sandbox-" + identity,
            "snapshot_digest": snapshot["digest"],
            "request_digest": digest(request),
            "request": request,
        }

    intent = await case.journal.sandbox_command_intent(case.run_id, "verify", 1, snapshot, payload)
    identity, request = intent["execution"]["execution_id"], intent["execution"]["request"]
    await case.journal.sandbox_effect_receipt(
        case.run_id, "verify", identity, effect="create", evidence={"mode": "host"}
    )
    await case.fresh()._cleanup_host(case.run_id, "verify", identity, request)
    with pytest.raises(SandboxReconciliationRequired, match="reconcil"):
        await command(case)
    task = case.store.tasks.rows[0]
    key = uuid5(NAMESPACE_URL, f"forge:{case.run_id}:verify:1").hex
    task.data["idempotency_key"], task.status = key, "needs_reconciliation"
    await case.journal.reconcile(case.run_id, key, evidence={"closed_preparation": identity}, outcome="retry")
    task.status = "running"
    await command(case)
    assert len(case.calls) == 1 and len(case.store.sandboxes.rows) == 2


@pytest.mark.parametrize("field", ["container_name", "request_digest"])
async def test_invalid_prepared_payload_never_creates_a_durable_slot(command_case, field):
    from firefly_forge.journal import digest

    case = command_case
    snapshot = case.store.run.data["sandbox_snapshot"]

    def payload(number, identity, previous):
        request = {"argv": ["controlled"], "configuration": snapshot["configuration"]}
        value = {
            "parent_attempt": 1,
            "ordinal": number,
            "container_name": "forge-sandbox-" + identity,
            "snapshot_digest": snapshot["digest"],
            "request_digest": digest(request),
            "request": request,
        }
        value[field] = "changed"
        return value

    with pytest.raises(ValueError, match="payload"):
        await case.journal.sandbox_command_intent(case.run_id, "verify", 1, snapshot, payload)
    assert not case.store.sandboxes.rows and not case.store.records.rows


@pytest.mark.parametrize("direct", [False, True])
async def test_pool_refuses_ambient_native_transaction_before_readonly_controller_http(pool_command_case, direct):
    from pyfly.data.transaction import TransactionTemplate
    from test_agentic_checkpoint_storage_host import MemoryManager

    pool = pool_command_case
    pool.case.journal.plans = Rows()
    manager = MemoryManager(pool.case.store, pool.case.journal)

    async def forbidden():
        raise AssertionError("Controller observation crossed the caller transaction")

    pool.controller.profile = forbidden
    executor = pool.fresh()
    target = executor.pool_worker if direct else executor
    async with TransactionTemplate(manager).transaction():
        with pytest.raises(ValueError, match="transaction"):
            await target.execute(pool.case.run_id, pool.workspace, pool.workspace, ["fixture"], 2, "verify")
    assert not pool.case.store.sandboxes.rows and pool.controller.submissions == 0
