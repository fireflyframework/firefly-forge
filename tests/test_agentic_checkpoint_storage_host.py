# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native services/template with an explicit memory backend; not PostgreSQL lock proof."""

import copy
import hashlib
import importlib
import json
from contextlib import asynccontextmanager

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage, UsageLimits
from pyfly.data.transaction import TransactionTemplate, current_unit_of_work
from pyfly.data.transaction.manager import TransactionCapabilities
from pyfly.data.transaction.unit_of_work import UnitOfWork
from test_bridge_completion_journal_host import fixture
from test_bridge_completion_records import snapshot
from test_bridge_inflight_cancel import RUN

from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary
from firefly_forge.bridge_completions import INTENT, RESPONSE, BridgeCompletions
from firefly_forge.journal import Journal


class MemoryManager:
    datasource = "checkpoint-host"
    capabilities = TransactionCapabilities(backend="host-memory", supports_savepoints=False)

    def __init__(self, store, journal):
        self.store, self.journal = store, journal
        self.events = []

    async def begin(self, definition):
        unit = UnitOfWork(self, self.datasource, object(), definition=definition)
        unit.attributes["snapshot"] = copy.deepcopy(
            (self.store.run.data, self.journal.records.rows, self.journal.tasks.rows, self.journal.plans.rows)
        )
        self.events.append("begin")
        return unit

    async def commit(self, unit):
        self.events.append("commit")

    async def rollback(self, unit):
        data, records, tasks, plans = unit.attributes["snapshot"]
        self.store.run.data = data
        self.journal.records.rows, self.journal.tasks.rows, self.journal.plans.rows = records, tasks, plans
        self.events.append("rollback")

    async def release(self, unit):
        pass

    async def open_auto_unit(self, **kwargs):
        raise AssertionError("Explicit native transactions only")

    async def create_savepoint(self, unit):
        raise AssertionError("No host savepoint emulation")

    async def release_savepoint(self, unit, savepoint):
        raise AssertionError("No host savepoint emulation")

    async def rollback_to_savepoint(self, unit, savepoint):
        raise AssertionError("No host savepoint emulation")

    def resource_active(self, unit):
        return True

    def marks_rollback_only(self, unit, error):
        return False

    def is_disconnect(self, error):
        return False


async def checkpoint_for(body):
    def unused(*args):
        raise AssertionError("Storage controls must not request a model")

    model = FunctionModel(unused)
    history = [ModelRequest(parts=[UserPromptPart("Fictional storage fixture")])]
    context = RunContext(deps=None, model=model, usage=RunUsage(), usage_limits=UsageLimits(), messages=history)
    request = ModelRequestContext(
        model=model, messages=history, model_settings=None, model_request_parameters=ModelRequestParameters()
    )
    boundary = AgenticBoundary(lambda: {"files_written": []})
    with boundary.invocation():
        await boundary.before_model_request(context, request)
        return boundary.capture(body)


