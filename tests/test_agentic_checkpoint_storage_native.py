# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED ONLY: native storage/authority controls for the sole PostgreSQL operator.

The completion and capture are explicit fictional callback inputs. These controls
prove no live inference or Agentic restoration, and never activate Engine integration.
"""

import asyncio
import copy

import pytest
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from test_agentic_checkpoint_storage_host import checkpoint_for
from test_bridge_completion_native import prepared
from test_bridge_completion_records import snapshot

from firefly_forge.agent_checkpoints import CHECKPOINT, CONSUME, AgentCheckpoints
from firefly_forge.bridge_completions import INTENT, RESPONSE
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.project_access import AccessDenied
from firefly_forge.store import identifier


async def test_native_checkpoint_pair_exact_replay_and_concurrent_consume_once(store, tmp_path):
    async with prepared(store, tmp_path) as (_, _, run, journal, completions, _, descriptor, body):
        checkpoint = await checkpoint_for(body)
        task = await journal.intent(run["id"], "implement")
        service = AgentCheckpoints(completions)
        scope = service.scope(run, task, capture=lambda wire: checkpoint)
        intent = await scope.admit(descriptor, body)
        assert await scope.admit(descriptor, body) == intent
        response = snapshot()[1]["terminal"]["result_json"].encode()
        outcomes = await asyncio.gather(
            scope.response(intent, response), scope.response(intent, response), return_exceptions=True
        )
        assert sum(value is None for value in outcomes) == 1
        refused = [value for value in outcomes if isinstance(value, ValueError)]
        assert len(refused) == 1 and "consum" in str(refused[0])
        entries = await journal.entries(run["id"])
        for kind in (INTENT, CHECKPOINT, RESPONSE, CONSUME):
            assert sum(entry["kind"] == kind for entry in entries) == 1
        assert Journal.verify_chain(entries)
        receipt = await service.exact(run["id"], descriptor["request_id"])
        assert receipt["consume"] and response.decode() not in str(receipt)
        assert (await store.get_run(run["id"]))["bridge_completion_hold"]
        with pytest.raises(ValueError, match="consum"):
            await scope.admit(descriptor, body)


async def test_native_checkpoint_hook_failure_rolls_back_intent_checkpoint_and_hold(store, tmp_path, monkeypatch):
    async with prepared(store, tmp_path) as (_, _, run, journal, completions, _, descriptor, body):
        checkpoint = await checkpoint_for(body)
        task = await journal.intent(run["id"], "implement")
        scope = AgentCheckpoints(completions).scope(run, task, capture=lambda wire: checkpoint)
        before = await journal.entries(run["id"])
        before_run = await store.get_run(run["id"])
        save = journal.records.save

        async def fail(row):
            await save(row)
            if row.kind == CHECKPOINT:
                raise ValueError("Native checkpoint fixture failure after insert")

        monkeypatch.setattr(journal.records, "save", fail)
        with pytest.raises(ValueError, match="after insert"):
            await scope.admit(descriptor, body)
        assert await journal.entries(run["id"]) == before
        assert await store.get_run(run["id"]) == before_run
        assert current_unit_of_work() is None


async def test_native_consume_failure_retains_http_then_disabled_actor_cannot_consume(store, tmp_path, monkeypatch):
    async with prepared(store, tmp_path) as (_, actor, run, journal, completions, _, descriptor, body):
        checkpoint = await checkpoint_for(body)
        task = await journal.intent(run["id"], "implement")
        scope = AgentCheckpoints(completions).scope(run, task, capture=lambda wire: checkpoint)
        intent = await scope.admit(descriptor, body)
        response = snapshot()[1]["terminal"]["result_json"].encode()
        save = journal.records.save

        async def fail(row):
            await save(row)
            if row.kind == CONSUME:
                raise ValueError("Native consume fixture failure after insert")

        monkeypatch.setattr(journal.records, "save", fail)
        with pytest.raises(ValueError, match="after insert"):
            await scope.response(intent, response)
        monkeypatch.setattr(journal.records, "save", save)
        entries = await journal.entries(run["id"])
        assert sum(entry["kind"] == RESPONSE for entry in entries) == 1
        assert not any(entry["kind"] == CONSUME for entry in entries)
        admission = store.admit_credentials

        async def revoke(value):
            assert current_unit_of_work() is None
            await admission(value)
            if isinstance(value, tuple):
                async with store._unit():
                    users = ForgeIdentityUserRepository()
                    user = await users.find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
                    assert user is not None
                    user.enabled = False
                    await users.save(user)

        monkeypatch.setattr(store, "admit_credentials", revoke)
        with pytest.raises(AccessDenied):
            await scope.response(intent, response)
        async with store._unit():
            assert (
                await journal.records.checkpoint_record(identifier(run["id"]), descriptor["request_id"], CONSUME)
                is None
            )
        assert (await store.get_run(run["id"]))["bridge_completion_hold"]


async def test_native_checkpoint_immutable_and_consume_stays_used_after_cancel(store, tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    async with prepared(store, tmp_path) as (_, _, run, journal, completions, _, descriptor, body):
        checkpoint = await checkpoint_for(body)
        task = await journal.intent(run["id"], "implement")
        service = AgentCheckpoints(completions)
        scope = service.scope(run, task, capture=lambda wire: checkpoint)
        intent = await scope.admit(descriptor, body)
        response = snapshot()[1]["terminal"]["result_json"].encode()
        await scope.response(intent, response)
        receipt = await service.exact(run["id"], descriptor["request_id"])
        with pytest.raises(DBAPIError):
            async with store._unit() as session:
                await session.execute(
                    text("UPDATE forge_journal SET data = '{}'::jsonb WHERE run_id=:run AND kind=:kind"),
                    {"run": identifier(run["id"]), "kind": CHECKPOINT},
                )
        await store.update(run["id"], status="cancelled")
        before = copy.deepcopy(await journal.entries(run["id"]))
        assert await service.exact(run["id"], descriptor["request_id"]) == receipt
        with pytest.raises(asyncio.CancelledError):
            await scope.response(intent, response)
        with pytest.raises(asyncio.CancelledError):
            await scope.admit(descriptor, body)
        assert await journal.entries(run["id"]) == before
        assert (await store.get_run(run["id"]))["bridge_completion_hold"]
