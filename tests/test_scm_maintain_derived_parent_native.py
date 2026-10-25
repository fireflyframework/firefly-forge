# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Store/creation lineage with controlled SCM and no model or provider dispatch."""

import copy
from uuid import UUID

import pytest
from checkpoint_test_native_fixtures import checkpoint_native as checkpoint_native  # noqa: F401
from scm_polling_native_fixtures import native_poll as native_poll  # noqa: F401
from test_scm_maintain_ticket_native import maintain_ticket as maintain_ticket  # noqa: F401

from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.project_access import AccessDenied
from firefly_forge.run_creation_persistence import ForgeRunCreationRequestRepository
from firefly_forge.scm_conversations import SCMConversations, observation_cut


async def retained_lineage(values):
    c, child, creation, observed, _, _ = values
    conversations = SCMConversations(c.case.collaboration)
    scopes = await conversations.scopes(observed, remote=False)
    references, _ = await conversations.references(observed, scopes, durable=True)
    reference = next(value for value in references if value["run_id"] == child["id"])
    assert reference["kind"] == "native_checkpoint_maintain"
    assert reference["receipt_id"] == creation["request_id"]
    assert reference["parent_receipt_id"] != reference["receipt_id"]
    return conversations, scopes, reference


def test_native_derived_parent_resolves_canonical_creation_and_distinct_scm_receipt(maintain_ticket):
    c, child, creation, observed, _, _ = maintain_ticket
    before = copy.deepcopy(c.case.state["posts"])

    async def verify():
        conversations, scopes, reference = await retained_lineage(maintain_ticket)
        parent = await conversations.resolve_reference(reference, observed, scopes)
        assert parent["id"] == reference["parent_receipt_id"]
        assert parent["run_id"] == child["source_checkpoint_snapshot"]["reference"]["run_id"]
        assert reference["creator_user_id"] == creation["original_actor"]["user_id"]
        expected = await c.case.collaboration.receipt(reference["parent_receipt_id"])
        assert observation_cut(parent) == observation_cut(expected)
        assert await c.case.store.get_run(child["id"]) == child

    c.case.http.portal.call(verify)
    assert c.case.state["posts"] == before


def test_native_derived_parent_rejects_forged_creation_reference(maintain_ticket):
    c, child, _, observed, _, _ = maintain_ticket
    before = copy.deepcopy(c.case.state["posts"])

    async def verify():
        conversations, scopes, reference = await retained_lineage(maintain_ticket)
        async with c.case.store._unit():
            records = ForgeRunCreationRequestRepository()
            original = copy.deepcopy((await records.find_by_id(UUID(reference["receipt_id"]))).data)
        for field, changed in (("context_digest", "f" * 64), ("creator_user_id", "f" * 32)):
            assert reference[field] != changed
            forged = {**reference, field: changed}
            with pytest.raises(AccessDenied, match="provenance changed"):
                await conversations.resolve_reference(forged, observed, scopes)
        async with c.case.store._unit():
            assert (await records.find_by_id(UUID(reference["receipt_id"]))).data == original
        assert await c.case.store.get_run(child["id"]) == child

    c.case.http.portal.call(verify)
    assert c.case.state["posts"] == before


def test_native_derived_parent_rechecks_current_reader_before_resolving(maintain_ticket):
    c, child, _, observed, _, _ = maintain_ticket
    before = copy.deepcopy(c.case.state["posts"])

    async def verify():
        conversations, scopes, reference = await retained_lineage(maintain_ticket)
        async with c.case.store._unit():
            users = ForgeIdentityUserRepository()
            user = await users.find_by_id(UUID(scopes[0]["native_user_id"]))
            user.enabled = False
            await users.save(user)
        with pytest.raises(AccessDenied, match="provenance changed"):
            await conversations.resolve_reference(reference, observed, scopes)
        assert await c.case.store.get_run(child["id"]) == child

    c.case.http.portal.call(verify)
    assert c.case.state["posts"] == before