async def setup(tmp_path, monkeypatch):
    name = "firefly_forge.agent_checkpoints"
    assert importlib.util.find_spec(name) is not None, "Atomic native checkpoint storage is missing"
    store, journal = await fixture(tmp_path, monkeypatch)
    store.credential_registry = dict
    manager = MemoryManager(store, journal)

    @asynccontextmanager
    async def unit():
        async with TransactionTemplate(manager).transaction() as current:
            yield current.resource

    store._unit = unit
    records = journal.records

    async def checkpoint_record(run_id, key, kind):
        return await records.bridge_record(run_id, key, kind)

    async def checkpoint_budget(run_id):
        selected = [r for r in records.rows if r.kind in {"agent_checkpoint", "agent_consume"}]
        return {
            "count": sum(r.kind == "agent_checkpoint" for r in selected),
            "consumes": sum(r.kind == "agent_consume" for r in selected),
            "bytes": sum(len(json.dumps(r.data).encode()) for r in selected),
            "max_bytes": max((len(json.dumps(r.data).encode()) for r in selected), default=0),
        }

    bridge_budget = records.bridge_budget

    async def shared_budget(run_id):
        return {
            **await bridge_budget(run_id),
            "checkpoints": sum(r.kind == "agent_checkpoint" for r in records.rows),
            "checkpoint_consumes": sum(r.kind == "agent_consume" for r in records.rows),
        }

    records.bridge_budget = shared_budget
    records.checkpoint_record = checkpoint_record
    records.checkpoint_budget = checkpoint_budget
    task = await journal.intent(RUN, "implement")
    run = await store.get_run(RUN)
    body = b'{"model":"bridge/default","messages":[]}'
    descriptor, remote = snapshot()
    descriptor.update(
        endpoint="http://localhost:19191/v1", wire_bytes=len(body), wire_sha256=hashlib.sha256(body).hexdigest()
    )
    checkpoint = await checkpoint_for(body)
    service = importlib.import_module(name).AgentCheckpoints(BridgeCompletions(store, journal))
    scope = service.scope(run, task, capture=lambda wire: checkpoint)
    response = remote["terminal"]["result_json"].encode()
    return store, journal, manager, service, scope, descriptor, checkpoint, body, response


async def test_native_intent_and_checkpoint_are_one_committed_unit_and_exact_replay(tmp_path, monkeypatch):
    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    original = await scope.admit(descriptor, body)
    pair = [r.data for r in journal.records.rows if r.kind in {INTENT, "agent_checkpoint"}]
    assert len(pair) == 2 and pair[0] == original
    assert pair[1]["payload"]["intent_digest"] == original["digest"]
    assert pair[1]["payload"]["checkpoint_digest"] == checkpoint.digest
    assert await scope.admit(descriptor, body) == original
    assert len([r for r in journal.records.rows if r.kind == "agent_checkpoint"]) == 1
    assert manager.events[-1] == "commit" and current_unit_of_work() is None
    assert Journal.verify_chain([r.data for r in journal.records.rows])
    assert store.run.data["bridge_completion_hold"]["reason"] == "graph_checkpoint_unavailable"


async def test_checkpoint_failure_rolls_back_both_new_rows_and_hold(tmp_path, monkeypatch):
    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    before = copy.deepcopy(([r.data for r in journal.records.rows], store.run.data))
    save = journal.records.save

    async def fail(row):
        await save(row)
        if row.kind == "agent_checkpoint":
            raise ValueError("Owned checkpoint save refused")

    monkeypatch.setattr(journal.records, "save", fail)
    with pytest.raises(ValueError, match="save refused"):
        await scope.admit(descriptor, body)
    assert ([r.data for r in journal.records.rows], store.run.data) == before
    assert "rollback" in manager.events and current_unit_of_work() is None


async def test_response_consumption_uses_retained_record_once_and_never_rearms(tmp_path, monkeypatch):
    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    intent = await scope.admit(descriptor, body)
    await scope.response(intent, response)
    consumes = [r.data for r in journal.records.rows if r.kind == "agent_consume"]
    retained = next(r.data for r in journal.records.rows if r.kind == RESPONSE)
    assert len(consumes) == 1
    assert consumes[0]["payload"]["response_record_digest"] == retained["digest"]
    assert consumes[0]["payload"]["response_sha256"] == hashlib.sha256(response).hexdigest()
    assert manager.events[-1] == "commit"
    before = copy.deepcopy([r.data for r in journal.records.rows])
    with pytest.raises(ValueError, match="consum"):
        await scope.response(intent, response)
    with pytest.raises(ValueError, match="consum"):
        await scope.admit(descriptor, body)
    assert [r.data for r in journal.records.rows] == before
    assert store.run.data.get("bridge_completion_hold")


