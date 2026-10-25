# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Lossless accumulated SDK history remains bounded at its encoded custody boundary."""

import hashlib
import json

import httpx2
import pytest
from fireflyframework_agentic.agents import FireflyAgent
from fireflyframework_agentic.config import FireflyAgenticConfig
from fireflyframework_agentic.model_utils import serialize_model_messages
from fireflyframework_agentic.tools import firefly_tool
from pydantic_ai import models
from pydantic_ai.usage import UsageLimits
from test_agentic_checkpoint_boundary import CutAfterOriginalPost, completion
from test_bridge_completion_native import mock_model_requests

from firefly_forge.agentic_checkpoint_boundary import (
    MAX_PAYLOAD,
    AgenticBoundary,
    BoundaryCheckpoint,
    OriginalCompletion,
)
from firefly_forge.providers import BridgeProvider


async def test_actual_sdk_third_frame_compresses_and_restores_exact_request_without_repeating_tools(monkeypatch):
    reads, posts, captures, consumed = [], [], [], []
    text = "Fictional retained tool source line.\n" * 1400

    async def read_file(path: str) -> str:
        reads.append(path)
        return text

    tool = firefly_tool("read_file", description="Read fictional source", auto_register=False)(read_file)

    def agent(model, boundary):
        return FireflyAgent(
            name="elastic-history",
            model=model,
            tools=[tool],
            auto_register=False,
            config=FireflyAgenticConfig(default_temperature=None, rate_limit_max_retries=0),
            capabilities=[boundary],
        )

    boundary = AgenticBoundary(lambda: {"files_written": []})
    model = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}).model()

    def transport(request):
        posts.append(request.content)
        if len(posts) == 3:
            raise CutAfterOriginalPost()
        return httpx2.Response(
            200,
            content=completion(
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "read-" + str(len(posts)),
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": str(len(posts)) + ".py"}),
                            },
                        }
                    ],
                },
                tokens=11,
                finish="tool_calls",
            ),
        )

    model.client._client._transport = httpx2.MockTransport(transport)
    model.client._client._mounts = {}
    send = model.client._send_request

    async def capture(request, **kwargs):
        captures.append(boundary.capture(await request.aread()))
        return await send(request, **kwargs)

    monkeypatch.setattr(model.client, "_send_request", capture)
    try:
        with (
            models.override_allow_model_requests(False),
            mock_model_requests(model),
            boundary.invocation(),
            pytest.raises(CutAfterOriginalPost),
        ):
            await agent(model, boundary).run(
                "Fictional product brief. " * 900, usage_limits=UsageLimits(request_limit=4, tool_calls_limit=2)
            )
        assert reads == ["1.py", "2.py"] and len(posts) == 3
        import base64
        import zlib

        envelope = json.loads(captures[-1].payload)
        envelope["data"] = base64.b64encode(
            zlib.compress(zlib.decompress(base64.b64decode(envelope["data"])), 1)
        ).decode()
        checkpoint = BoundaryCheckpoint.decode(*reseal_envelope(envelope))
        raw = json.dumps(
            checkpoint.record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        assert len(raw) > MAX_PAYLOAD and len(checkpoint.payload) <= MAX_PAYLOAD
        assert json.loads(checkpoint.payload)["version"] == 2
        assert checkpoint.usage.requests == checkpoint.usage.tool_calls == 2
        assert checkpoint.record.wire_bytes == len(posts[-1])
        assert checkpoint.wire_sha256 == hashlib.sha256(posts[-1]).hexdigest()
        restored = AgenticBoundary(lambda: {"files_written": []}, restore=checkpoint)
        restored_model = BridgeProvider(
            {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
        ).model()

        restored_model.client._client._transport = httpx2.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError("No replacement request"))
        )
        restored_model.client._client._mounts = {}

        async def consume(checkpoint_digest, response_digest):
            consumed.append((checkpoint_digest, response_digest))

        original = completion({"content": "Original retained final answer"}, tokens=19)
        replay = OriginalCompletion(checkpoint, original, consume)

        async def retained(request, **kwargs):
            body = await request.aread()
            assert body == posts[-1]
            again = restored.capture(body)
            assert again.payload == checkpoint.payload and again.digest == checkpoint.digest
            assert serialize_model_messages(again.history) == serialize_model_messages(checkpoint.history)
            return httpx2.Response(200, content=await replay.expose(body), request=request)

        monkeypatch.setattr(restored_model.client, "_send_request", retained)
        try:
            with (
                models.override_allow_model_requests(False),
                mock_model_requests(restored_model),
                restored.invocation(),
            ):
                result = await agent(restored_model, restored).run(**checkpoint.resume_kwargs())
            assert result.output == "Original retained final answer"
            assert result.usage.requests == 3 and result.usage.tool_calls == 2
            assert reads == ["1.py", "2.py"] and len(posts) == 3
            assert consumed == [(checkpoint.digest, hashlib.sha256(original).hexdigest())]
            with pytest.raises(ValueError, match="consum"):
                await replay.expose(posts[-1])
        finally:
            await restored_model.client.close()
    finally:
        await model.client.close()


