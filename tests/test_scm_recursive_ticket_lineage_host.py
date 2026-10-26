# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native descendants keep their authenticated root and deterministic current read scope."""

import copy
from uuid import UUID

import pytest
from test_checkpoint_test_trace_host import CHILD, CREATION, PARENT, PARENT_RECEIPT
from test_checkpoint_test_trace_host import lineage as lineage

from firefly_forge.journal import digest
from firefly_forge.project_access import AccessDenied
from firefly_forge.scm_conversation_models import addressed_status
from firefly_forge.scm_natural_tasks import SCMNaturalTasks

GRAND, REQUEST = "a" * 32, "b" * 32


def family(c, monkeypatch):
    c.receipt["admitted_at"] = "2026-10-25T10:00:00Z"
    c.child["status"] = "failed"
    child = copy.deepcopy(c.child)
    child["id"] = GRAND
    source = copy.deepcopy(child["source_checkpoint_snapshot"])
    source["operation"] = "maintain"
    source["reference"]["run_id"] = CHILD
    source["digest"] = digest({k: v for k, v in source.items() if k != "digest"})
    child.update(operation="maintain", source_checkpoint=source["reference"], source_checkpoint_snapshot=source)
    receipt = {
        **copy.deepcopy(c.receipt),
        "request_id": REQUEST,
        "run_id": GRAND,
        "source_checkpoint": source,
        "admitted_at": "2026-10-25T11:00:00Z",
    }
    runs = {PARENT: c.parent, CHILD: c.child, GRAND: child}
    receipts = {CREATION: c.receipt, REQUEST: receipt}

    async def current(run_id):
        return copy.deepcopy(runs[run_id])

    async def children(parents, repository):
        c.state["queries"] += 1
        assert len(parents) <= 200
        return [UUID(k) for k, v in receipts.items() if UUID(v["source_checkpoint"]["reference"]["run_id"]) in parents]

    async def load(self, request_id):
        return copy.deepcopy(receipts.get(request_id))

    async def inbox(key):
        assert key.hex == PARENT_RECEIPT
        return (await c.service.collaboration.inbox.ticket_context(c.delivery))[0]

    monkeypatch.setattr("firefly_forge.run_creation_requests.RunCreationRequests._load", load)
    c.service.store.get_run = current
    c.service.store.run_creation_records.checkpoint_children = children
    c.service.collaboration.inbox.find_by_id = inbox
    c.service.collaboration.inbox.conversation_context = c.service.collaboration.inbox.ticket_context
    c.service.collaboration.deployments = None
    c.scope.update(binding_digest="c" * 64)
    c.delivery["command"] = addressed_status("@forge what is the status?").model_dump(mode="json")
    return runs, receipts


async def test_failed_grandchild_current_and_resolves_through_authenticated_root(lineage, monkeypatch):
    c = lineage
    family(c, monkeypatch)
    refs, runs = await c.service.references(c.delivery, [c.scope])
    target = next(r for r in refs if r["run_id"] == GRAND)
    assert target["parent_receipt_id"] == PARENT_RECEIPT
    assert (await c.service.resolve_reference(target, c.delivery, [c.scope]))["id"] == PARENT_RECEIPT
    direct = await c.service.selection(c.delivery, [c.scope])
    natural = await SCMNaturalTasks(c.service.collaboration)._context(c.delivery, [c.scope])
    assert direct["selected_run_id"] == natural["status_context"]["selected_run_id"] == GRAND


@pytest.mark.parametrize("change", ["scope", "source", "forged_parent", "revoked", "stale"])
async def test_grandchild_cannot_bypass_native_chain_or_current_reader(lineage, monkeypatch, change):
    c = lineage
    r, receipts = family(c, monkeypatch)
    refs, _ = await c.service.references(c.delivery, [c.scope])
    if change == "scope":
        r[GRAND]["project_id"] = "0" * 32
    elif change == "source":
        r[GRAND]["source_checkpoint_snapshot"]["revision"] = "0" * 40
    elif change == "forged_parent":
        receipts[REQUEST]["source_checkpoint"]["reference"]["run_id"] = "0" * 32
    elif change == "revoked":
        c.state["read_allowed"] = False
    else:
        receipts[CREATION]["digest"] = "0" * 64
    if change == "stale":
        old = next(x for x in refs if x["run_id"] == GRAND)
        with pytest.raises((AccessDenied, ValueError)):
            await c.service.resolve_reference(old, c.delivery, [c.scope])
    else:
        _, seen = await c.service.references(c.delivery, [c.scope])
        assert GRAND not in seen