async def test_failed_consume_commit_rolls_back_only_consume_and_never_exposes_response(tmp_path, monkeypatch):
    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    intent = await scope.admit(descriptor, body)
    save = journal.records.save

    async def fail(row):
        await save(row)
        if row.kind == "agent_consume":
            raise ValueError("Owned consume save refused")

    monkeypatch.setattr(journal.records, "save", fail)
    with pytest.raises(ValueError, match="consume save refused"):
        await scope.response(intent, response)
    assert len([r for r in journal.records.rows if r.kind == RESPONSE]) == 1
    assert not any(r.kind == "agent_consume" for r in journal.records.rows)
    assert any(r.kind == "agent_checkpoint" for r in journal.records.rows)
    assert manager.events[-1] == "rollback" and current_unit_of_work() is None
    assert store.run.data.get("bridge_completion_hold")


@pytest.mark.parametrize("replacement", [b"{}", b'{"error":{"message":"Fictional error"}}'])
async def test_retained_http_record_alone_does_not_make_an_original_success(tmp_path, monkeypatch, replacement):
    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    intent = await scope.admit(descriptor, body)
    with pytest.raises(ValueError):
        await scope.response(intent, replacement)
    assert not any(r.kind == "agent_consume" for r in journal.records.rows)
    assert store.run.data.get("bridge_completion_hold")


async def test_mutable_body_and_changed_wire_are_refused_before_intent(tmp_path, monkeypatch):
    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    before = copy.deepcopy([r.data for r in journal.records.rows])
    with pytest.raises(ValueError, match="immutable"):
        await scope.admit(descriptor, bytearray(body))
    with pytest.raises(ValueError, match="wire"):
        await scope.admit(descriptor, body + b" ")
    assert [r.data for r in journal.records.rows] == before


async def test_conflicting_checkpoint_refuses_exact_intent_without_replacing_history(tmp_path, monkeypatch):
    from dataclasses import replace

    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    await scope.admit(descriptor, body)
    changed = json.loads(checkpoint.payload)
    changed["state"]["files_written"] = ["different.txt"]
    payload = json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    other = replace(checkpoint, payload=payload, digest=hashlib.sha256(payload).hexdigest())
    task = await journal.intent(RUN, "implement")
    other_scope = service.scope(await store.get_run(RUN), task, capture=lambda wire: other)
    before = copy.deepcopy([r.data for r in journal.records.rows])
    with pytest.raises(ValueError, match="different checkpoint"):
        await other_scope.admit(descriptor, body)
    assert [r.data for r in journal.records.rows] == before


@pytest.mark.parametrize("budget", [{"count": 100}, {"bytes": 4 * 1024 * 1024 + 1}, {"max_bytes": 262145}])
async def test_scalar_budget_refuses_before_checkpoint_materialization(tmp_path, monkeypatch, budget):
    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    original = journal.records.checkpoint_budget
    reads = []

    async def full(run_id):
        return {**await original(run_id), **budget}

    async def no_read(*args):
        reads.append(args)
        raise AssertionError("Oversized checkpoint must not be materialized")

    monkeypatch.setattr(journal.records, "checkpoint_budget", full)
    if budget != {"count": 100}:
        monkeypatch.setattr(journal.records, "checkpoint_record", no_read)
    before = copy.deepcopy(([r.data for r in journal.records.rows], store.run.data))
    with pytest.raises(ValueError, match="retention"):
        await scope.admit(descriptor, body)
    assert ([r.data for r in journal.records.rows], store.run.data) == before
    assert not reads


async def test_newly_registered_credential_after_checkpoint_append_rolls_back_pair(tmp_path, monkeypatch):
    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    before = copy.deepcopy(([r.data for r in journal.records.rows], store.run.data))
    save = journal.records.save
    registered = False

    async def register(row):
        nonlocal registered
        await save(row)
        if row.kind == "agent_checkpoint":
            registered = True

    def reject(value):
        if registered and "Fictional storage fixture" in str(value):
            raise ValueError("Current credential reflected")

    monkeypatch.setattr(journal.records, "save", register)
    monkeypatch.setattr(store, "reject_credentials", reject)
    with pytest.raises(ValueError, match="credential"):
        await scope.admit(descriptor, body)
    assert ([r.data for r in journal.records.rows], store.run.data) == before


