# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Engine/Agentic/SDK and Journal callbacks with explicit in-memory ACL substitutes."""

import copy
import hashlib
import json

import httpx2
import pytest
from test_bridge_completion_callbacks import CAPABILITY, RESULT
from test_bridge_inflight_cancel import RUN, MemoryStore, Rows

from firefly_forge import engine as engine_module
from firefly_forge.bridge_completions import INTENT, BridgeCompletions
from firefly_forge.configuration import ConfigurationStore, ProviderConfiguration
from firefly_forge.controllers import RunService
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.providers import BridgeProvider


class BoundedRows(Rows):
    async def bridge_budget(self, run_id):
        intents = [row for row in self.rows if row.kind == INTENT]
        return {
            "rows": len(self.rows),
            "bytes": sum(len(json.dumps(row.data).encode()) for row in self.rows),
            "responses": len([row for row in self.rows if row.kind == "bridge_request_response"]),
            "intents": len(intents),
            "max_intent_bytes": max((len(json.dumps(row.data).encode()) for row in intents), default=0),
        }

    async def bridge_record(self, run_id, key, kind):
        return next(
            (row for row in self.rows if row.run_id == run_id and row.idempotency_key == key and row.kind == kind), None
        )

    async def bridge_intents(self, run_id, after, limit):
        return [
            {
                "data": copy.deepcopy(row.data),
                "run_id": row.run_id.hex,
                "sequence": row.sequence,
                "task_id": row.task_id,
                "idempotency_key": row.idempotency_key,
                "kind": row.kind,
                "digest": row.digest,
            }
            for row in self.rows
            if row.run_id == run_id and row.kind == INTENT and row.sequence > after
        ][:limit]


async def fixture(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "host-only")
    store.configuration = ConfigurationStore(tmp_path / "private")
    store.configuration.update(
        "bridge",
        ProviderConfiguration(
            values={"endpoint": "http://localhost:19191", "token": "fictional-token", "completion_recovery": "required"}
        ),
    )
    snapshot = store.configuration.snapshot("bridge")
    store.run.data.update(snapshot, project_id="f" * 32)

    async def ownership():
        pass

    store.assert_ownership = ownership
    store.reject_credentials = lambda value: None
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=BoundedRows())

    # Native identity/transaction locks are deliberately NOT asserted by this host fixture.
    async def writer(self, run, reader=None):
        pass

    async def execution(self, run, reader):
        pass

    monkeypatch.setattr(BridgeCompletions, "_writer", writer)
    monkeypatch.setattr(BridgeCompletions, "_execution", execution)
    return store, journal


