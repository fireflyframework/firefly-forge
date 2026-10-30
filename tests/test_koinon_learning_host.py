# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real native WAL and SDK HTTP contracts retain operational evidence without promotion."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from test_koinon_knowledge_host import context, entry
from test_koinon_sql_host import sql_manager as sql_manager

from firefly_forge.journal import digest
from firefly_forge.learning import LearningLedger
from firefly_forge.learning_configuration import configure_learning
from firefly_forge.learning_models import LearningEnvelope, LearningPolicy
from firefly_forge.learning_persistence import ForgeLearningProjectionEntity, ForgeLearningRecordEntity
from firefly_forge.learning_projection import LearningProjection
from firefly_forge.project_access import ProjectAccess


@pytest.fixture
async def learning_case(sql_manager, monkeypatch):
    knowledge, store, journal, state, access = sql_manager
    run = store.run.data
    run.update(id=store.run.id.hex, status="succeeded", source_tree="b" * 40, checkpoint_revision="c" * 40)
    store.run.repository_id = UUID(run["repository_id"])
    run["final_verification"] = [{"argv": ["check"], "exit_code": 0, "output": "PASS"}]
    run["learning_snapshot"] = configure_learning(store.configuration, LearningPolicy(projected_retrieval=True))
    for task, payload in [
        ("verify", {"checks": run["final_verification"], "source_tree": run["source_tree"]}),
        ("checkpoint", {"source_tree": run["source_tree"], "checkpoint_revision": run["checkpoint_revision"]}),
    ]:
        await journal._append(run["id"], task, task, "receipt", payload)
    envelope = LearningEnvelope.model_validate(
        {
            "correlation_id": "1" * 32,
            "run_id": run["id"],
            "type": "local_outcome",
            "task_id": "verify",
            "role": "qa",
            "purpose": "verification",
            "question": "Check the public contract",
            "statement": "Observed check returned exit code 0; external claims remain unverified.",
            "context": {
                "repository_id": run["repository_id"],
                "project_id": run["project_id"],
                "framework": "pyfly",
                "version": "26.10.16",
                "dependency_digest": "d" * 64,
                "source_tree": run["source_tree"],
                "query_digest": hashlib.sha256(b"Check the public contract").hexdigest(),
            },
            "source": {"base_revision": "c" * 40, "head": "c" * 40, "source_tree": run["source_tree"]},
            "receipts": [
                {"kind": x["kind"], "key": x["idempotency_key"], "sequence": x["sequence"], "digest": x["digest"]}
                for x in await journal.entries(run["id"])
            ],
        }
    ).model_dump(mode="json")
    now = datetime(2026, 10, 30, tzinfo=UTC)
    record = ForgeLearningRecordEntity(
        id=UUID("2" * 32),
        run_id=store.run.id,
        repository_id=UUID(run["repository_id"]),
        correlation_id="1" * 32,
        state="verified",
        kind="local_outcome",
        data={
            "envelope": envelope,
            "verified_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=72)).isoformat(),
        },
    )
    state.update(record=record, projection=None, events=[], source_posts=0, now=now, lost=False)

    class Records:
        async def find_by_id(self, key, **_kwargs):
            return record if key == record.id else None

        async def for_repository(self, key):
            return [record] if key == record.repository_id else []

        async def database_time(self):
            return state["now"]

    class Projections:
        async def for_record(self, key):
            return state["projection"] if key == record.id else None

        async def find_by_id(self, key, **_kwargs):
            row = state["projection"]
            return row if row and row.id == key else None

        async def save(self, row):
            state["projection"] = row

    class Events:
        async def for_record(self, key):
            return [x for x in state["events"] if x.record_id == key]

        async def for_request(self, key, request):
            return next((x for x in state["events"] if x.record_id == key and x.request_id == request), None)

        async def save(self, row):
            state["events"].append(row)

    async def project(_self, _actor, _project, permission):
        await access.require_repository(_actor, run["repository_id"], permission)

    monkeypatch.setattr(ProjectAccess, "require_project", project)
    ledger = LearningLedger(store, journal, Records(), Events(), Projections())
    projection = LearningProjection(store, journal, ledger, knowledge)
    state["projection"] = ForgeLearningProjectionEntity(
        id=UUID("3" * 32),
        record_id=record.id,
        state="pending",
        payload_digest=digest(envelope),
        data={"payload": copy.deepcopy(envelope)},
    )

    async def handler(request):
        path = request.url.path
        if path.endswith("/context"):
            value = context()
            value["workspace_permissions"]["workspace"] += ["ingest:write", "catalog:write"]
        elif path.endswith("/sources/upload"):
            state["source_posts"] += 1
            assert any(x.kind == "koinon_source_intent" for x in journal.records.rows)
            assert state["projection"].state == "executing"
            state["body"] = request.content.split(b"\r\n\r\n")[-1].rsplit(b"\r\n--", 1)[0]
            state["sha"] = hashlib.sha256(state["body"]).hexdigest()
            if state["lost"]:
                raise httpx.ReadTimeout("Controlled uncertain source receipt", request=request)
            if state.get("cancel"):
                import asyncio

                raise asyncio.CancelledError()
            value = {"source": source_entry(), "assets": [entry("document", "document", parent="source")]}
        elif "/catalog/" in path:
            selected = path.rsplit("/", 1)[1]
            if selected == "source":
                value = source_entry()
            else:
                parent = {"project": "workspace", "collection": "project", "document": "source"}[selected]
                value = entry(selected, selected, parent=parent)
        elif path.endswith("/catalog"):
            value = {
                "items": [entry("document", "document", parent="source")],
                "page": 1,
                "page_size": 25,
                "pages": 1,
                "total": 1,
            }
        elif path.endswith("/documents/document/chunks"):
            value = {
                "document_id": "document",
                "source_id": "source",
                "native_source_id": "native",
                "source_version_id": state.get("version", "version"),
                "extraction_id": "extraction",
                "items": [],
                "offset": 0,
                "limit": 1,
                "total": 0,
                "next_offset": None,
            }
        elif path.endswith("/search"):
            payload = json.loads(request.content)
            assert payload["answer"] is False
            if payload.get("asset_ids") is not None:
                assert payload["asset_ids"] == ["document"]
            value = {
                "hits": [
                    {
                        "id": state.get("chunk", "chunk"),
                        "asset_id": "document",
                        "source_id": "source",
                        "title": "Operational observation",
                        "text": "Observed exit code",
                        "source_version_id": state.get("hit_version", "version"),
                        "extraction_id": "extraction",
                    }
                ],
                "citations": [],
                "answer": None,
            }
            if state.get("foreign_citation"):
                value["hits"][0]["citations"] = [{"asset_id": "foreign-document", "source_id": "foreign-source"}]
            if not payload.get("asset_ids"):
                value["hits"] = []
        else:
            raise AssertionError("Unadmitted Koinon operation " + path)
        if state.get("after_remote"):
            state["after_remote"](path)
        return httpx.Response(200, json=value)

    def source_entry():
        value = entry("source", "source", parent="collection")
        value.update(name="forge-learning-" + record.id.hex + "-v2.json", metadata={"sha256": state["sha"]})
        return value

    knowledge.transport = httpx.MockTransport(handler)
    return SimpleNamespace(
        service=projection,
        ledger=ledger,
        knowledge=knowledge,
        store=store,
        journal=journal,
        state=state,
        record=record,
        access=access,
        run=run,
    )