async def test_newly_registered_credential_after_consume_append_rolls_back_consume(tmp_path, monkeypatch):
    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    intent = await scope.admit(descriptor, body)
    save = journal.records.save
    registered = False

    async def register(row):
        nonlocal registered
        await save(row)
        if row.kind == "agent_consume":
            registered = True

    def reject(value):
        if registered:
            raise ValueError("Current credential reflected")

    monkeypatch.setattr(journal.records, "save", register)
    monkeypatch.setattr(store, "reject_credentials", reject)
    with pytest.raises(ValueError, match="credential"):
        await scope.response(intent, response)
    assert not any(r.kind == "agent_consume" for r in journal.records.rows)
    assert any(r.kind == RESPONSE for r in journal.records.rows)


async def test_exact_receipt_read_returns_no_completion_and_does_not_consume(tmp_path, monkeypatch):
    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    intent = await scope.admit(descriptor, body)

    async def reader(run_id, actor, **kwargs):
        return await store.get_run(run_id)

    monkeypatch.setattr(service.completions, "_reader", reader)
    before = copy.deepcopy([r.data for r in journal.records.rows])
    result = await service.exact(RUN, intent["idempotency_key"])
    assert set(result) == {"checkpoint", "consume"} and result["consume"] is None
    assert response.decode() not in json.dumps(result)
    assert [r.data for r in journal.records.rows] == before
    await scope.response(intent, response)
    consumed = await service.exact(RUN, intent["idempotency_key"])
    assert consumed["consume"]["payload"]["response_record_digest"]
    assert response.decode() not in json.dumps(consumed)
    with pytest.raises(ValueError, match="consum"):
        await scope.response(intent, response)


async def test_shared_response_budget_reserves_pending_consume_envelopes(tmp_path, monkeypatch):
    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    original = journal.records.bridge_budget

    async def budget(run_id):
        return {
            **await original(run_id),
            "bytes": 16 * 1024 * 1024 - 4095,
            "intents": 1,
            "responses": 1,
            "checkpoints": 1,
            "checkpoint_consumes": 0,
        }

    monkeypatch.setattr(journal.records, "bridge_budget", budget)
    with pytest.raises(ValueError, match="retention"):
        await service.completions._budget(RUN)
    with pytest.raises(ValueError, match="reserved"):
        await journal._bridge_read_bound(RUN)


async def test_authority_change_after_credential_admission_refuses_before_write(tmp_path, monkeypatch):
    from firefly_forge.project_access import AccessDenied

    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    before = copy.deepcopy([r.data for r in journal.records.rows])

    async def changed(value):
        assert current_unit_of_work() is None
        store.run.data["source_tree"] = "changed"

    monkeypatch.setattr(store, "admit_credentials", changed)
    with pytest.raises(AccessDenied, match="cut changed"):
        await scope.admit(descriptor, body)
    assert [r.data for r in journal.records.rows] == before


async def test_cancellation_after_consume_save_commits_no_consume_and_restores_unit(tmp_path, monkeypatch):
    import asyncio

    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    intent = await scope.admit(descriptor, body)
    save = journal.records.save

    async def cancel(row):
        await save(row)
        if row.kind == "agent_consume":
            raise asyncio.CancelledError()

    monkeypatch.setattr(journal.records, "save", cancel)
    with pytest.raises(asyncio.CancelledError):
        await scope.response(intent, response)
    assert not any(r.kind == "agent_consume" for r in journal.records.rows)
    assert any(r.kind == RESPONSE for r in journal.records.rows)
    assert manager.events[-1] == "rollback" and current_unit_of_work() is None


