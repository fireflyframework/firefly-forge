# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit new work exposes no hidden model read cap or automatic POST retry."""

import asyncio

import httpx2
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from firefly_forge.providers import APIProvider
from firefly_forge.work_duration_models import WorkDurationPolicyV2


@pytest.mark.parametrize(
    "family",
    [
        "openai-chat:gpt-fixture",
        "openai-responses:gpt-fixture",
        "anthropic:claude-fixture",
        "google-gla:gemini-fixture",
    ],
)
async def test_explicit_new_policy_keeps_finite_transport_bounds_but_no_model_read_cap_or_post_retry(family):
    from firefly_forge.model_operation_client import ModelOperationClient

    requests = []

    async def transport(request):
        requests.append(request)
        return httpx2.Response(
            500,
            json={
                "error": {
                    "message": "Controlled unknown model outcome",
                    "type": "server_error",
                    "code": 500,
                    "status": "INTERNAL",
                }
            },
        )

    client = ModelOperationClient(transport=httpx2.MockTransport(transport))
    provider = APIProvider({"FORGE_AGENTIC_MODEL": family, "FORGE_AGENTIC_API_KEY": "fictional-key"})
    provider.configure_work_duration(WorkDurationPolicyV2())
    try:
        model = provider.model(http_client=client)
        with pytest.raises(ModelHTTPError):
            await model.request(
                [ModelRequest(parts=[UserPromptPart("Controlled task")])], None, ModelRequestParameters()
            )
        assert len(requests) == 1, "Unknown new-policy model POST cannot retry inside the SDK"
        timeout = requests[0].extensions["timeout"]
        assert timeout == {"connect": 10, "read": None, "write": 30, "pool": 10}
    finally:
        await client.aclose()


async def test_explicit_scalar_request_timeout_cannot_reintroduce_google_style_whole_operation_cap():
    from firefly_forge.model_operation_client import ModelOperationClient

    observed = []

    async def transport(request):
        observed.append(request.extensions["timeout"])
        return httpx2.Response(200, json={"ok": True})

    async with ModelOperationClient(transport=httpx2.MockTransport(transport)) as client:
        await client.post("https://example.invalid/controlled", json={"task": "fixture"}, timeout=300)
    assert observed == [{"connect": 10, "read": None, "write": 30, "pool": 10}]


async def test_owned_model_scope_closes_only_this_child_client_and_preserves_other_profile():
    first = APIProvider({"FORGE_AGENTIC_MODEL": "openai-chat:gpt-fixture", "FORGE_AGENTIC_API_KEY": "fictional-key"})
    second = APIProvider({"FORGE_AGENTIC_MODEL": "openai-chat:gpt-other", "FORGE_AGENTIC_API_KEY": "fictional-key"})
    first.configure_work_duration(WorkDurationPolicyV2())
    second.configure_work_duration(WorkDurationPolicyV2(timeout_seconds=864000))
    assert hasattr(first, "model_scope"), "Missing public owned client scope for a child invocation"
    async with second.model_scope() as sibling:
        async with first.model_scope() as child:
            assert child.client is not sibling.client
            assert child.client.is_closed() is False and sibling.client.is_closed() is False
        assert child.client.is_closed() is True and sibling.client.is_closed() is False
    assert sibling.client.is_closed() is True


async def test_legacy_default_model_keeps_its_existing_sdk_retry_and_read_timeout():
    provider = APIProvider({"FORGE_AGENTIC_MODEL": "openai-chat:gpt-fixture", "FORGE_AGENTIC_API_KEY": "fictional-key"})
    model = provider.model()
    try:
        assert model.client.max_retries == 2 and model.client.timeout.read == 600
    finally:
        await model.client.close()