async def test_native_projection_retains_v2_operational_bytes_before_one_upload(learning_case):
    c = learning_case
    result = await c.service.submit(c.record.id.hex, request_id="4" * 32)
    assert result["version"] == 2 and result["state"] == "source_admitted"
    body = json.loads(c.state["body"])
    assert body["kind"] == "local_outcome" and body["authority"] == "native_receipt_observation"
    assert body["envelope"] == c.record.data["envelope"]
    assert body["canonical_memory"] is False and "user_statement" not in c.state["body"].decode()
    assert (await c.service.submit(c.record.id.hex, request_id="4" * 32))["source_id"] == "source"
    assert c.state["source_posts"] == 1


async def test_native_projection_lost_reply_holds_without_redispatch_and_reads_exact_versions(learning_case):
    c = learning_case
    c.state["lost"] = True
    with pytest.raises(ValueError, match="unknown|uncertain"):
        await c.service.submit(c.record.id.hex, request_id="4" * 32)
    with pytest.raises(ValueError, match="reconcil|uncertain"):
        await c.service.submit(c.record.id.hex, request_id="5" * 32)
    observed = await c.service.observe(c.record.id.hex, request_id="6" * 32, source_id="source")
    assert observed["state"] == "verified" and observed["source_versions"][0]["source_version_id"] == "version"
    assert observed["upstream_authority"] == "unverified" and c.record.state == "verified"
    assert c.state["source_posts"] == 1