async def test_consumed_boundary_stays_used_after_cancelled_run(tmp_path, monkeypatch):
    import asyncio

    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    intent = await scope.admit(descriptor, body)
    await scope.response(intent, response)
    before = copy.deepcopy([r.data for r in journal.records.rows])
    store.run.data["status"] = "cancelled"

    async def execution(run, reader):
        if run["status"] == "cancelled":
            raise asyncio.CancelledError()

    monkeypatch.setattr(service.completions, "_execution", execution)
    with pytest.raises(asyncio.CancelledError):
        await scope.admit(descriptor, body)
    assert [r.data for r in journal.records.rows] == before
    assert len([r for r in journal.records.rows if r.kind == "agent_consume"]) == 1


async def test_generic_journal_read_admits_checkpoint_history_before_returning_snapshot(tmp_path, monkeypatch):
    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    await scope.admit(descriptor, body)
    admitted = []

    async def current_credentials(value):
        assert current_unit_of_work() is None
        admitted.append(copy.deepcopy(value))
        if "Fictional storage fixture" in str(value):
            raise ValueError("Current checkpoint credential reflected")

    monkeypatch.setattr(store, "admit_credentials", current_credentials)
    before = copy.deepcopy([r.data for r in journal.records.rows])
    with pytest.raises(ValueError, match="checkpoint credential"):
        await journal.entries(RUN)
    assert admitted and [r.data for r in journal.records.rows] == before


async def test_original_completion_callback_commits_consumption_before_exposure(tmp_path, monkeypatch):
    from firefly_forge.agentic_checkpoint_boundary import OriginalCompletion

    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    intent = await scope.admit(descriptor, body)

    async def consume(checkpoint_digest, response_digest):
        assert checkpoint_digest == checkpoint.digest
        assert response_digest == hashlib.sha256(response).hexdigest()
        await scope.response(intent, response)
        assert manager.events[-1] == "commit" and current_unit_of_work() is None
        assert len([r for r in journal.records.rows if r.kind == "agent_consume"]) == 1

    original = OriginalCompletion(checkpoint, response, consume)
    assert await original.expose(body) == response
    with pytest.raises(ValueError):
        await original.expose(body)
    assert len([r for r in journal.records.rows if r.kind == "agent_consume"]) == 1


async def test_postcommit_refusal_never_rearms_a_durably_consumed_boundary(tmp_path, monkeypatch):
    from firefly_forge.project_access import AccessDenied

    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    intent = await scope.admit(descriptor, body)
    commit = manager.commit

    async def changed(unit):
        await commit(unit)
        if any(r.kind == "agent_consume" for r in journal.records.rows):
            store.run.data["source_tree"] = "changed-after-commit"

    monkeypatch.setattr(manager, "commit", changed)
    with pytest.raises(AccessDenied, match="cut changed"):
        await scope.response(intent, response)
    assert len([r for r in journal.records.rows if r.kind == "agent_consume"]) == 1
    assert manager.events[-1] == "commit" and current_unit_of_work() is None
    assert store.run.data.get("bridge_completion_hold")


@pytest.mark.parametrize(("kind", "limit"), [("agent_checkpoint", 262144), ("agent_consume", 4096)])
async def test_actual_repository_refuses_oversized_json_before_entity_read(monkeypatch, kind, limit):
    from types import SimpleNamespace
    from uuid import UUID

    from sqlalchemy.dialects import postgresql

    from firefly_forge.persistence import ForgeJournalRepository

    statements = []

    async def scalar(statement):
        statements.append(str(statement.compile(dialect=postgresql.dialect())))
        assert len(statements) == 1
        return limit + 1

    repository = ForgeJournalRepository(session=SimpleNamespace(scalar=scalar))
    with pytest.raises(ValueError, match="bounded read"):
        await repository.checkpoint_record(UUID(RUN), "fbr1_" + "a" * 32, kind)
    assert len(statements) == 1 and "octet_length(CAST(" in statements[0]


