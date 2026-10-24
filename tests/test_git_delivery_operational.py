# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The authenticated API queues effects; the ordinary owner poller performs actual local Git."""

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_git_delivery_local import delivery_case as delivery_case
from test_git_delivery_local import git

from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.git_delivery_controllers import GitDeliveryController
from firefly_forge.git_delivery_models import GitDeliveryInputV1
from firefly_forge.git_delivery_requests import GitDeliveryRequests
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL


@pytest.fixture
def operational(delivery_case):
    c = delivery_case()
    rows = {}
    events = []

    async def find(id, **kwargs):
        return rows.get(id)

    async def save(row):
        rows[row.id] = row
        return row

    async def for_run(id, kind):
        return [r for r in rows.values() if r.run_id == id and r.kind == kind]

    async def state(status, **kwargs):
        return [r for r in rows.values() if r.status == status and r.kind == kwargs.get("kind")]

    async def count():
        return sum(r.status in {"queued", "executing"} for r in rows.values())

    async def event_save(row):
        events.append(row)
        return row

    async def event_rows(id):
        return [r for r in events if r.request_id == id]

    async def runrow(id, **kwargs):
        return SimpleNamespace(data=c.state["run"], status=c.state["run"]["status"])

    async def locked(id):
        return await runrow(id)

    c.service.store.run_records = SimpleNamespace(find_by_id=runrow)
    c.service.store.schema = "public_fixture"
    c.service.journal._run = locked
    runtime = SimpleNamespace(require_execution=lambda: None)
    records = SimpleNamespace(find_by_id=find, save=save, for_run=for_run, state=state, pending_count=count)
    receipts = SimpleNamespace(save=event_save, events=event_rows)
    requests = ExecutionRequests(c.service.store, runtime, records, receipts)
    tickets = GitDeliveryRequests(c.service, requests)
    requests.git_deliveries = tickets
    c.controller = GitDeliveryController(c.service, tickets)
    c.requests = requests
    c.rows = rows
    user = Principal(UUID("4" * 32), "public-fixture", "owner", authentication_method="local_bootstrap")
    token = CURRENT_PRINCIPAL.set(user)
    yield c
    CURRENT_PRINCIPAL.reset(token)


async def test_api_admits_exact_ticket_without_git_then_owner_poller_commits_and_reads_receipt(operational):
    c = operational
    (c.root / "product.py").write_text("value = 2\n")
    git(c.root, "add", "product.py")
    body = GitDeliveryInputV1(
        version=1, request_id="a" * 32, expected_head=c.base, expected_source_tree=git(c.root, "write-tree")
    )
    admitted = await c.controller.request("1" * 32, body)
    assert admitted["status"] == "queued" and git(c.root, "rev-parse", "HEAD") == c.base
    assert len(c.rows) == 1 and not c.state["entries"]
    assert (await c.controller.request("1" * 32, body))["id"] == admitted["id"]
    poller = asyncio.create_task(c.requests._poll())
    try:
        async with asyncio.timeout(10):
            while c.rows[UUID("a" * 32)].status == "queued" or c.requests.active:
                await asyncio.sleep(0.02)
                if c.rows[UUID("a" * 32)].status == "completed":
                    break
        receipt = await c.controller.ticket("1" * 32, "a" * 32)
        assert receipt["status"] == "completed" and receipt["result"]["revision"] == git(c.root, "rev-parse", "HEAD")
        assert [e["kind"] for e in receipt["events"]] == ["request_admitted", "worker_claim", "worker_completed"]
        assert (await c.controller.list("1" * 32))["items"][0]["revision"] == receipt["result"]["revision"]
    finally:
        poller.cancel()
        await asyncio.gather(poller, *c.requests.active.values(), return_exceptions=True)


async def test_worker_revocation_between_admission_and_dispatch_keeps_source_uncommitted(operational):
    c = operational
    (c.root / "product.py").write_text("value = 2\n")
    git(c.root, "add", "product.py")
    body = GitDeliveryInputV1(
        version=1, request_id="a" * 32, expected_head=c.base, expected_source_tree=git(c.root, "write-tree")
    )
    await c.controller.request("1" * 32, body)
    c.state["allowed"] = False
    await c.requests.git_deliveries.execute("a" * 32)
    assert c.rows[UUID("a" * 32)].status == "failed"
    assert git(c.root, "rev-parse", "HEAD") == c.base and not c.state["entries"]


async def test_readback_does_not_replace_a_changed_or_unknown_ticket(operational):
    c = operational
    (c.root / "product.py").write_text("value = 2\n")
    git(c.root, "add", "product.py")
    body = GitDeliveryInputV1(
        version=1, request_id="a" * 32, expected_head=c.base, expected_source_tree=git(c.root, "write-tree")
    )
    await c.controller.request("1" * 32, body)
    changed = body.model_copy(update={"expected_source_tree": "f" * 40})
    with pytest.raises(ValueError, match="identity"):
        await c.controller.request("1" * 32, changed)
    assert len(c.rows) == 1 and c.rows[UUID("a" * 32)].status == "queued"


async def test_api_checkpoint_refuses_stale_index_tree_then_uses_reviewed_worktree(operational):
    c = operational
    (c.root / "product.py").write_text("value = 2\n")
    stale_tree = git(c.root, "write-tree")
    assert stale_tree == git(c.root, "rev-parse", "HEAD^{tree}")
    with pytest.raises(ValueError, match="The reviewed checkpoint source tree changed"):
        await c.service.checkpoint(
            "1" * 32,
            request_id="a" * 32,
            expected_head=c.base,
            expected_source_tree=stale_tree,
            actor=c.state["run"]["execution_actor"],
        )
    assert git(c.root, "rev-parse", "HEAD") == c.base and not c.state["entries"]
    files, _patch, tree = await c.service.engine._snapshot(c.root, c.base, expected_head=c.base)
    assert files == ["product.py"] and tree != stale_tree
    body = GitDeliveryInputV1(version=1, request_id="b" * 32, expected_head=c.base, expected_source_tree=tree)
    await c.controller.request("1" * 32, body)
    await c.requests.git_deliveries.execute("b" * 32)
    receipt = await c.controller.ticket("1" * 32, "b" * 32)
    assert receipt["status"] == "completed", receipt
    assert receipt["result"]["source_tree"] == tree
    assert git(c.root, "rev-parse", "HEAD^{tree}") == tree
    assert c.state["run"]["base_revision"] == c.base