async def test_native_projection_changed_receipt_refuses_before_upload(learning_case):
    c = learning_case
    c.record.data["envelope"]["receipts"][0]["digest"] = "0" * 64
    with pytest.raises(ValueError, match="receipt|payload|evidence"):
        await c.service.submit(c.record.id.hex, request_id="4" * 32)
    assert c.state["source_posts"] == 0 and c.state["projection"].state == "pending"


async def verified(c):
    await c.service.submit(c.record.id.hex, request_id="4" * 32)
    return await c.service.observe(c.record.id.hex, request_id="6" * 32)


async def test_native_projected_reuse_requires_current_version_and_exact_learning_context(learning_case):
    c = learning_case
    await verified(c)
    context = c.record.data["envelope"]["context"]
    result = await c.service.retrieve(c.run["id"], "Check the public contract", context, c.record.id.hex)
    assert result["state"] == "verified" and result["version"] == 2
    assert result["citations"][0]["source_version_id"] == "version"
    c.state["version"] = "changed"
    result = await c.service.retrieve(c.run["id"], "Check the public contract", context, c.record.id.hex)
    assert result["state"] == "unavailable" and result["reason"] == "native_projection_unavailable"


async def test_native_projected_reuse_expiry_source_and_local_invalidation_never_use_remote_cache(learning_case):
    c = learning_case
    await verified(c)
    context = c.record.data["envelope"]["context"]
    for change in ["expiry", "invalidated", "source", "dependency"]:
        c.record.state = "verified"
        c.state["now"] = datetime(2026, 10, 30, tzinfo=UTC)
        observed = copy.deepcopy(context)
        if change == "expiry":
            c.state["now"] += timedelta(days=4)
        elif change == "invalidated":
            c.record.state = "invalidated"
        else:
            observed["source_tree" if change == "source" else "dependency_digest"] = "f" * (
                40 if change == "source" else 64
            )
        result = await c.service.retrieve(c.run["id"], "Check the public contract", observed, c.record.id.hex)
        assert result["state"] == "unavailable" and result["reason"] == "record_ineligible"
    assert c.state["source_posts"] == 1


async def failed(c):
    c.run["status"] = "failed"
    receipt = await c.journal._append(
        c.run["id"],
        "verify",
        "failed-check",
        "receipt",
        {
            "source_tree": c.run["source_tree"],
            "checks": [{"argv": ["check"], "exit_code": 1, "output": "assertion failed"}],
        },
    )
    c.record.kind, c.record.state = "observed_failure", "candidate"
    c.record.data["envelope"].update(
        type="observed_failure",
        statement="Observed exit code 1; cause not classified.",
        receipts=[
            {
                "kind": "receipt",
                "key": receipt["idempotency_key"],
                "digest": receipt["digest"],
                "sequence": receipt["sequence"],
            }
        ],
    )
    c.state["projection"] = None


async def test_native_negative_hint_is_retrieved_with_provenance_without_becoming_positive_authority(learning_case):
    c = learning_case
    await failed(c)
    await verified(c)
    hints = await c.service.failure_hints(
        c.run["id"], "Check the public contract", c.record.data["envelope"]["context"]
    )
    assert len(hints) == 1 and hints[0]["kind"] == "observed_failure"
    assert hints[0]["failure_cause"] == "not_classified" and hints[0]["may_skip_independent_checks"] is False
    assert hints[0]["citations"][0]["source_version_id"] == "version"
    assert c.record.state == "candidate"
    assert (await c.ledger.lookup(c.run["id"], "Check the public contract", c.record.data["envelope"]["context"]))[
        "eligible"
    ] is False


async def test_native_observation_receipt_keeps_v2_version_provenance_and_exact_request_identity(learning_case):
    c = learning_case
    await verified(c)
    receipt = await c.service.observation_receipt(c.record.id.hex, "6" * 32)
    assert receipt["version"] == 2 and receipt["kind"] == "projection_observation_v2"
    assert receipt["source_versions"][0]["extraction_id"] == "extraction"
    with pytest.raises(KeyError):
        await c.service.observation_receipt(c.record.id.hex, "7" * 32)


