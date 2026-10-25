# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""High-entropy native SDK histories retain exact semantics with shared storage."""

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
from test_agentic_checkpoint_codec import large_checkpoint as large_checkpoint
from test_agentic_checkpoint_codec import reseal_envelope
from test_bridge_completion_native import mock_model_requests

from firefly_forge.agentic_checkpoint_boundary import (
    MAX_PAYLOAD,
    AgenticBoundary,
    BoundaryCheckpoint,
    OriginalCompletion,
)
from firefly_forge.providers import BridgeProvider


async def test_actual_sdk_sixth_high_entropy_frame_shares_history_and_restores_exact_wire(monkeypatch):
    reads, posts, captures, consumed = [], [], [], []
    import base64
    import random

    entropy = random.Random(251025)
    texts = [base64.b64encode(entropy.randbytes(16500)).decode() for _ in range(5)]

    async def read_file(path: str) -> str:
        reads.append(path)
        return "Fictional lock-like integrity data.\n" + texts[int(path.removesuffix(".py")) - 1]

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

    boundary = AgenticBoundary(lambda: {"files_written": ["retained.lock"]})
    model = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}).model()

    def transport(request):
        posts.append(request.content)
        if len(posts) == 6:
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
                "Fictional product brief. " * 900, usage_limits=UsageLimits(request_limit=7, tool_calls_limit=5)
            )
        assert reads == [str(i) + ".py" for i in range(1, 6)] and len(posts) == 6
        checkpoint = BoundaryCheckpoint.decode(captures[-1].payload, captures[-1].digest)
        raw = json.dumps(
            checkpoint.record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        assert len(raw) > MAX_PAYLOAD and len(checkpoint.payload) <= MAX_PAYLOAD
        assert json.loads(checkpoint.payload)["version"] == 3
        assert checkpoint.usage.requests == checkpoint.usage.tool_calls == 5
        assert checkpoint.state == {"files_written": ["retained.lock"]}
        assert checkpoint.record.wire_bytes == len(posts[-1])
        assert checkpoint.wire_sha256 == hashlib.sha256(posts[-1]).hexdigest()
        restored = AgenticBoundary(lambda: {"files_written": ["retained.lock"]}, restore=checkpoint)
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
            assert result.usage.requests == 6 and result.usage.tool_calls == 5
            assert reads == [str(i) + ".py" for i in range(1, 6)] and len(posts) == 6
            assert consumed == [(checkpoint.digest, hashlib.sha256(original).hexdigest())]
            with pytest.raises(ValueError, match="consum"):
                await replay.expose(posts[-1])
        finally:
            await restored_model.client.close()
    finally:
        await model.client.close()


@pytest.fixture
def shared_checkpoint(large_checkpoint):
    from firefly_forge.agentic_checkpoint_boundary import _checkpoint_payload

    raw = large_checkpoint.canonical_payload
    payload = _checkpoint_payload(raw)
    checkpoint = BoundaryCheckpoint.decode(payload, hashlib.sha256(payload).hexdigest())
    assert json.loads(payload)["version"] == 3
    assert checkpoint.canonical_payload == raw
    return checkpoint


@pytest.mark.parametrize(
    "change",
    [
        "packed-bool",
        "packed-too-large",
        "packed-short",
        "raw-bool",
        "raw-too-large",
        "wrong-raw-hash",
        "wrong-shared-reference",
        "mirror-present",
        "missing-history",
        "extra-packed-field",
        "packed-noncanonical",
        "packed-duplicate-key",
        "raw-expanded-short",
        "data-trailing",
        "version-bool",
        "codec-mismatch",
        "envelope-extra",
        "packed-bomb",
        "history-dict",
    ],
)
def test_v3_shared_history_refuses_corruption_without_exposing_record(shared_checkpoint, change):
    import base64
    import zlib

    value = json.loads(shared_checkpoint.payload)
    packed = zlib.decompress(base64.b64decode(value["data"]))
    body = json.loads(packed)
    secret = "fictional-secret-must-not-appear-in-refusal"
    if change == "packed-bool":
        value["packed_bytes"] = True
    elif change == "packed-too-large":
        value["packed_bytes"] = 8 * 1024**2 + 1
    elif change == "packed-short":
        value["packed_bytes"] -= 1
    elif change == "raw-bool":
        value["raw_bytes"] = True
    elif change == "raw-too-large":
        value["raw_bytes"] = 8 * 1024**2 + 1
    elif change == "wrong-raw-hash":
        value["raw_sha256"] = "0" * 64
    elif change == "raw-expanded-short":
        value["raw_bytes"] -= 1
    elif change == "data-trailing":
        value["data"] = base64.b64encode(base64.b64decode(value["data"]) + b"trailing").decode()
    elif change == "version-bool":
        value["version"] = True
    elif change == "codec-mismatch":
        value["codec"] = "zlib-base64"
    elif change == "envelope-extra":
        value["extra"] = secret
    else:
        if change == "wrong-shared-reference":
            body["request_history"] = secret
        elif change == "mirror-present":
            body["request_history"] = body["history"]
        elif change == "missing-history":
            del body["history"]
        elif change == "extra-packed-field":
            body[secret] = "extra"
        elif change == "history-dict":
            body["history"] = {"unexpected": secret}
        packed = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if change == "packed-noncanonical":
            packed += b"\n"
        elif change == "packed-duplicate-key":
            packed = packed[:-1] + b',"request_history":"history"}'
        elif change == "packed-bomb":
            packed = b"x" * (8 * 1024**2 + 1)
        value["packed_bytes"] = min(len(packed), 8 * 1024**2)
        value["data"] = base64.b64encode(zlib.compress(packed, 9)).decode()
    with pytest.raises(ValueError) as error:
        BoundaryCheckpoint.decode(*reseal_envelope(value))
    assert secret not in str(error.value)


def test_v3_alternate_stream_preserves_exact_encoded_identity_and_full_record(shared_checkpoint):
    import base64
    import zlib

    value = json.loads(shared_checkpoint.payload)
    packed = zlib.decompress(base64.b64decode(value["data"]))
    value["data"] = base64.b64encode(zlib.compress(packed, 1)).decode()
    payload, digest = reseal_envelope(value)
    restored = BoundaryCheckpoint.decode(payload, digest)
    assert restored.payload == payload and restored.digest == digest
    assert restored.canonical_payload == shared_checkpoint.canonical_payload
    assert restored.resume_kwargs() == shared_checkpoint.resume_kwargs()


def test_encoded_size_and_digest_have_distinct_safe_refusals(shared_checkpoint):
    with pytest.raises(ValueError, match="encoded size"):
        BoundaryCheckpoint.decode(b"x" * (MAX_PAYLOAD + 1), "0" * 64)
    with pytest.raises(ValueError, match="encoded digest"):
        BoundaryCheckpoint.decode(shared_checkpoint.payload, "0" * 64)