async def test_runtime_metadata_and_credentials_are_read_only_outside_native_units(tmp_path, monkeypatch):
    from firefly_forge import agentic_checkpoint_boundary as boundary_module

    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    version = boundary_module.version
    checks = []

    def outside(name):
        assert current_unit_of_work() is None, "Runtime metadata read inside native transaction"
        checks.append(name)
        return version(name)

    async def credentials(value):
        assert current_unit_of_work() is None, "Credential admission inside native transaction"

    monkeypatch.setattr(boundary_module, "version", outside)
    monkeypatch.setattr(store, "admit_credentials", credentials)
    intent = await scope.admit(descriptor, body)
    assert await scope.admit(descriptor, body) == intent
    await scope.response(intent, response)
    assert checks


@pytest.mark.parametrize("operation", ["checkpoint", "consume"])
async def test_actual_credential_configuration_never_reads_or_locks_inside_native_unit(
    tmp_path, monkeypatch, operation
):
    from types import MethodType

    from firefly_forge.store import Store

    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    store.credential_registry = lambda: store.configuration.resolve("bridge")
    monkeypatch.setattr(store, "reject_credentials", MethodType(Store.reject_credentials, store))
    intent = await scope.admit(descriptor, body) if operation == "consume" else None
    resolve = store.configuration.resolve
    acquire = type(store.configuration.lock).acquire
    reads, locks = [], []

    def outside_resolve(provider):
        assert current_unit_of_work() is None, "Actual credential configuration read inside native writer unit"
        reads.append(provider)
        return resolve(provider)

    def outside_lock(lock, *args, **kwargs):
        assert current_unit_of_work() is None, "Actual credential FileLock acquired inside native writer unit"
        locks.append(lock.lock_file)
        return acquire(lock, *args, **kwargs)

    monkeypatch.setattr(store.configuration, "resolve", outside_resolve)
    monkeypatch.setattr(type(store.configuration.lock), "acquire", outside_lock)
    if operation == "checkpoint":
        await scope.admit(descriptor, body)
        assert sum(row.kind == "agent_checkpoint" for row in journal.records.rows) == 1
    else:
        await scope.response(intent, response)
        assert sum(row.kind == "agent_consume" for row in journal.records.rows) == 1
    assert reads and locks and current_unit_of_work() is None


@pytest.mark.parametrize("kind", ["agent_checkpoint", "agent_consume"])
async def test_real_configuration_change_after_append_refuses_and_rolls_back_writer(tmp_path, monkeypatch, kind):
    from types import MethodType

    from firefly_forge.configuration import ConfigurationStore, ProviderConfiguration
    from firefly_forge.store import Store

    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    monkeypatch.setattr(store, "reject_credentials", MethodType(Store.reject_credentials, store))
    intent = await scope.admit(descriptor, body) if kind == "agent_consume" else None
    other = ConfigurationStore(store.configuration.directory.parent)
    save = journal.records.save

    async def changed(row):
        await save(row)
        if row.kind == kind:
            other.update("bridge", ProviderConfiguration(values={"token": "Fictional storage fixture"}))

    monkeypatch.setattr(journal.records, "save", changed)
    with pytest.raises(ValueError, match="Credential sources changed"):
        if kind == "agent_checkpoint":
            await scope.admit(descriptor, body)
        else:
            await scope.response(intent, response)
    assert not any(row.kind == kind for row in journal.records.rows)
    assert manager.events[-1] == "rollback" and current_unit_of_work() is None
    if kind == "agent_checkpoint":
        assert not any(row.kind == INTENT for row in journal.records.rows)
        assert "bridge_completion_hold" not in store.run.data
    else:
        assert any(row.kind == RESPONSE for row in journal.records.rows)