async def test_admitted_v1_projection_is_never_relabelled_or_replayed_in_native_configuration(learning_case):
    c = learning_case
    row = c.state["projection"]
    row.state = "source_admitted"
    row.data.update(target={"revision": "a" * 32}, source_id="retained-v1")
    before = copy.deepcopy(row.data)
    with pytest.raises(ValueError, match="V1|migration"):
        await c.service.submit(c.record.id.hex, request_id="4" * 32)
    assert row.data == before and c.state["source_posts"] == 0


async def test_native_receipt_reads_across_sdk_controller_and_mcp_keep_typed_version_provenance(
    learning_case, monkeypatch
):
    import contextlib

    from firefly_forge import cli
    from firefly_forge.client import ForgeClient
    from firefly_forge.learning_controllers import LearningController
    from firefly_forge.mcp_server import create_server

    c = learning_case
    await verified(c)
    controller = LearningController(c.ledger, c.service, c.store, None)
    response = await controller.observation_receipt(c.record.id.hex, "6" * 32)
    payload = response.model_dump(mode="json")
    with ForgeClient("https://forge.example", "controlled-fixture") as client:
        client.http.close()
        calls = []
        client.http = httpx.Client(
            base_url="https://forge.example/api/v1/",
            transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json=payload)),
        )
        result = client.learning_projection_observation_receipt(c.record.id.hex, "6" * 32)
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        wire = await create_server().call_tool(
            "get_learning_projection_observation_receipt", {"record_id": c.record.id.hex, "request_id": "6" * 32}
        )
        assert not wire.is_error and wire.structured_content == result
    assert result["version"] == 2 and result["source_versions"][0]["source_version_id"] == "version"
    assert len(calls) == 2 and all(x.method == "GET" for x in calls)


async def test_native_negative_hint_expires_and_retrieved_version_change_never_relabels_it(learning_case):
    c = learning_case
    await failed(c)
    await verified(c)
    context = c.record.data["envelope"]["context"]
    c.state["version"] = "changed"
    assert await c.service.failure_hints(c.run["id"], "Check the public contract", context) == []
    c.state["version"] = "version"
    c.state["now"] += timedelta(days=4)
    assert await c.service.failure_hints(c.run["id"], "Check the public contract", context) == []
    assert c.record.state == "candidate"


async def test_foreign_native_receipt_scalar_is_refused_before_projection_upload(learning_case):
    c = learning_case
    c.journal.records.rows[0].run_id = UUID("f" * 32)
    with pytest.raises(ValueError, match="custody|receipt|ownership"):
        await c.service.submit(c.record.id.hex, request_id="4" * 32)
    assert c.state["source_posts"] == 0


async def test_knowledge_first_uses_native_positive_projection_without_dispatching_web(
    learning_case, monkeypatch, tmp_path
):
    from firefly_forge import knowledge_first as module
    from firefly_forge.knowledge_first import KnowledgeFirst
    from firefly_forge.learning_models import LearningContext

    c = learning_case
    await verified(c)
    context = c.record.data["envelope"]["context"]

    async def observed(*args, **kwargs):
        return LearningContext.model_validate(context)

    monkeypatch.setattr(module, "observe_context", observed)
    c.run["search_snapshot"] = {"revision": "f" * 32}

    async def task(*args):
        return SimpleNamespace(data={"idempotency_key": "task"})

    c.journal._task = task

    class Broker:
        async def invoke(self, _run, _task, _tool, _correlation, _arguments, request, **_kwargs):
            return await request()

    class Web:
        async def search(self, *args, **kwargs):
            pytest.fail("An eligible local outcome must not dispatch a web provider")

    first = KnowledgeFirst(c.store, c.journal, c.ledger, Broker(), Web(), c.service)
    first.knowledge = c.knowledge
    result = await first.search(
        c.run["id"],
        "implement",
        "Check the public contract",
        1,
        source={"source_tree": c.run["source_tree"]},
        workspace=tmp_path,
    )
    assert result["origin"] == "verified_local_outcome"
    assert result["knowledge_decision"]["projection"]["version"] == 2


async def test_native_cached_projection_guard_refuses_local_invalidation_and_projection_drift(learning_case):
    c = learning_case
    await verified(c)
    context = c.record.data["envelope"]["context"]
    selected = await c.service.retrieve(c.run["id"], "Check the public contract", context, c.record.id.hex)
    session = object()
    c.store.session_provider = SimpleNamespace(current=lambda: session)
    guard = await c.service.retain(c.run["id"], context, selected)
    guard()
    c.record.state = "invalidated"
    with pytest.raises(ValueError, match="changed|applicable"):
        guard()
    c.record.state = "verified"
    c.state["projection"].data["source_versions"][0]["source_version_id"] = "foreign-version"
    with pytest.raises(ValueError, match="changed|applicable"):
        guard()