@pytest.mark.parametrize("tie,active", [(False, False), (True, False), (False, True)])
async def test_native_admission_order_and_active_or_equal_latest_ambiguity(lineage, monkeypatch, tie, active):
    c = lineage
    r, receipts = family(c, monkeypatch)
    rival = "4" * 32
    creation = "5" * 32
    r[rival] = {**copy.deepcopy(r[GRAND]), "id": rival}
    receipts[creation] = {
        **copy.deepcopy(receipts[REQUEST]),
        "request_id": creation,
        "run_id": rival,
        "admitted_at": "2026-10-25T11:00:00Z" if tie else "2026-10-25T12:00:00Z",
    }
    if active:
        r[GRAND]["status"] = r[rival]["status"] = "queued"
    selected = await c.service.selection(c.delivery, [c.scope])
    natural = await SCMNaturalTasks(c.service.collaboration)._context(c.delivery, [c.scope])
    assert selected["selection"] == natural["status_context"]["selection"] == ("ambiguous" if tie or active else "run")
    assert selected["selected_run_id"] == (None if tie or active else rival)


async def test_cycle_never_duplicates_or_replaces_authenticated_chain(lineage, monkeypatch):
    c = lineage
    r, receipts = family(c, monkeypatch)
    fake = {**copy.deepcopy(receipts[REQUEST]), "request_id": "e" * 32, "run_id": PARENT}
    fake["source_checkpoint"]["reference"]["run_id"] = GRAND
    receipts["e" * 32] = fake
    refs, runs = await c.service.references(c.delivery, [c.scope])
    assert set(runs) == {PARENT, CHILD, GRAND} and len(refs) == 3 and c.state["queries"] <= 4


async def test_recursive_child_lookup_overflow_refuses_without_truncation(lineage, monkeypatch):
    c = lineage
    family(c, monkeypatch)

    async def overflow(*_):
        return [UUID(int=n) for n in range(201)]

    c.service.store.run_creation_records.checkpoint_children = overflow
    with pytest.raises(AccessDenied):
        await c.service.references(c.delivery, [c.scope])


async def test_historical_qualified_reference_remains_resolvable_after_new_descendant(lineage, monkeypatch):
    c = lineage
    _, receipts = family(c, monkeypatch)
    later = receipts.pop(REQUEST)
    refs, _ = await c.service.references(c.delivery, [c.scope])
    old = next(r for r in refs if r["run_id"] == CHILD)
    receipts[REQUEST] = later
    assert (await c.service.resolve_reference(old, c.delivery, [c.scope]))["id"] == PARENT_RECEIPT
    assert (await c.service.selection(c.delivery, [c.scope]))["selected_run_id"] == GRAND


async def test_terminal_descendant_does_not_hide_active_ancestor(lineage, monkeypatch):
    c = lineage
    family(c, monkeypatch)
    c.parent["status"] = "running"
    selected = await c.service.selection(c.delivery, [c.scope])
    natural = await SCMNaturalTasks(c.service.collaboration)._context(c.delivery, [c.scope])
    assert selected["selected_run_id"] == natural["status_context"]["selected_run_id"] == PARENT


async def test_active_grandchild_supersedes_ancestor_through_terminal_intermediate(lineage, monkeypatch):
    c = lineage
    runs, _ = family(c, monkeypatch)
    c.parent["status"] = runs[GRAND]["status"] = "running"
    assert (await c.service.selection(c.delivery, [c.scope]))["selected_run_id"] == GRAND


async def test_expired_root_authority_cannot_be_replaced_by_native_descendant(lineage, monkeypatch):
    c = lineage
    family(c, monkeypatch)
    c.state["allowed"] = False
    refs, runs = await c.service.references(c.delivery, [c.scope])
    assert not refs and not runs and c.state["queries"] == 0


async def test_noncanonical_admission_timestamp_never_becomes_current(lineage, monkeypatch):
    c = lineage
    _, receipts = family(c, monkeypatch)
    receipts[REQUEST]["admitted_at"] = "untrusted-time"
    refs, runs = await c.service.references(c.delivery, [c.scope])
    assert GRAND not in runs
