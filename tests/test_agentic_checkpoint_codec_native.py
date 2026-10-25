# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared isolated SQL custody/SDK controls; no real provider or original CRM access."""

import base64
import hashlib
import json
from uuid import uuid4

import pytest
from pydantic_ai import models
from test_agentic_checkpoint_storage_host import checkpoint_for
from test_agentic_restore_receipt_native import native_restoration
from test_bridge_completion_native import mock_model_requests, prepared
from test_bridge_completion_records import snapshot

from firefly_forge import agentic_runtime
from firefly_forge.agent_checkpoint_models import RestoreInput
from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.agentic_checkpoint_boundary import MAX_PAYLOAD, BoundaryCheckpoint, _checkpoint_payload
from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.journal import Journal


async def compressed_for(body):
    small = await checkpoint_for(body)
    record = small.record.model_dump(mode="json")
    for message in record["history"]:
        for part in message["parts"]:
            if isinstance(part.get("content"), str):
                part["content"] = base64.b64encode(
                    hashlib.shake_256(b"fictional-native-shared-history").digest(100000)
                ).decode()
    record["request_history"] = record["history"]
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert len(raw) > MAX_PAYLOAD
    payload = _checkpoint_payload(raw)
    checkpoint = BoundaryCheckpoint.decode(payload, hashlib.sha256(payload).hexdigest())
    assert checkpoint.canonical_payload == raw
    return checkpoint


async def test_native_compressed_checkpoint_consumes_once_and_retains_encoded_wal(store, tmp_path):
    async with prepared(store, tmp_path) as (_, _, run, journal, completions, _, descriptor, body):
        checkpoint = await compressed_for(body)
        task = await journal.intent(run["id"], "implement")
        scope = AgentCheckpoints(completions).scope(run, task, capture=lambda wire: checkpoint)
        intent = await scope.admit(descriptor, body)
        response = snapshot()[1]["terminal"]["result_json"].encode()
        await scope.response(intent, response)
        with pytest.raises(ValueError, match="consum"):
            await scope.response(intent, response)
        entries = await journal.entries(run["id"])
        assert Journal.verify_chain(entries)
        record = next(r for r in entries if r["kind"] == "agent_checkpoint")
        assert record["payload"]["snapshot"].encode() == checkpoint.payload
        assert json.loads(record["payload"]["snapshot"])["version"] == 3
        assert sum(r["kind"] == "agent_consume" for r in entries) == 1


async def test_native_compressed_journal_current_credential_refuses_normal_and_ambient_reads(store, tmp_path):
    async with prepared(store, tmp_path) as (_, _, run, journal, completions, _, descriptor, body):
        checkpoint = await compressed_for(body)
        task = await journal.intent(run["id"], "implement")
        scope = AgentCheckpoints(completions).scope(run, task, capture=lambda wire: checkpoint)
        await scope.admit(descriptor, body)
        store.credential_registry = lambda: store.configuration.resolve("bridge")
        store.configuration.update(
            "bridge",
            ProviderConfiguration(
                values={
                    "token": base64.b64encode(
                        hashlib.shake_256(b"fictional-native-shared-history").digest(100000)
                    ).decode()[:64]
                }
            ),
        )
        with pytest.raises(ValueError, match="configured credentials"):
            await journal.entries(run["id"])
        async with store._unit():
            with pytest.raises(ValueError, match="configured credentials"):
                await journal.entries(run["id"])


async def test_native_large_sdk_retained_response_restores_without_replacement_post(store, tmp_path, monkeypatch):
    build = agentic_runtime.build_instruction
    monkeypatch.setattr(
        agentic_runtime,
        "build_instruction",
        lambda *a, **kw: build(*a, **kw) + "\n" + "Fictional compressed native instruction.\n" * 5500,
    )
    async with native_restoration(store, tmp_path, monkeypatch) as env:
        entries = await env.journal.entries(env.run_id)
        encoded = [r["payload"]["snapshot"] for r in entries if r["kind"] == "agent_checkpoint"][-1]
        assert json.loads(encoded)["version"] == 3
        checkpoint = BoundaryCheckpoint.decode(encoded.encode(), hashlib.sha256(encoded.encode()).hexdigest())
        assert len(checkpoint.canonical_payload) > MAX_PAYLOAD
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(env.request.request_id)
        ticket = await env.requests.get(env.request.request_id)
        assert ticket["status"] == "completed", ticket.get("result")
        assert len(env.posts) == 2
        assert (env.workspace / "service.py").read_text() == "VALUE = 1\n"
        assert env.schemas and set(env.schemas) == {store.schema}


async def test_native_compressed_unknown_result_has_no_consume_or_replacement_authority(store, tmp_path):
    async with prepared(store, tmp_path) as (_, _, run, journal, completions, _, descriptor, body):
        checkpoint = await compressed_for(body)
        task = await journal.intent(run["id"], "implement")
        service = AgentCheckpoints(completions)
        intent = await service.scope(run, task, capture=lambda wire: checkpoint).admit(descriptor, body)
        entries = await journal.entries(run["id"])
        retained = next(r for r in entries if r["kind"] == "agent_checkpoint")
        request = RestoreInput.model_validate(
            {
                "request_id": uuid4().hex,
                "expected_checkpoint_digest": retained["digest"],
                "expected_intent_digest": intent["digest"],
                "source": {"kind": "http_response", "record_digest": "0" * 64},
            }
        )
        with pytest.raises((KeyError, ValueError)):
            await service.restore_bundle(run["id"], descriptor["request_id"], request)
        assert await journal.entries(run["id"]) == entries
        assert not any(r["kind"] in {"agent_consume", "bridge_request_response"} for r in entries)
