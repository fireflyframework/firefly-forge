# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native creation provenance must resolve to its distinct authenticated SCM parent."""

import copy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_scm_maintain_ticket_host import body, prepared_binding
from test_scm_polling import poll_case as poll_case

from firefly_forge.journal import digest
from firefly_forge.project_access import AccessDenied
from firefly_forge.run_creation_requests import RunCreationRequests
from firefly_forge.scm_maintain_ticket_models import MaintainTicketInput
from firefly_forge.scm_maintain_tickets import MaintainTickets


async def nested_binding(c, monkeypatch):
    service, first = await prepared_binding(c, monkeypatch)
    parent = c.db["runs"][first.run_id].data
    parent_creation = await service._creation(first.creation_request_id)
    parent_creation["original_actor"] = {"user_id": c.user_id}
    parent_creation["digest"] = digest({k: v for k, v in parent_creation.items() if k != "digest"})
    child_id, creation_id = uuid4().hex, uuid4().hex
    reference = {**parent["source_checkpoint"], "run_id": parent["id"], "request_id": uuid4().hex}
    source = {**copy.deepcopy(parent["source_checkpoint_snapshot"]), "reference": reference}
    source["digest"] = digest({k: v for k, v in source.items() if k != "digest"})
    child = {
        **copy.deepcopy(parent),
        "id": child_id,
        "source_checkpoint": reference,
        "source_checkpoint_snapshot": source,
    }
    c.db["runs"][child_id] = SimpleNamespace(
        id=UUID(child_id), repository_id=UUID(c.repository), status="queued", data=child
    )
    creation = {
        **copy.deepcopy(parent_creation),
        "request_id": creation_id,
        "run_id": child_id,
        "source_checkpoint": source,
        "input": {"operation": "maintain", "publish": False, "source_checkpoint": reference},
    }
    creation["digest"] = digest({k: v for k, v in creation.items() if k != "digest"})
    creations = {parent_creation["request_id"]: parent_creation, creation_id: creation}

    async def load(self, request_id):
        return copy.deepcopy(creations.get(request_id))

    async def children(parents, repository):
        assert repository == UUID(c.repository)
        return [UUID(parent_creation["request_id"])] if UUID(c.run_id) in parents else []

    monkeypatch.setattr(RunCreationRequests, "_load", load)
    monkeypatch.setattr(MaintainTickets, "_creation", load)
    c.store.run_creation_records = SimpleNamespace(checkpoint_children=children)
    request = MaintainTicketInput.model_validate(
        {
            **body(),
            "request_id": uuid4().hex,
            "run_id": child_id,
            "creation_request_id": creation_id,
            "expected_creation_digest": creation["digest"],
            "observed_receipt_id": first.observed_receipt_id,
            "expected_reference_digest": first.expected_reference_digest,
        }
    )
    c.state["derived_parent_posts"] = copy.deepcopy(c.state["posts"])
    return service, request, creations, parent


async def test_native_derived_parent_configures_and_revalidates_without_treating_creation_as_inbox(
    poll_case, monkeypatch
):
    async with poll_case() as c:
        service, request, creations, parent = await nested_binding(c, monkeypatch)
        before = copy.deepcopy(parent)
        kept = await service.configure(c.project, c.repository, request)
        ref = kept["parent_reference"]
        assert ref["kind"] == "native_checkpoint_maintain"
        assert ref["receipt_id"] in creations and ref["receipt_id"] not in c.db["inbox"]
        assert ref["parent_receipt_id"] == kept["parent_observation"]["id"]
        anchor = await c.collaboration.receipt(kept["outbox_id"])
        assert (await service.current(anchor))["run"]["id"] == request.run_id
        assert c.db["runs"][parent["id"]].data == before
        assert c.state["posts"] == c.state["derived_parent_posts"]


@pytest.mark.parametrize("change", ["digest", "creator", "source", "scope"])
async def test_derived_parent_provenance_drift_refuses_current_notification(poll_case, monkeypatch, change):
    async with poll_case() as c:
        service, request, creations, parent = await nested_binding(c, monkeypatch)
        kept = await service.configure(c.project, c.repository, request)
        creation = creations[kept["parent_reference"]["receipt_id"]]
        if change == "digest":
            creation["digest"] = "f" * 64
        elif change == "creator":
            creation["original_actor"]["user_id"] = "f" * 32
        elif change == "source":
            c.db["runs"][parent["id"]].data["source_checkpoint_snapshot"]["revision"] = "f" * 40
        else:
            creation["component_id"] = "other"
        anchor = await c.collaboration.receipt(kept["outbox_id"])
        with pytest.raises((AccessDenied, ValueError)):
            await service.current(anchor)
        assert c.state["posts"] == c.state["derived_parent_posts"]


async def test_newer_ticket_reference_does_not_replace_the_bound_native_parent(poll_case, monkeypatch):
    async with poll_case() as c:
        service, request, _, _ = await nested_binding(c, monkeypatch)
        kept = await service.configure(c.project, c.repository, request)
        previous = copy.deepcopy(kept["parent_reference"])
        await c.new_command("status", number=102)
        anchor = await c.collaboration.receipt(kept["outbox_id"])
        context = await service.current(anchor)
        assert context["binding"]["parent_reference"] == previous
        assert context["run"]["id"] == request.run_id