@pytest.mark.parametrize("family", ["openai-chat:gpt-fixture", "anthropic:claude-fixture", "google-gla:gemini-fixture"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_native_provider_execute_closes_owned_child_clients_after_each_terminal_outcome(
    tmp_path, monkeypatch, family, outcome
):
    from firefly_forge import agentic_runtime, model_operation_client

    clients = []
    original = model_operation_client.ModelOperationClient

    class ObservedClient(original):
        def __init__(self):
            super().__init__()
            clients.append(self)

    monkeypatch.setattr(model_operation_client, "ModelOperationClient", ObservedClient)
    entered = asyncio.Event()

    async def controlled(*args, **kwargs):
        entered.set()
        if outcome == "cancelled":
            await asyncio.Event().wait()
        if outcome == "failed":
            raise ValueError("Controlled model outcome")
        return "Observed child analysis", {"requests": 1}

    monkeypatch.setattr(agentic_runtime, "execute_agentic", controlled)
    provider = APIProvider({"FORGE_AGENTIC_MODEL": family, "FORGE_AGENTIC_API_KEY": "fictional-key"})
    provider.configure_work_duration(WorkDurationPolicyV2())

    async def emit(*args):
        pass

    task = asyncio.create_task(provider.execute(tmp_path, "Controlled child goal", "none", "none", emit))
    await entered.wait()
    if outcome == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "failed":
        with pytest.raises(ValueError, match="Controlled model outcome"):
            await task
    else:
        assert (await task).summary == "Observed child analysis"
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize(
    "family",
    [
        "openai-chat:gpt-fixture",
        "openai-responses:gpt-fixture",
        "anthropic:claude-fixture",
        "google-gla:gemini-fixture",
    ],
)
async def test_direct_public_model_context_owns_and_closes_its_supplied_operation_client(family):
    from firefly_forge.model_operation_client import ModelOperationClient

    client = ModelOperationClient()
    provider = APIProvider({"FORGE_AGENTIC_MODEL": family, "FORGE_AGENTIC_API_KEY": "fictional-key"})
    provider.configure_work_duration(WorkDurationPolicyV2())
    try:
        model = provider.model(http_client=client)
        async with model:
            async with model:
                assert not client.is_closed
            assert not client.is_closed
        assert client.is_closed
    finally:
        await client.aclose()


async def test_repeated_cancellation_waits_for_this_invocations_public_client_cleanup(monkeypatch):
    from firefly_forge.model_operation_client import ModelOperationClient

    client = ModelOperationClient()
    closing, release, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = client.aclose

    async def delayed_close():
        closing.set()
        await release.wait()
        await original()

    monkeypatch.setattr(client, "aclose", delayed_close)
    provider = APIProvider({"FORGE_AGENTIC_MODEL": "openai-chat:gpt-fixture", "FORGE_AGENTIC_API_KEY": "fictional-key"})
    provider.configure_work_duration(WorkDurationPolicyV2())
    model = provider.model(http_client=client)

    async def work():
        async with model:
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    try:
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "Repeated cancellation must not publish before owned HTTP cleanup"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await original()
    assert client.is_closed


def test_model_transport_and_lifecycle_module_changes_both_current_authority_contracts(monkeypatch):
    from pathlib import Path

    from test_source_edit_v14_transition_host import declared_names

    from firefly_forge import source_edit_qualification as qualification
    from firefly_forge.agentic_checkpoint_invocation import runtime_contract
    from firefly_forge.source_budget_allowances import admission_contract_digest

    root = Path(qualification.__file__).parent
    name = "model_operation_client.py"
    runtime = declared_names(root / "agentic_checkpoint_invocation.py", "runtime_contract", 21)
    admission = declared_names(root / "source_budget_allowances.py", "admission_contract_digest", 35)
    assert runtime.count(name) == admission.count(name) == 1
    before_runtime, before_admission = runtime_contract(), admission_contract_digest()
    read = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes", lambda path: read(path) + b"\nchanged-owned-model\n" if path.name == name else read(path)
    )
    assert runtime_contract() != before_runtime and admission_contract_digest() != before_admission


@pytest.mark.parametrize(
    "family",
    [
        "openai-chat:gpt-fixture",
        "openai-responses:gpt-fixture",
        "anthropic:claude-fixture",
        "google-gla:gemini-fixture",
    ],
)
async def test_public_sdk_retains_one_healthy_response_and_closes_direct_model_context(family):
    from firefly_forge.model_operation_client import ModelOperationClient

    calls = []
    bodies = {
        "openai-chat": {
            "id": "chat-fixture",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-fixture",
            "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Controlled result"}}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
        "openai-responses": {
            "id": "resp-fixture",
            "object": "response",
            "created_at": 1,
            "model": "gpt-fixture",
            "status": "completed",
            "output": [
                {
                    "id": "msg-fixture",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Controlled result", "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        },
        "anthropic": {
            "id": "msg-fixture",
            "type": "message",
            "role": "assistant",
            "model": "claude-fixture",
            "content": [{"type": "text", "text": "Controlled result"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        "google-gla": {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "Controlled result"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
            "modelVersion": "gemini-fixture",
        },
    }

    async def transport(request):
        calls.append(request)
        return httpx2.Response(200, json=bodies[family.split(":")[0]])

    client = ModelOperationClient(transport=httpx2.MockTransport(transport))
    provider = APIProvider({"FORGE_AGENTIC_MODEL": family, "FORGE_AGENTIC_API_KEY": "fictional-key"})
    provider.configure_work_duration(WorkDurationPolicyV2())
    async with provider.model(http_client=client) as model:
        response = await model.request(
            [ModelRequest(parts=[UserPromptPart("Controlled task")])], None, ModelRequestParameters()
        )
        assert response.parts[0].content == "Controlled result"
        assert response.usage.input_tokens == response.usage.output_tokens == 1
    assert client.is_closed and len(calls) == 1 and calls[0].extensions["timeout"]["read"] is None


async def test_native_agent_keeps_original_chosen_work_cap_while_http_reads_remain_uncapped(tmp_path, monkeypatch):
    from firefly_forge import model_operation_client

    clients, posts = [], []
    drained = asyncio.Event()
    original = model_operation_client.ModelOperationClient

    async def transport(request):
        posts.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    class ControlledClient(original):
        def __init__(self):
            super().__init__(transport=httpx2.MockTransport(transport))
            clients.append(self)

    monkeypatch.setattr(model_operation_client, "ModelOperationClient", ControlledClient)
    provider = APIProvider({"FORGE_AGENTIC_MODEL": "openai-chat:gpt-fixture", "FORGE_AGENTIC_API_KEY": "fictional-key"})
    provider.configure_work_duration(WorkDurationPolicyV2(timeout_seconds=1))

    async def emit(*args):
        pass

    with pytest.raises(TimeoutError):
        await provider.execute(tmp_path, "Controlled task", "pyfly", "none", emit)
    assert len(posts) == 1 and drained.is_set()
    assert posts[0].extensions["timeout"]["read"] is None
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize("google_style", [False, True])
async def test_stalled_vendor_cleanup_is_finite_and_always_attempts_owned_http_close(monkeypatch, google_style):
    from types import SimpleNamespace

    from firefly_forge import model_operation_client as module

    monkeypatch.setattr(module, "MODEL_CLEANUP_TIMEOUT_SECONDS", 0.02, raising=False)
    http_client = module.ModelOperationClient()
    attempted = asyncio.Event()

    async def stalled():
        attempted.set()
        await asyncio.sleep(0.12)

    vendor = (
        SimpleNamespace(aio=SimpleNamespace(aclose=stalled), close=lambda: None)
        if google_style
        else SimpleNamespace(close=stalled)
    )

    class PublicProvider:
        def __init__(self, client):
            self.client = client

    provider = module.owned_model_provider(PublicProvider, http_client, client=vendor)
    with pytest.raises(RuntimeError, match="cleanup"):
        async with asyncio.timeout(1):
            async with provider:
                pass
    assert attempted.is_set() and http_client.is_closed


async def test_failed_sdk_construction_closes_undispatched_client_without_global_cache(tmp_path, monkeypatch):
    from firefly_forge import model_operation_client as module

    clients = []
    original = module.ModelOperationClient

    class ObservedClient(original):
        def __init__(self):
            super().__init__()
            clients.append(self)

    def refused(*args, **kwargs):
        raise ValueError("Controlled constructor refusal")

    monkeypatch.setattr(module, "ModelOperationClient", ObservedClient)
    monkeypatch.setattr(module, "owned_model_provider", refused)
    provider = APIProvider({"FORGE_AGENTIC_MODEL": "openai-chat:gpt-fixture", "FORGE_AGENTIC_API_KEY": "fictional-key"})
    provider.configure_work_duration(WorkDurationPolicyV2())
    with pytest.raises(ValueError, match="constructor refusal"):
        provider.model()
    assert len(clients) == 1 and clients[0].construction_cleanup is not None
    await clients[0].construction_cleanup
    assert clients[0].is_closed and clients[0].construction_refused


async def test_one_operation_http_client_cannot_be_reowned_by_a_second_model():
    from firefly_forge.model_operation_client import ModelOperationClient

    client = ModelOperationClient()
    provider = APIProvider({"FORGE_AGENTIC_MODEL": "openai-chat:gpt-fixture", "FORGE_AGENTIC_API_KEY": "fictional-key"})
    provider.configure_work_duration(WorkDurationPolicyV2())
    model = provider.model(http_client=client)
    try:
        with pytest.raises(ValueError, match="owned"):
            provider.model(http_client=client)
        assert not client.is_closed
    finally:
        async with model:
            pass
    assert client.is_closed


def test_synchronous_failed_construction_closes_undispatched_client_without_running_loop(monkeypatch):
    from firefly_forge import model_operation_client as module

    clients = []
    original = module.ModelOperationClient

    class ObservedClient(original):
        def __init__(self):
            super().__init__()
            clients.append(self)

    def refused(*args, **kwargs):
        raise ValueError("Controlled synchronous constructor refusal")

    monkeypatch.setattr(module, "ModelOperationClient", ObservedClient)
    monkeypatch.setattr(module, "owned_model_provider", refused)
    provider = APIProvider({"FORGE_AGENTIC_MODEL": "openai-chat:gpt-fixture", "FORGE_AGENTIC_API_KEY": "fictional-key"})
    provider.configure_work_duration(WorkDurationPolicyV2())
    with pytest.raises(ValueError, match="synchronous constructor refusal"):
        provider.model()
    assert len(clients) == 1 and clients[0].is_closed and clients[0].construction_refused
