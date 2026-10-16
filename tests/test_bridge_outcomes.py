# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline SDK and journal boundary controls; no PostgreSQL or provider execution."""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx2
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from firefly_forge import providers
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal

JOB = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (429, "quota_exhausted", "quota"),
        (503, "execution_paused", "provider_paused"),
        (503, "unavailable", "provider_outcome_unknown"),
        (504, "completion_timeout", "provider_outcome_unknown"),
        (500, "execution_failed", "provider_outcome_unknown"),
        (0, "timeout", "provider_outcome_unknown"),
    ],
)
async def test_real_sdk_bridge_failure_submits_once_and_keeps_structured_outcome(status, code, expected):
    model = providers.BridgeProvider(
        {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
    ).model()
    client = model.client
    await client._client.aclose()
    calls = []

    def respond(request):
        calls.append(request)
        if not status:
            raise httpx2.ReadTimeout("offline timeout", request=request)
        return httpx2.Response(
            status,
            headers={"retry-after": "0.001", "x-bridge-job-id": JOB},
            json={"error": {"code": code, "message": "opaque upstream prose", "job_id": JOB}},
        )

    client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    try:
        with pytest.raises(Exception) as caught:
            await model.request(
                [ModelRequest(parts=[UserPromptPart("Offline fixture")])], None, ModelRequestParameters()
            )
        assert providers.classify_provider_failure(caught.value) == (expected, 0.001 if status else None)
        assert len(calls) == 1
        assert client.max_retries == 0
        if status:
            assert caught.value.observation == {"job_id": JOB, "retry_after": 0.001}
        assert "opaque upstream prose" not in str(caught.value)
    finally:
        await client.close()


def test_other_api_provider_status_classification_is_unchanged():
    assert providers.classify_provider_failure(ModelHTTPError(503, "other", body={"code": "execution_paused"})) == (
        "transient",
        5.0,
    )
    assert providers.classify_provider_failure(
        ModelHTTPError(429, "other", body={"code": "quota_exhausted"}, headers={})
    ) == ("rate_limit", 60.0)


class Rows:
    def __init__(self, rows):
        self.rows = rows

    async def find_for_run(self, run_id):
        return self.rows

    async def find_by_id(self, run_id, **kwargs):
        return self.rows[0]

    async def save(self, row):
        if row not in self.rows:
            self.rows.append(row)


class MemoryStore:
    runtime_roles = None

    def __init__(self):
        self.path = Path("fictional-offline-store.db")
        self.run = SimpleNamespace(id=UUID(int=1), status="running", data={"status": "running"})
        self.run_records = Rows([self.run])
        self.events = []
        self.finished = []

    @asynccontextmanager
    async def _unit(self):
        yield

    async def get_run(self, run_id):
        return {"id": run_id, **self.run.data}

    async def claim(self, run_id):
        return True

    async def update(self, run_id, **values):
        self.run.data.update(values)
        self.run.status = self.run.data["status"]

    async def finish(self, *args, **kwargs):
        self.finished.append(args)

    async def event(self, *args):
        self.events.append(args)


@pytest.mark.parametrize(
    "kind,status", [("provider_paused", "paused"), ("provider_outcome_unknown", "needs_reconciliation")]
)
async def test_real_journal_retains_manual_outcome_and_original_intent(kind, status):
    store = MemoryStore()
    task = SimpleNamespace(task_id="implement", status="running", data={"idempotency_key": "original"})
    journal = Journal(store, tasks=Rows([task]), records=Rows([]))
    entry = await journal.fail(
        UUID(int=1).hex, "original", kind, "Retained Bridge outcome", observation={"job_id": JOB}
    )
    assert entry["idempotency_key"] == "original"
    assert entry["payload"]["automatic"] is False and entry["payload"]["retry_at"] is None
    assert entry["payload"]["provider_observation"] == {"job_id": JOB}
    assert store.run.status == task.status == status
    assert store.run.data["display_status"] == ("provider_paused" if kind == "provider_paused" else status)
    assert task.data["idempotency_key"] == "original"
    assert await journal.fail(UUID(int=1).hex, "original", kind, "different") == entry
    assert Journal.verify_chain([entry])


async def test_unknown_outcome_resume_refuses_before_any_new_dispatch_or_configuration():
    store = MemoryStore()
    store.run.status = "needs_reconciliation"
    store.run.data = {"status": "needs_reconciliation", "pause": {"kind": "provider_outcome_unknown"}}
    with pytest.raises(ValueError, match="Bridge.*reconcil"):
        await Journal(store).resume(UUID(int=1).hex, "claude")
    assert store.run.data == {"status": "needs_reconciliation", "pause": {"kind": "provider_outcome_unknown"}}


async def test_engine_unknown_provider_outcome_is_not_generic_failure_without_journal(tmp_path):
    store = MemoryStore()
    get_run = store.get_run
    calls = 0

    async def fail_once(run_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            error = getattr(providers, "ProviderOutcomeUnknown", providers.ProviderError)
            raise error("Bridge outcome requires reconciliation", observation={"job_id": JOB})
        return await get_run(run_id)

    store.get_run = fail_once
    engine = Engine(store, tmp_path)
    await engine.execute_run(UUID(int=1).hex)
    assert store.run.status == "needs_reconciliation"
    assert store.finished == []
    assert store.run.data["pause"]["kind"] == "provider_outcome_unknown"
    assert store.run.data["pause"]["provider_observation"] == {"job_id": JOB}


@pytest.mark.parametrize(
    "code,status,kind", [("quota_exhausted", 429, "quota"), ("execution_paused", 503, "provider_paused")]
)
async def test_bridge_pause_without_reset_does_not_invent_a_retry_time(code, status, kind):
    model = providers.BridgeProvider(
        {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
    ).model()
    await model.client._client.aclose()
    model.client._client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(status, json={"error": {"code": code, "message": "arbitrary"}})
        )
    )
    try:
        with pytest.raises(providers.ProviderLimitError) as caught:
            await model.request([ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters())
        assert providers.classify_provider_failure(caught.value) == (kind, None)
        assert caught.value.observation == {}
    finally:
        await model.client.close()


@pytest.mark.parametrize("retry", ["nan", "inf", "-1", "86401", "not a time"])
async def test_bridge_observation_does_not_reflect_arbitrary_job_or_unbounded_retry(retry):
    model = providers.BridgeProvider(
        {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
    ).model()
    await model.client._client.aclose()
    model.client._client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                503,
                headers={"retry-after": retry},
                json={"error": {"code": "execution_paused", "job_id": "secret-value"}},
            )
        )
    )
    try:
        with pytest.raises(providers.ProviderLimitError) as caught:
            await model.request([ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters())
        assert caught.value.observation == {}
        assert "secret-value" not in str(caught.value)
    finally:
        await model.client.close()


async def test_bridge_stream_error_is_not_a_transient_retry():
    model = providers.BridgeProvider(
        {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
    ).model()
    await model.client._client.aclose()
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(503, json={"error": {"code": "execution_paused", "message": "opaque"}})

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    try:
        with pytest.raises(providers.ProviderLimitError) as caught:
            async with model.request_stream(
                [ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters()
            ):
                pytest.fail("Must not receive a stream")
        assert providers.classify_provider_failure(caught.value) == ("provider_paused", None)
        assert len(calls) == 1
    finally:
        await model.client.close()


async def test_prose_alone_does_not_classify_a_retained_bridge_pause():
    model = providers.BridgeProvider(
        {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
    ).model()
    await model.client._client.aclose()
    model.client._client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                503,
                json={"error": {"code": "different", "message": "quota_exhausted context_exhausted execution_paused"}},
            )
        )
    )
    try:
        with pytest.raises(Exception) as caught:
            await model.request([ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters())
        assert type(caught.value).__name__ == "ProviderOutcomeUnknown"
    finally:
        await model.client.close()


async def test_old_quota_error_payload_has_no_new_observation_fields():
    store = MemoryStore()
    task = SimpleNamespace(task_id="implement", status="running", data={"idempotency_key": "original"})
    journal = Journal(store, tasks=Rows([task]), records=Rows([]))
    entry = await journal.fail(UUID(int=1).hex, "original", "quota", "Original quota")
    assert set(entry["payload"]) == {"kind", "message", "automatic", "retry_at", "retry_count"}
    assert entry["payload"]["automatic"] is False


async def test_engine_unknown_failure_uses_same_active_journal_intent(tmp_path, monkeypatch):
    store = MemoryStore()
    store.run.data.update(agent="bridge", repair_budget=0, resume_existing=True, repository_id=UUID(int=2).hex)
    task = SimpleNamespace(task_id="implement", status="running", data={"idempotency_key": "original"})
    journal = Journal(store, tasks=Rows([task]), records=Rows([]))
    engine = Engine(store, tmp_path, journal=journal)
    workspace = tmp_path / UUID(int=1).hex
    workspace.mkdir()
    (workspace / "retained.txt").write_text("Retained partial work")

    async def repository(repository_id):
        return {"url": "fictional"}

    async def intent(*args):
        return {"idempotency_key": "original", "disposition": "completed"}

    async def unknown(*args):
        raise providers.ProviderOutcomeUnknown("Bridge result is unknown", observation={"job_id": JOB})

    store.repository = repository
    monkeypatch.setattr(engine, "_begin_task", intent)
    monkeypatch.setattr(engine, "_inspect_workspace", unknown)
    await engine.execute_run(UUID(int=1).hex)
    assert task.status == store.run.status == "needs_reconciliation"
    assert task.data["idempotency_key"] == "original"
    assert (workspace / "retained.txt").read_text() == "Retained partial work"
    assert store.finished == []
    assert len(journal.records.rows) == 1
    assert journal.records.rows[0].data["payload"]["automatic"] is False


@pytest.mark.parametrize(
    "observation",
    [
        {"job_id": "not-a-job"},
        {"retry_after": True},
        {"retry_after": float("inf")},
        {"retry_after": -1},
        {"raw_body": "private"},
    ],
)
async def test_journal_rejects_unbounded_provider_observation_before_append(observation):
    store = MemoryStore()
    task = SimpleNamespace(task_id="implement", status="running", data={"idempotency_key": "original"})
    records = Rows([])
    journal = Journal(store, tasks=Rows([task]), records=records)
    with pytest.raises(ValueError):
        await journal.fail(UUID(int=1).hex, "original", "provider_paused", "Retained", observation=observation)
    assert records.rows == []
    assert task.status == store.run.status == "running"


async def test_stream_error_after_http_success_remains_unknown_without_resubmission():
    model = providers.BridgeProvider(
        {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
    ).model()
    await model.client._client.aclose()
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"error":{"message":"Retained job unavailable","code":"execution_paused"}}\n\n',
        )

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    try:
        with pytest.raises(Exception) as caught:
            async with model.request_stream(
                [ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters()
            ) as stream:
                async for _ in stream:
                    pass
        assert type(caught.value).__name__ == "ProviderOutcomeUnknown"
        assert len(calls) == 1
    finally:
        await model.client.close()


@pytest.mark.parametrize("kind", ["provider_outcome_unknown", "provider_paused", "quota"])
async def test_assurance_keeps_bridge_outcome_on_the_original_review_intent(tmp_path, monkeypatch, kind):
    import zipfile

    import firefly_forge.assurance as module

    store = MemoryStore()
    run_id = UUID(int=1).hex
    store.run.data.update(
        id=run_id,
        source_tree="frozen",
        assurance={"review_agent": "bridge"},
        brief="Review",
        backend="pyfly",
        frontend="none",
        final_verification=[],
    )
    task = SimpleNamespace(task_id="review", status="running", data={"idempotency_key": "review-original"})
    journal = Journal(store, tasks=Rows([task]), records=Rows([]))
    calls = []

    async def plan(*args):
        return {"tasks": [{"id": "review", "attempt": 1}]}

    async def intent(*args, **kwargs):
        return {"disposition": "execute", "idempotency_key": "review-original"}

    async def checked(argv, cwd):
        for arg in argv:
            if arg.startswith("--output="):
                with zipfile.ZipFile(arg.split("=", 1)[1], "w") as archive:
                    archive.writestr("product.py", "value = 1\n")
        return "frozen"

    class Reviewer:
        async def execute(self, *args):
            calls.append(args)
            if kind == "provider_outcome_unknown":
                raise providers.ProviderOutcomeUnknown("Unknown Bridge result", observation={"job_id": JOB})
            raise providers.ProviderLimitError("Retained Bridge pause", kind=kind, observation={"job_id": JOB})

    async def emit(*args):
        pass

    monkeypatch.setattr(journal, "ensure_plan", plan)
    monkeypatch.setattr(journal, "intent", intent)
    monkeypatch.setattr(module, "checked", checked)
    workspace = tmp_path / "original"
    workspace.mkdir()
    (workspace / "product.py").write_text("value = 1\n")
    assurance = module.Assurance(store, tmp_path / "reviews", journal, lambda _: Reviewer())
    expected = providers.ProviderOutcomeUnknown if kind == "provider_outcome_unknown" else module.ReviewPending
    with pytest.raises(expected):
        await assurance.review(await store.get_run(run_id), workspace, emit)
    assert len(calls) == 1
    assert len(journal.records.rows) == 1
    entry = journal.records.rows[0].data
    assert entry["idempotency_key"] == "review-original"
    assert entry["payload"]["kind"] == kind
    assert entry["payload"]["provider_observation"] == {"job_id": JOB}
    assert entry["payload"]["automatic"] is False
    assert task.status == ("needs_reconciliation" if kind == "provider_outcome_unknown" else "paused")


@pytest.mark.parametrize("stream", [False, True], ids=["invalid-response-json", "empty-response-stream"])
async def test_bridge_unusable_success_response_retains_unknown_outcome(stream):
    model = providers.BridgeProvider(
        {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
    ).model()
    await model.client._client.aclose()
    calls = []

    def respond(request):
        calls.append(request)
        if stream:
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text="data: [DONE]\n\n")
        return httpx2.Response(200, json={"id": "fictional", "choices": "unusable"})

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    try:
        with pytest.raises(providers.ProviderOutcomeUnknown) as caught:
            if stream:
                async with model.request_stream(
                    [ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters()
                ):
                    pytest.fail("An empty stream cannot provide a completion")
            else:
                await model.request([ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters())
        assert providers.classify_provider_failure(caught.value) == ("provider_outcome_unknown", None)
        assert caught.value.observation == {}
        assert len(calls) == 1
        assert model.client.max_retries == 0
    finally:
        await model.client.close()


@pytest.mark.parametrize("stream", [False, True])
async def test_bridge_pre_submit_validation_does_not_claim_unknown_outcome(stream):
    from pydantic_ai.exceptions import UserError
    from pydantic_ai.native_tools import WebSearchTool

    model = providers.BridgeProvider(
        {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
    ).model()
    await model.client._client.aclose()
    calls = []

    def respond(request):
        calls.append(request)
        pytest.fail("Invalid input must not submit a request")

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    parameters = ModelRequestParameters(native_tools=[WebSearchTool()])
    try:
        with pytest.raises(UserError, match="WebSearchTool is not supported") as caught:
            if stream:
                async with model.request_stream([ModelRequest(parts=[UserPromptPart("Offline")])], None, parameters):
                    pytest.fail("Invalid native tools cannot produce a stream")
            else:
                await model.request([ModelRequest(parts=[UserPromptPart("Offline")])], None, parameters)
        assert providers.classify_provider_failure(caught.value) is None
        assert calls == []
    finally:
        await model.client.close()


@pytest.mark.parametrize("stream", [False, True], ids=["invalid-response-json", "empty-response-stream"])
async def test_unusable_bridge_response_reaches_engine_original_journal_intent(tmp_path, monkeypatch, stream):
    store = MemoryStore()
    store.run.data.update(agent="bridge", repair_budget=0, resume_existing=True, repository_id=UUID(int=2).hex)
    task = SimpleNamespace(task_id="implement", status="running", data={"idempotency_key": "original"})
    journal = Journal(store, tasks=Rows([task]), records=Rows([]))
    engine = Engine(store, tmp_path, journal=journal)
    workspace = tmp_path / UUID(int=1).hex
    workspace.mkdir()
    (workspace / "retained.txt").write_text("Retained partial work")
    model = providers.BridgeProvider(
        {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
    ).model()
    await model.client._client.aclose()
    calls = []

    def respond(request):
        calls.append(request)
        if stream:
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text="data: [DONE]\n\n")
        return httpx2.Response(200, json={"id": "fictional", "choices": "unusable"})

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))

    async def repository(repository_id):
        return {"url": "fictional"}

    async def intent(*args):
        return {"idempotency_key": "original", "disposition": "completed"}

    async def inspect(*args):
        if stream:
            async with model.request_stream(
                [ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters()
            ):
                pytest.fail("An empty stream cannot provide a completion")
        else:
            await model.request([ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters())

    store.repository = repository
    monkeypatch.setattr(engine, "_begin_task", intent)
    monkeypatch.setattr(engine, "_inspect_workspace", inspect)
    try:
        await engine.execute_run(UUID(int=1).hex)
        assert store.finished == []
        assert task.status == store.run.status == "needs_reconciliation"
        assert task.data["idempotency_key"] == "original"
        assert (workspace / "retained.txt").read_text() == "Retained partial work"
        assert len(journal.records.rows) == len(calls) == 1
        error = journal.records.rows[0].data
        assert error["idempotency_key"] == "original"
        assert error["payload"]["kind"] == "provider_outcome_unknown"
        assert error["payload"]["automatic"] is False and error["payload"]["retry_at"] is None
        with pytest.raises(ValueError, match="Bridge.*reconcil"):
            await journal.resume(UUID(int=1).hex)
        assert len(calls) == 1
    finally:
        await model.client.close()


async def test_bridge_partial_stream_decoding_failure_is_unknown_not_consumer_failure():
    model = providers.BridgeProvider(
        {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
    ).model()
    await model.client._client.aclose()
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"id":"fictional","model":"bridge/default","created":1,'
            '"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
            'data: {"id":"fictional","model":"bridge/default","created":1,"choices":"unusable"}\n\n'
            "data: [DONE]\n\n",
        )

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    try:
        with pytest.raises(providers.ProviderOutcomeUnknown):
            async with model.request_stream(
                [ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters()
            ) as response:
                async for _ in response:
                    pass
        assert len(calls) == 1
        consumer_error = ValueError("Local consumer did not accept the response")
        with pytest.raises(ValueError) as caught:
            async with model.request_stream(
                [ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters()
            ):
                raise consumer_error
        assert caught.value is consumer_error
        assert len(calls) == 2
    finally:
        await model.client.close()


def retry_boundary_store(status, kind):
    store = MemoryStore()
    store.run.status = status
    store.run.data = {
        "status": status,
        "repository_id": UUID(int=2).hex,
        "brief": "Offline Bridge outcome boundary",
        "pause": {"kind": kind, "automatic": False, "retry_at": None},
    }
    created = []

    async def create_run(request):
        created.append(request)
        return {"id": UUID(int=3).hex, "status": "queued"}

    store.create_run = create_run
    return store, created


@pytest.mark.parametrize("kind", ["provider_outcome_unknown", "quota"])
async def test_cancelled_bridge_outcome_does_not_escape_origin_retry_hold(tmp_path, kind):
    from types import MethodType

    from firefly_forge.controllers import RunService
    from firefly_forge.store import Store

    store, created = retry_boundary_store(
        "needs_reconciliation" if kind == "provider_outcome_unknown" else "paused", kind
    )
    store.finish = MethodType(Store.finish, store)
    engine = Engine(store, tmp_path)
    await engine.cancel_effect(UUID(int=1).hex)
    assert store.run.status == "cancelled"
    assert store.run.data["pause"]["kind"] == kind
    if kind == "provider_outcome_unknown":
        with pytest.raises(ValueError, match="Bridge.*reconcil"):
            await RunService(store, engine).retry(UUID(int=1).hex)
        assert created == []
    else:
        result = await RunService(store, engine).retry(UUID(int=1).hex)
        assert result["status"] == "queued" and len(created) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
async def test_terminal_unknown_bridge_outcome_cannot_create_retry_run(tmp_path, status):
    from firefly_forge.controllers import RunService

    store, created = retry_boundary_store(status, "provider_outcome_unknown")
    before = dict(store.run.data)
    with pytest.raises(ValueError, match="Bridge.*reconcil"):
        await RunService(store, Engine(store, tmp_path)).retry(UUID(int=1).hex)
    assert created == [] and store.run.data == before


@pytest.mark.parametrize("status", ["failed", "interrupted"])
async def test_known_terminal_failure_still_creates_explicit_retry_run(tmp_path, status):
    from firefly_forge.controllers import RunService

    store, created = retry_boundary_store(status, "verification")
    result = await RunService(store, Engine(store, tmp_path)).retry(UUID(int=1).hex)
    assert result["status"] == "queued" and len(created) == 1
    assert created[0].repository_id == UUID(int=2).hex
    assert created[0].brief == store.run.data["brief"]
