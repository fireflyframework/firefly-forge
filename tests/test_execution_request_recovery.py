# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Only retained server receipts can resolve a missing queue completion."""

from uuid import uuid4

import pytest
from test_execution_requests import queue_fixture

from firefly_forge.execution_requests import ExecutionRequestReconciliation
from firefly_forge.journal import Journal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess


async def publication_receipt(store, run):
    await store.update(run["id"], branch="forge/" + run["id"][:12])
    current = await store.get_run(run["id"])
    journal = Journal(store)
    intent = await journal.intent(
        run["id"],
        "publish",
        payload={
            "branch": current["branch"],
            "source_tree": current["source_tree"],
        },
    )
    url = "https://github.com/example/product/pull/1"
    await store.update(run["id"], publication_status="published", pr_url=url)
    await journal.receipt(
        run["id"],
        intent["idempotency_key"],
        evidence={
            "pr_url": url,
            "branch": current["branch"],
            "source_tree": current["source_tree"],
        },
    )
    return url


async def unknown_request(api, worker, store, run, owner):
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        request = await api.request_publish(run["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)
    await store.acquire_ownership()
    try:
        assert await worker._claim(request["id"])
        await worker.recover()
    finally:
        await store.release_ownership()
    return request


async def test_lost_queue_receipt_settles_from_matching_terminal_publication_journal(store, tmp_path):
    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    await store.update(run["id"], branch="forge/" + run["id"][:12])
    run = await store.get_run(run["id"])
    request = await unknown_request(api, worker, store, run, owner)
    url = await publication_receipt(store, run)
    body = ExecutionRequestReconciliation(reconciliation_id=uuid4().hex)
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        result = await api.reconcile(run["id"], request["id"], body)
        assert result["status"] == "completed" and result["result"]["pr_url"] == url
        assert await api.reconcile(run["id"], request["id"], body) == result
        with pytest.raises(ValueError, match="another reconciliation"):
            await api.reconcile(run["id"], request["id"], ExecutionRequestReconciliation(reconciliation_id=uuid4().hex))
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert api.verify_chain(await api.events(request["id"]))


async def test_unknown_remote_effect_without_receipt_stays_unknown_and_cannot_resubmit(store, tmp_path):
    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    request = await unknown_request(api, worker, store, run, owner)
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        with pytest.raises(ValueError, match="unknown|receipt"):
            await api.reconcile(run["id"], request["id"], ExecutionRequestReconciliation(reconciliation_id=uuid4().hex))
        assert (await api.request_publish(run["id"]))["id"] == request["id"]
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert (await api.get(request["id"]))["status"] == "needs_reconciliation"


async def test_reconciliation_rechecks_current_releaser_authority(store, tmp_path):
    api, worker, run, project, owner, builder = await queue_fixture(store, tmp_path)
    request = await unknown_request(api, worker, store, run, owner)
    await publication_receipt(store, run)
    await ProjectAccess(store).set_membership(project["id"], builder, "owner")
    await ProjectAccess(store).set_membership(project["id"], owner, "viewer")
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        with pytest.raises(AccessDenied):
            await api.reconcile(run["id"], request["id"], ExecutionRequestReconciliation(reconciliation_id=uuid4().hex))
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert (await api.get(request["id"]))["status"] == "needs_reconciliation"


async def test_cancel_receipt_requires_actual_drained_worker_and_matching_terminal_run(store, tmp_path):
    import asyncio

    from firefly_forge.engine import Engine
    from firefly_forge.store import identifier

    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    async with store._unit():
        row = await store.run_records.find_by_id(identifier(run["id"]))
        row.status = "developing"
        row.data = {**row.data, "status": row.status}
        await store.run_records.save(row)
    context = CURRENT_PRINCIPAL.set(owner)
    request = await api.request_cancel(run["id"])
    CURRENT_PRINCIPAL.reset(context)
    engine = Engine(store, tmp_path / "worker")
    started, drained = asyncio.Event(), asyncio.Event()

    async def owned_task():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    engine.active[run["id"]] = asyncio.create_task(owned_task())
    await started.wait()
    await store.acquire_ownership()
    try:
        assert await worker._claim(request["id"])
        result = await engine.cancel_effect(run["id"])
        assert drained.is_set()
        await worker._effect_receipt(request["id"], result)
        await worker.recover()
    finally:
        await store.release_ownership()
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        result = await api.reconcile(
            run["id"], request["id"], ExecutionRequestReconciliation(reconciliation_id=uuid4().hex)
        )
        assert result["status"] == "completed" and result["result"]["worker_drained"]
        assert (await store.get_run(run["id"]))["cancel_request"]["status"] == "acknowledged"
    finally:
        CURRENT_PRINCIPAL.reset(context)


def test_reconciliation_cannot_accept_client_assertions_of_remote_outcome():
    with pytest.raises(ValueError):
        ExecutionRequestReconciliation(reconciliation_id=uuid4().hex, no_effect=True)


async def test_error_after_dispatch_can_never_be_classified_as_safe_pre_effect_failure(store, tmp_path):
    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)

    async def ambiguous(*args):
        raise ValueError("Provider response is unavailable after dispatch")

    worker.publish_effect = ambiguous
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        request = await api.request_publish(run["id"])
        await store.acquire_ownership()
        try:
            await worker.execute(request["id"])
        finally:
            await store.release_ownership()
        assert (await worker.get(request["id"]))["status"] == "needs_reconciliation"
        assert (await api.request_publish(run["id"]))["id"] == request["id"]
        assert not any(event["kind"] == "worker_pre_effect_failure" for event in await api.events(request["id"]))
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_revocation_during_awaited_publication_proof_prevents_reconciliation_commit(store, tmp_path, monkeypatch):
    api, worker, run, project, owner, builder = await queue_fixture(store, tmp_path)
    await store.update(run["id"], branch="forge/" + run["id"][:12])
    run = await store.get_run(run["id"])
    request = await unknown_request(api, worker, store, run, owner)
    await publication_receipt(store, run)
    await ProjectAccess(store).set_membership(project["id"], builder, "owner")
    original = Journal.entries

    async def revoking(self, run_id):
        entries = await original(self, run_id)
        await ProjectAccess(store).set_membership(project["id"], owner, "viewer")
        return entries

    monkeypatch.setattr(Journal, "entries", revoking)
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        with pytest.raises(AccessDenied):
            await api.reconcile(run["id"], request["id"], ExecutionRequestReconciliation(reconciliation_id=uuid4().hex))
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert (await api.get(request["id"]))["status"] == "needs_reconciliation"


async def test_crash_after_worker_effect_receipt_before_queue_completion_is_recoverable(store, tmp_path, monkeypatch):
    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    await store.update(run["id"], branch="forge/" + run["id"][:12])
    run = await store.get_run(run["id"])
    context = CURRENT_PRINCIPAL.set(owner)
    request = await api.request_publish(run["id"])
    CURRENT_PRINCIPAL.reset(context)
    calls = []

    async def published(*args):
        calls.append(args)
        return await publication_receipt(store, run)

    worker.publish_effect = published
    original = worker._finish

    async def crash_once(request_id, status, evidence):
        if status == "completed":
            raise RuntimeError("Controlled lost queue completion")
        return await original(request_id, status, evidence)

    monkeypatch.setattr(worker, "_finish", crash_once)
    await store.acquire_ownership()
    try:
        await worker.execute(request["id"])
    finally:
        await store.release_ownership()
    assert len(calls) == 1
    assert (await api.get(request["id"]))["status"] == "needs_reconciliation"
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        completed = await api.reconcile(
            run["id"], request["id"], ExecutionRequestReconciliation(reconciliation_id=uuid4().hex)
        )
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert completed["status"] == "completed" and len(calls) == 1


async def test_source_tamper_after_terminal_receipt_cannot_settle_admitted_request(store, tmp_path):
    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    await store.update(run["id"], branch="forge/" + run["id"][:12])
    run = await store.get_run(run["id"])
    request = await unknown_request(api, worker, store, run, owner)
    await publication_receipt(store, run)
    await store.update(run["id"], source_tree="c" * 40)
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        with pytest.raises(ValueError, match="unknown|source|receipt"):
            await api.reconcile(run["id"], request["id"], ExecutionRequestReconciliation(reconciliation_id=uuid4().hex))
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert (await api.get(request["id"]))["status"] == "needs_reconciliation"


async def test_pre_dispatch_rejection_with_existing_delivery_intent_cannot_permit_retry(store, tmp_path):
    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(owner)
    request = await api.request_publish(run["id"])
    CURRENT_PRINCIPAL.reset(context)
    await Journal(store).intent(run["id"], "publish", payload={"source_tree": run["source_tree"]})
    await store.acquire_ownership()
    try:
        await worker.execute(request["id"])
    finally:
        await store.release_ownership()
    assert (await worker.get(request["id"]))["status"] == "needs_reconciliation"
    assert not any(event["kind"] == "worker_pre_effect_failure" for event in await api.events(request["id"]))


async def test_pre_effect_failure_receipt_gap_can_settle_failed_then_retry_explicitly(store, tmp_path):
    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(owner)
    request = await api.request_publish(run["id"])
    CURRENT_PRINCIPAL.reset(context)
    await store.acquire_ownership()
    try:
        assert await worker._claim(request["id"])
        await worker._observation(
            request["id"],
            "worker_pre_effect_failure",
            {
                "request_digest": request["request_digest"],
                "before_delivery_dispatch": True,
                "failure_kind": "authorization",
            },
        )
        await worker.recover()
    finally:
        await store.release_ownership()
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        result = await api.reconcile(
            run["id"], request["id"], ExecutionRequestReconciliation(reconciliation_id=uuid4().hex)
        )
        assert result["status"] == "failed"
        retry = await api.request_publish(run["id"])
        assert retry["id"] != request["id"] and retry["request"]["attempt"] == 2
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_known_credential_cannot_be_persisted_as_reconciliation_identity(store, tmp_path):
    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    request = await unknown_request(api, worker, store, run, owner)
    secret = "0123456789abcdef0123456789abcdef"
    store.credential_registry = lambda: {"FORGE_API_TOKEN": secret}
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        with pytest.raises(ValueError, match="credential"):
            await api.reconcile(run["id"], request["id"], ExecutionRequestReconciliation(reconciliation_id=secret))
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def rewrite_publication_metadata(store, run, *, status="done", evidence=None, attempt=1):
    from firefly_forge.store import identifier

    journal = Journal(store)
    async with store._unit():
        task = next(
            task for task in await journal.tasks.find_for_run(identifier(run["id"])) if task.task_id == "publish"
        )
        task.status, task.attempt = status, attempt
        task.data = {**task.data, "evidence": evidence or {}}
        await journal.tasks.save(task)


async def test_intent_only_with_fabricated_task_and_run_metadata_remains_unknown_http409(store, tmp_path, monkeypatch):
    import httpx

    from firefly_forge.api import create_application
    from firefly_forge.identity import IdentityService

    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    await store.update(run["id"], branch="forge/" + run["id"][:12])
    run = await store.get_run(run["id"])
    request = await unknown_request(api, worker, store, run, owner)
    await Journal(store).intent(
        run["id"], "publish", payload={"branch": run["branch"], "source_tree": run["source_tree"]}
    )
    url = "https://github.com/example/product/pull/99"
    await store.update(run["id"], publication_status="published", pr_url=url)
    await rewrite_publication_metadata(
        store, run, evidence={"pr_url": url, "branch": run["branch"], "source_tree": run["source_tree"]}
    )
    app = create_application(store.path.parent, "local-bootstrap-fixture-token", role="api")
    async with app.router.lifespan_context(app):

        async def authenticate(request):
            return owner

        monkeypatch.setattr(app.state.runtime.context.get_bean(IdentityService), "authenticate", authenticate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await client.post(
                f"/api/v1/runs/{run['id']}/execution-requests/{request['id']}/reconcile",
                headers={"Authorization": "Bearer fixture-auth"},
                json={"reconciliation_id": uuid4().hex},
            )
    assert response.status_code == 409
    assert (await api.get(request["id"]))["status"] == "needs_reconciliation"
    assert not any(entry["kind"] == "reconciliation" for entry in await api.events(request["id"]))


async def test_actual_receipt_is_authoritative_when_mutable_task_metadata_is_corrupted(store, tmp_path):
    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    await store.update(run["id"], branch="forge/" + run["id"][:12])
    run = await store.get_run(run["id"])
    request = await unknown_request(api, worker, store, run, owner)
    url = await publication_receipt(store, run)
    await rewrite_publication_metadata(store, run, status="running", attempt=99, evidence={"pr_url": "fabricated"})
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        result = await api.reconcile(
            run["id"], request["id"], ExecutionRequestReconciliation(reconciliation_id=uuid4().hex)
        )
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert result["status"] == "completed" and result["result"]["pr_url"] == url
    assert result["reconciliation"]["proof"]["attempt"] == 1


@pytest.mark.parametrize("mismatch", ["attempt", "source_tree", "branch", "pr_url", "multiple_receipts"])
async def test_publication_receipt_cannot_be_reused_from_another_attempt_or_source(store, tmp_path, mismatch):
    import uuid

    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    await store.update(run["id"], branch="forge/" + run["id"][:12])
    run = await store.get_run(run["id"])
    request = await unknown_request(api, worker, store, run, owner)
    journal = Journal(store)
    payload = {"branch": run["branch"], "source_tree": run["source_tree"]}
    intent = await journal.intent(run["id"], "publish", payload=payload)
    url = "https://github.com/example/product/pull/1"
    evidence = {**payload, "pr_url": url}
    key = intent["idempotency_key"]
    if mismatch == "attempt":
        key = uuid.uuid5(uuid.NAMESPACE_URL, f"forge:{run['id']}:publish:2").hex
    elif mismatch == "source_tree":
        evidence["source_tree"] = "c" * 40
    elif mismatch == "branch":
        evidence["branch"] = "forge/another"
    elif mismatch == "pr_url":
        evidence["pr_url"] = "https://github.com/example/product/pull/2"
    async with store._unit():
        await journal._append(run["id"], "publish", key, "receipt", evidence)
        if mismatch == "multiple_receipts":
            second_key = uuid.uuid5(uuid.NAMESPACE_URL, f"forge:{run['id']}:publish:2").hex
            await journal._append(run["id"], "publish", second_key, "intent", payload)
            await journal._append(run["id"], "publish", second_key, "receipt", evidence)
    await store.update(run["id"], publication_status="published", pr_url=url)
    await rewrite_publication_metadata(store, run, evidence={**payload, "pr_url": url})
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        with pytest.raises(ValueError, match="unknown|receipt"):
            await api.reconcile(run["id"], request["id"], ExecutionRequestReconciliation(reconciliation_id=uuid4().hex))
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert (await api.get(request["id"]))["status"] == "needs_reconciliation"