@pytest.fixture
async def large_checkpoint():
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    captures = []
    boundary = AgenticBoundary(lambda: {"files_written": ["app.py"]})

    def model(messages, info):
        captures.append(boundary.capture(b"fictional-wire"))
        return ModelResponse(parts=[TextPart("Fictional")])

    with boundary.invocation():
        await FireflyAgent(
            name="large-codec", model=FunctionModel(model), auto_register=False, capabilities=[boundary]
        ).run("Fictional source line.\n" * 6000)
    return captures[0]


def reseal_envelope(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return payload, hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    "change",
    [
        "size-bool",
        "too-large",
        "short-size",
        "wrong-hash",
        "base64",
        "truncated",
        "trailing",
        "concatenated",
        "raw-noncanonical",
        "bomb",
        "extra",
        "version-bool",
        "codec",
    ],
)
def test_compressed_envelope_refuses_noncanonical_corrupt_or_unbounded_input(large_checkpoint, change):
    import base64
    import zlib

    value = json.loads(large_checkpoint.payload)
    assert value["version"] == 2
    raw = zlib.decompress(base64.b64decode(value["data"]))
    if change == "size-bool":
        value["raw_bytes"] = True
    elif change == "too-large":
        value["raw_bytes"] = 8 * 1024**2 + 1
    elif change == "short-size":
        value["raw_bytes"] -= 1
    elif change == "wrong-hash":
        value["raw_sha256"] = "0" * 64
    elif change == "base64":
        value["data"] += "\n"
    elif change == "truncated":
        value["data"] = base64.b64encode(base64.b64decode(value["data"])[:-1]).decode()
    elif change in {"trailing", "concatenated"}:
        suffix = b"fictional-extra" if change == "trailing" else zlib.compress(b"other")
        value["data"] = base64.b64encode(base64.b64decode(value["data"]) + suffix).decode()
    elif change == "raw-noncanonical":
        raw += b"\n"
        value["data"] = base64.b64encode(zlib.compress(raw, 9)).decode()
        value["raw_sha256"] = hashlib.sha256(raw).hexdigest()
        value["raw_bytes"] = len(raw)
    elif change == "bomb":
        raw = b"x" * (8 * 1024**2 + 1)
        value["data"] = base64.b64encode(zlib.compress(raw, 9)).decode()
        value["raw_sha256"] = hashlib.sha256(raw).hexdigest()
        value["raw_bytes"] = 8 * 1024**2
    elif change == "extra":
        value["extra"] = "ignored"
    elif change == "version-bool":
        value["version"] = True
    elif change == "codec":
        value["codec"] = "gzip"
    with pytest.raises(ValueError):
        BoundaryCheckpoint.decode(*reseal_envelope(value))