@pytest.mark.parametrize("kind", ["agent_checkpoint", "agent_consume"])
async def test_registry_change_after_commit_refuses_exposure_without_rearming(tmp_path, monkeypatch, kind):
    from types import MethodType

    from firefly_forge.store import Store

    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    registry = {}
    store.credential_registry = lambda: dict(registry)
    monkeypatch.setattr(store, "reject_credentials", MethodType(Store.reject_credentials, store))
    intent = await scope.admit(descriptor, body) if kind == "agent_consume" else None
    commit = manager.commit

    async def changed(unit):
        await commit(unit)
        if any(row.kind == kind for row in journal.records.rows):
            registry["CURRENT_TOKEN"] = "Original Ω result" if kind == "agent_consume" else "Fictional storage fixture"

    monkeypatch.setattr(manager, "commit", changed)
    with pytest.raises(ValueError, match="configured credentials"):
        if kind == "agent_checkpoint":
            await scope.admit(descriptor, body)
        else:
            await scope.response(intent, response)
    assert sum(row.kind == kind for row in journal.records.rows) == 1
    assert store.run.data.get("bridge_completion_hold")
    before = copy.deepcopy([row.data for row in journal.records.rows])
    with pytest.raises(ValueError):
        if kind == "agent_checkpoint":
            await scope.admit(descriptor, body)
        else:
            await scope.response(intent, response)
    assert [row.data for row in journal.records.rows] == before and current_unit_of_work() is None


@pytest.mark.parametrize("consumed", [False, True])
async def test_exact_read_rejects_late_real_registry_change_without_rearming(tmp_path, monkeypatch, consumed):
    from types import MethodType

    from firefly_forge.store import Store

    store, journal, manager, service, scope, descriptor, checkpoint, body, response = await setup(tmp_path, monkeypatch)
    registered = {}
    resolve = store.configuration.resolve
    acquire = type(store.configuration.lock).acquire
    reads, locks = [], []

    def outside_resolve(provider):
        assert current_unit_of_work() is None, "Exact read performed credential configuration I/O under its unit"
        reads.append(provider)
        return resolve(provider)

    def outside_lock(lock, *args, **kwargs):
        assert current_unit_of_work() is None, "Exact read acquired a credential FileLock under its unit"
        locks.append(lock.lock_file)
        return acquire(lock, *args, **kwargs)

    def credentials():
        assert current_unit_of_work() is None, "Exact read invoked its credential callback under its unit"
        return {**store.configuration.resolve("bridge"), **registered}

    monkeypatch.setattr(store.configuration, "resolve", outside_resolve)
    monkeypatch.setattr(type(store.configuration.lock), "acquire", outside_lock)
    store.credential_registry = credentials
    monkeypatch.setattr(store, "reject_credentials", MethodType(Store.reject_credentials, store))
    intent = await scope.admit(descriptor, body)
    if consumed:
        await scope.response(intent, response)
    before = copy.deepcopy(([row.data for row in journal.records.rows], store.run.data))
    reader_calls = 0

    async def reader(run_id, actor, **kwargs):
        nonlocal reader_calls
        reader_calls += 1
        if reader_calls == 2:
            assert current_unit_of_work() is not None
            registered["CURRENT_TOKEN"] = "Fictional storage fixture"
        return await store.get_run(run_id)

    monkeypatch.setattr(service.completions, "_reader", reader)
    with pytest.raises(ValueError, match="configured credentials"):
        await service.exact(RUN, intent["idempotency_key"])
    assert reader_calls == 2 and reads and locks and current_unit_of_work() is None
    assert ([row.data for row in journal.records.rows], store.run.data) == before
    assert sum(row.kind == "agent_consume" for row in journal.records.rows) == int(consumed)
    assert store.run.data.get("bridge_completion_hold")
    if consumed:
        registered.clear()
        with pytest.raises(ValueError, match="consum"):
            await scope.response(intent, response)
        assert ([row.data for row in journal.records.rows], store.run.data) == before