@pytest.mark.parametrize("responses", [1, 2], ids=["first-unknown", "structured-reask-then-unknown"])
async def test_actual_agent_engine_retains_each_wire_before_post_and_keeps_graph_held(tmp_path, monkeypatch, responses):
    store, journal = await fixture(tmp_path, monkeypatch)
    provider = BridgeProvider(store.configuration.load_snapshot(store.run.data["config_revision"]))
    model = provider.model()
    requests = []

    def respond(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        intents = [row for row in journal.records.rows if row.kind == INTENT]
        entry = next(row.data for row in intents if row.idempotency_key == request.headers["idempotency-key"])
        assert entry["payload"]["descriptor"]["wire_sha256"] == hashlib.sha256(request.content).hexdigest()
        assert entry["payload"]["descriptor"]["wire_bytes"] == len(request.content)
        assert store.run.data["bridge_completion_hold"]["reason"] == "graph_checkpoint_unavailable"
        requests.append(request)
        if len(requests) < responses:
            return httpx2.Response(200, json=RESULT)
        return httpx2.Response(503, json={"error": {"code": "unavailable", "message": "Unknown retained effect"}})

    model.client._client._transport = httpx2.MockTransport(respond)
    model.client._client._mounts = {}
    monkeypatch.setattr(provider, "model", lambda **kwargs: model)
    engine = Engine(store, tmp_path / "workspaces", lambda _: provider, journal=journal)
    workspace = engine.workspaces / RUN
    workspace.mkdir()

    async def inspect(*args):
        return {"base_revision": "b" * 40, "head": "b" * 40, "source_tree": "c" * 40}

    async def checked(argv, *args, **kwargs):
        assert argv == ["git", "ls-files", "-z"]
        return ""

    monkeypatch.setattr(engine, "_inspect_workspace", inspect)
    monkeypatch.setattr(engine_module, "checked", checked)
    try:
        await engine.execute_run(RUN)
        run = await store.get_run(RUN)
        assert len(requests) == responses
        assert run["status"] == "needs_reconciliation" and run["pause"]["kind"] == "provider_outcome_unknown"
        assert len({r.headers["idempotency-key"] for r in requests}) == responses
        assert Journal.verify_chain([row.data for row in journal.records.rows])
        implementation = next(row for row in journal.tasks.rows if row.task_id == "implement")
        intents = [row.data for row in journal.records.rows if row.kind == INTENT]
        assert len(intents) == responses
        assert all(entry["payload"]["task_key"] == implementation.data["idempotency_key"] for entry in intents)
        assert all(entry["payload"]["config_field"] == "config_revision" for entry in intents)
        for status in ("cancelled", "interrupted", "failed"):
            await store.update(RUN, status=status)
            with pytest.raises(ValueError, match="graph checkpoint"):
                await RunService(store, engine).retry(RUN)
        assert store.created == []
    finally:
        await model.client.close()


async def test_native_callback_exact_identity_and_scope_are_checked_before_post(tmp_path, monkeypatch):
    store, journal = await fixture(tmp_path, monkeypatch)
    effect = await journal.intent(RUN, "implement")
    service = BridgeCompletions(store, journal)
    scope = service.scope(await store.get_run(RUN), effect)
    body = b'{"model":"bridge/default","messages":[]}'
    descriptor = {
        "request_id": "fbr1_" + "a" * 32,
        "principal_id": "fixture",
        "model": "bridge/default",
        "endpoint": "http://localhost:19191/v1",
        "wire_sha256": hashlib.sha256(body).hexdigest(),
        "wire_bytes": len(body),
    }
    original = await scope.admit(descriptor, body)
    assert await scope.admit(descriptor, body) == original
    assert len([row for row in journal.records.rows if row.kind == INTENT]) == 1
    changed = body + b" "
    with pytest.raises(ValueError, match="another request"):
        await scope.admit(
            {**descriptor, "wire_sha256": hashlib.sha256(changed).hexdigest(), "wire_bytes": len(changed)}, changed
        )
    await scope.guard(original, body)
    store.run.data["source_tree"] = "c" * 40
    with pytest.raises(Exception, match="cut changed"):
        await scope.guard(original, body)


@pytest.mark.parametrize(
    "budget", [{"intents": 100}, {"bytes": 16 * 1024 * 1024}, {"rows": 10001}, {"max_intent_bytes": 16385}]
)
async def test_retention_preflight_precedes_journal_body_materialization(tmp_path, monkeypatch, budget):
    store, journal = await fixture(tmp_path, monkeypatch)
    service = BridgeCompletions(store, journal)

    async def bounded(run_id):
        return {"rows": 1, "bytes": 1, "intents": 0, "max_intent_bytes": 0, "responses": 0, **budget}

    async def forbidden(*args):
        pytest.fail("Oversized journal body must not be materialized")

    monkeypatch.setattr(journal.records, "bridge_budget", bounded)
    monkeypatch.setattr(journal.records, "find_for_run", forbidden)
    with pytest.raises(ValueError, match="retention is full"):
        await service._budget(RUN, reserve=True)


async def test_assurance_uses_its_own_review_revision_and_preserves_cancellation(tmp_path, monkeypatch):
    import asyncio
    import zipfile

    from firefly_forge import assurance as assurance_module
    from firefly_forge.providers import ProviderSubmissionCancelled

    store, journal = await fixture(tmp_path, monkeypatch)
    snapshot = store.configuration.snapshot("bridge")
    store.run.data.update(
        assurance={"review_agent": "bridge"},
        source_tree="frozen",
        final_verification=[],
        review_config_revision=snapshot["config_revision"],
    )
    provider = BridgeProvider(store.configuration.load_snapshot(snapshot["config_revision"]))
    model = provider.model()
    submitted = asyncio.Event()
    calls = []

    async def respond(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        calls.append(request)
        submitted.set()
        await asyncio.Event().wait()

    model.client._client._transport = httpx2.MockTransport(respond)
    model.client._client._mounts = {}
    monkeypatch.setattr(provider, "model", lambda **kwargs: model)

    async def checked(argv, cwd):
        for arg in argv:
            if arg.startswith("--output="):
                with zipfile.ZipFile(arg.split("=", 1)[1], "w") as archive:
                    archive.writestr("product.py", "value = 1\n")
        return "frozen"

    monkeypatch.setattr(assurance_module, "checked", checked)
    workspace = tmp_path / "original"
    workspace.mkdir()
    (workspace / "product.py").write_text("value = 1\n")

    async def emit(*args):
        pass

    assurance = assurance_module.Assurance(store, tmp_path / "reviews", journal, lambda _: provider)
    execution = asyncio.create_task(assurance.review(await store.get_run(RUN), workspace, emit))
    try:
        await asyncio.wait_for(submitted.wait(), 5)
        execution.cancel()
        with pytest.raises(ProviderSubmissionCancelled) as caught:
            await execution
        assert execution.cancelled() and caught.value.native_retained is True
        intents = [row.data for row in journal.records.rows if row.kind == INTENT]
        assert len(intents) == len(calls) == 1
        assert intents[0]["task_id"] == "review"
        assert intents[0]["payload"]["config_field"] == "review_config_revision"
        assert intents[0]["payload"]["config_revision"] == snapshot["config_revision"]
        assert intents[0]["payload"]["config_revision"] != store.run.data["config_revision"]
        errors = [row.data for row in journal.records.rows if row.kind == "error"]
        assert len(errors) == 1 and errors[0]["task_id"] == "review"
        assert errors[0]["idempotency_key"] == intents[0]["payload"]["task_key"]
        assert Journal.verify_chain([row.data for row in journal.records.rows])
    finally:
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await model.client.close()