def test_legacy_raw_checkpoint_retains_immutable_encoding(large_checkpoint):
    import base64
    import zlib

    large = json.loads(large_checkpoint.payload)
    record = json.loads(zlib.decompress(base64.b64decode(large["data"])))
    for message in record["history"]:
        for part in message["parts"]:
            if isinstance(part.get("content"), str):
                part["content"] = "Fictional legacy source"
    record["request_history"] = record["history"]
    payload, digest = reseal_envelope(record)
    assert len(payload) < MAX_PAYLOAD
    checkpoint = BoundaryCheckpoint.decode(payload, digest)
    assert checkpoint.payload == payload and checkpoint.digest == digest
    assert checkpoint.record.model_dump(mode="json") == record
    assert checkpoint.state == {"files_written": ["app.py"]}


def test_valid_alternate_zlib_stream_retains_its_original_encoded_identity(large_checkpoint):
    import base64
    import zlib

    value = json.loads(large_checkpoint.payload)
    raw = zlib.decompress(base64.b64decode(value["data"]))
    value["data"] = base64.b64encode(zlib.compress(raw, 1)).decode()
    payload, digest = reseal_envelope(value)
    restored = BoundaryCheckpoint.decode(payload, digest)
    assert restored.payload == payload and restored.digest == digest
    assert restored.canonical_payload == raw
    assert restored.record == large_checkpoint.record


@pytest.mark.parametrize("stage", ["capture", "consume", "exact", "journal", "journal-ambient"])
async def test_compressed_history_current_credentials_refuse_custody_without_hiding_secret(
    tmp_path, monkeypatch, large_checkpoint, stage
):
    from types import MethodType

    from test_agentic_checkpoint_storage_host import setup
    from test_bridge_inflight_cancel import RUN

    from firefly_forge.agentic_checkpoint_boundary import _checkpoint_payload
    from firefly_forge.store import Store

    store, journal, manager, service, scope, descriptor, small, body, response = await setup(tmp_path, monkeypatch)
    record = large_checkpoint.record.model_dump(mode="json")
    record.update(wire_sha256=hashlib.sha256(body).hexdigest(), wire_bytes=len(body))
    raw, _ = reseal_envelope(record)
    payload = _checkpoint_payload(raw)
    compressed = BoundaryCheckpoint.decode(payload, hashlib.sha256(payload).hexdigest())
    run = await store.get_run(RUN)
    task = await journal.intent(RUN, "implement")
    scope = service.scope(run, task, capture=lambda wire: compressed)

    async def reader(run_id, actor, **kwargs):
        return await store.get_run(run_id)

    monkeypatch.setattr(service.completions, "_reader", reader)
    current = {}
    store.credential_registry = lambda: current
    monkeypatch.setattr(store, "reject_credentials", MethodType(Store.reject_credentials, store))
    if stage != "capture":
        intent = await scope.admit(descriptor, body)
    current["controlled_token"] = "Fictional source line."
    before = len(journal.records.rows)
    with pytest.raises(ValueError, match="configured credentials"):
        if stage == "capture":
            await scope.admit(descriptor, body)
        elif stage == "consume":
            await scope.response(intent, response)
        elif stage == "exact":
            await service.exact(RUN, intent["idempotency_key"])
        elif stage == "journal":
            await journal.entries(RUN)
        else:
            from firefly_forge.store import prepared_credential_rejection

            with prepared_credential_rejection(store):
                async with store._unit():
                    await journal.entries(RUN)
    assert not any(row.kind == "agent_consume" for row in journal.records.rows)
    assert len(journal.records.rows) == before + (1 if stage == "consume" else 0)