async def test_native_projection_current_permission_loss_withholds_retrieved_hint(learning_case):
    from firefly_forge.project_access import AccessDenied

    c = learning_case
    await failed(c)
    await verified(c)

    def revoke(path):
        if path.endswith("/search"):
            c.access.allowed = False

    c.state["after_remote"] = revoke
    with pytest.raises(AccessDenied):
        await c.service.failure_hints(c.run["id"], "Check the public contract", c.record.data["envelope"]["context"])
    assert c.record.state == "candidate" and c.state["source_posts"] == 1


async def test_knowledge_lookup_returns_native_failure_hint_without_asserting_a_positive_solution(
    learning_case, monkeypatch, tmp_path
):
    from firefly_forge import knowledge_first as module
    from firefly_forge.knowledge_first import KnowledgeFirst
    from firefly_forge.learning_models import LearningContext

    c = learning_case
    await failed(c)
    await verified(c)

    async def observed(*args, **kwargs):
        return LearningContext.model_validate(c.record.data["envelope"]["context"])

    monkeypatch.setattr(module, "observe_context", observed)

    async def task(*args):
        return SimpleNamespace(data={"idempotency_key": "task"})

    c.journal._task = task

    class Broker:
        async def invoke(self, _run, _task, _tool, _correlation, _arguments, request, **_kwargs):
            return await request()

    class Web:
        async def search(self, *args, **kwargs):
            pytest.fail("A knowledge-only lookup cannot dispatch web")

    first = KnowledgeFirst(c.store, c.journal, c.ledger, Broker(), Web(), c.service)
    first.knowledge = c.knowledge
    result = await first.search(
        c.run["id"],
        "implement",
        "Check the public contract",
        1,
        source={"source_tree": c.run["source_tree"]},
        workspace=tmp_path,
        allow_external=False,
    )
    assert result["origin"] == "candidate_reference" and result["knowledge_decision"]["eligible"] is False
    hint = result["knowledge_decision"]["observed_failure_hints"][0]
    assert hint["may_skip_independent_checks"] is False and hint["failure_cause"] == "not_classified"
    assert result["results"][0]["citation_id"] == "learning-failure-" + c.record.id.hex


async def test_native_projection_cancellation_retains_unknown_source_without_repeating_write(learning_case):
    import asyncio

    c = learning_case
    c.state["cancel"] = True
    with pytest.raises(asyncio.CancelledError):
        await c.service.submit(c.record.id.hex, request_id="4" * 32)
    assert c.state["projection"].state == "unknown"
    with pytest.raises(ValueError, match="uncertain"):
        await c.service.submit(c.record.id.hex, request_id="5" * 32)
    assert c.state["source_posts"] == 1


async def test_native_projection_never_saves_credential_as_retrieved_citation(learning_case):
    c = learning_case
    await c.service.submit(c.record.id.hex, request_id="4" * 32)
    c.state["chunk"] = "private-machine-token"
    with pytest.raises(ValueError, match="credential"):
        await c.service.observe(c.record.id.hex, request_id="6" * 32)
    assert c.state["projection"].state == "source_admitted"
    assert c.state["events"][-1].data["kind"] != "projection_observation_v2"


async def test_native_projection_refuses_foreign_nested_retrieval_citation(learning_case):
    c = learning_case
    await c.service.submit(c.record.id.hex, request_id="4" * 32)
    c.state["foreign_citation"] = True
    from firefly_forge.project_access import AccessDenied

    with pytest.raises(AccessDenied, match="citation"):
        await c.service.observe(c.record.id.hex, request_id="6" * 32)
    assert c.state["projection"].state == "source_admitted"


async def test_retained_negative_hint_cannot_gain_a_fabricated_cause_or_skip_permission(learning_case):
    c = learning_case
    await failed(c)
    await verified(c)
    context = c.record.data["envelope"]["context"]
    selected = (await c.service.failure_hints(c.run["id"], "Check the public contract", context))[0]
    c.store.session_provider = SimpleNamespace(current=lambda: c)
    selected["may_skip_independent_checks"] = True
    with pytest.raises(ValueError, match="changed|applicable|hint"):
        await c.service.retain(c.run["id"], context, selected)