def test_raw_record_bound_and_encoded_custody_bound_are_independent(large_checkpoint):
    import base64

    from firefly_forge.agentic_checkpoint_boundary import MAX_RAW_PAYLOAD, _checkpoint_payload

    with pytest.raises(ValueError, match="8 MiB"):
        _checkpoint_payload(b"x" * (MAX_RAW_PAYLOAD + 1))
    record = large_checkpoint.record.model_dump(mode="json")
    content = base64.b64encode(hashlib.shake_256(b"fictional-incompressible").digest(300000)).decode()
    for message in record["history"]:
        for part in message["parts"]:
            if isinstance(part.get("content"), str):
                part["content"] = content
    record["request_history"] = record["history"]
    raw, _ = reseal_envelope(record)
    assert len(raw) < MAX_RAW_PAYLOAD
    payload = _checkpoint_payload(raw)
    assert len(payload) > MAX_PAYLOAD
    with pytest.raises(ValueError, match="size"):
        BoundaryCheckpoint.decode(payload, hashlib.sha256(payload).hexdigest())


@pytest.mark.parametrize("encoding", ["whitespace", "duplicate-key", "schema-secret", "usage-secret"])
def test_compressed_envelope_canonical_json_and_errors_do_not_expose_record_text(large_checkpoint, encoding):
    envelope = json.loads(large_checkpoint.payload)
    secret = "fictional-credential-must-not-appear-in-errors"
    if encoding == "whitespace":
        payload = json.dumps(envelope, indent=2).encode()
    elif encoding == "duplicate-key":
        payload = large_checkpoint.payload[:-1] + b',"version":2}'
    else:
        import base64
        import zlib

        raw = json.loads(large_checkpoint.canonical_payload)
        if encoding == "usage-secret":
            raw["usage"]["requests"] = secret
        else:
            raw["state"] = {"files_written": ["/" + secret]}
        raw, _ = reseal_envelope(raw)
        envelope.update(
            raw_bytes=len(raw),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            data=base64.b64encode(zlib.compress(raw, 9)).decode(),
        )
        payload, _ = reseal_envelope(envelope)
    with pytest.raises(ValueError) as error:
        BoundaryCheckpoint.decode(payload, hashlib.sha256(payload).hexdigest())
    assert secret not in str(error.value)


@pytest.mark.parametrize("drift", [False, True])
async def test_compressed_journal_checks_current_generation_in_prepared_ambient_unit(
    tmp_path, monkeypatch, large_checkpoint, drift
):
    import copy
    from types import MethodType

    from pyfly.data.transaction import current_unit_of_work
    from test_agentic_checkpoint_storage_host import setup
    from test_bridge_inflight_cancel import RUN

    from firefly_forge.agentic_checkpoint_boundary import _checkpoint_payload
    from firefly_forge.configuration import ProviderConfiguration
    from firefly_forge.store import Store, prepared_credential_rejection

    store, journal, manager, service, _, descriptor, _, body, _ = await setup(tmp_path, monkeypatch)
    record = large_checkpoint.record.model_dump(mode="json")
    record.update(wire_sha256=hashlib.sha256(body).hexdigest(), wire_bytes=len(body))
    raw, _ = reseal_envelope(record)
    payload = _checkpoint_payload(raw)
    checkpoint = BoundaryCheckpoint.decode(payload, hashlib.sha256(payload).hexdigest())
    run, task = await store.get_run(RUN), await journal.intent(RUN, "implement")
    await service.scope(run, task, capture=lambda wire: checkpoint).admit(descriptor, body)
    store.credential_registry = lambda: store.configuration.resolve("bridge")
    monkeypatch.setattr(store, "reject_credentials", MethodType(Store.reject_credentials, store))
    admit = store.admit_credentials
    scans = []

    async def scan(value):
        assert current_unit_of_work() is None
        await admit(value)
        if isinstance(value, str) and "Fictional source line." in value:
            scans.append(len(value))
            if drift:
                store.configuration.update("bridge", ProviderConfiguration(values={"token": "Fictional source line."}))

    monkeypatch.setattr(store, "admit_credentials", scan)
    before = copy.deepcopy([r.data for r in journal.records.rows])
    with prepared_credential_rejection(store):
        async with store._unit():
            if drift:
                with pytest.raises(ValueError, match="Credential sources changed"):
                    await journal.entries(RUN)
            else:
                entries = await journal.entries(RUN)
                stored = next(r for r in entries if r["kind"] == "agent_checkpoint")
                assert stored["payload"]["snapshot"].encode() == checkpoint.payload
                assert "Fictional source line." not in stored["payload"]["snapshot"]
    assert scans and [r.data for r in journal.records.rows] == before


def test_sub192k_raw_history_compresses_before_binding_and_json_escaping_exceed_wal(large_checkpoint):
    from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2, InvocationBinding
    from firefly_forge.agent_checkpoints import ENVELOPE_LIMIT, _size
    from firefly_forge.agentic_checkpoint_boundary import _checkpoint_payload

    record = large_checkpoint.record.model_dump(mode="json")
    for message in record["history"]:
        for part in message["parts"]:
            if isinstance(part.get("content"), str):
                part["content"] = '"\\\n' * 10000
    record["request_history"] = record["history"]
    raw, digest = reseal_envelope(record)
    assert 64 * 1024 < len(raw) < MAX_PAYLOAD
    binding = InvocationBinding(
        invocation_id="1" * 32,
        ordinal=1,
        predecessor=None,
        brief="F" * 99000,
        backend="pyfly",
        frontend="none",
        round=1,
        deadline_ms=1,
        grants=[],
        contract="0" * 64,
        run_cut="0" * 64,
        workspace={"path": "owned", "head": "0" * 40, "metadata": "0" * 64, "source": "0" * 64},
    )
    legacy = CheckpointPayloadV2(
        version=2,
        request_id="fbr1_" + "1" * 32,
        intent_digest="0" * 64,
        checkpoint_digest=digest,
        snapshot=raw.decode(),
        invocation=binding,
    )
    assert _size(legacy.model_dump(mode="json")) + 4096 > ENVELOPE_LIMIT
    encoded = _checkpoint_payload(raw)
    assert json.loads(encoded)["version"] == 2
    compressed = legacy.model_copy(
        update={"snapshot": encoded.decode(), "checkpoint_digest": hashlib.sha256(encoded).hexdigest()}
    )
    assert _size(compressed.model_dump(mode="json")) + 4096 < ENVELOPE_LIMIT
    assert BoundaryCheckpoint.decode(encoded, hashlib.sha256(encoded).hexdigest()).canonical_payload == raw


@pytest.mark.parametrize("source", ["http_response", "bridge_observation"])
async def test_large_actual_engine_sdk_checkpoint_restores_known_or_observed_original(tmp_path, monkeypatch, source):
    from test_agentic_checkpoint_activation_host import activated, controlled_source_snapshot, queue_original

    from firefly_forge import agentic_runtime

    build = agentic_runtime.build_instruction
    monkeypatch.setattr(
        agentic_runtime,
        "build_instruction",
        lambda *a, **kw: build(*a, **kw) + "\n" + "Fictional compressed native instruction.\n" * 5500,
    )
    env = await activated(tmp_path, monkeypatch)
    try:
        posts, intent, retained, body, request = await queue_original(env, monkeypatch, source=source)
        checkpoint = BoundaryCheckpoint.decode(
            retained["payload"]["snapshot"].encode(), retained["payload"]["checkpoint_digest"]
        )
        assert json.loads(checkpoint.payload)["version"] == 2 and len(checkpoint.canonical_payload) > MAX_PAYLOAD

        async def snapshot(*a, **kw):
            return ["service.py"], controlled_source_snapshot("Fictional retained patch"), "c" * 40

        monkeypatch.setattr(env.engine, "_snapshot", snapshot)
        before = (env.workspace / "service.py").stat().st_mtime_ns
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(request.request_id)
        ticket = await env.requests.get(request.request_id)
        assert ticket["status"] == "completed", ticket.get("result")
        assert len(posts) == 2 and (env.workspace / "service.py").stat().st_mtime_ns == before
        assert sum(r.kind == "agent_consume" for r in env.journal.records.rows) == 2
    finally:
        await env.model.client.close()
