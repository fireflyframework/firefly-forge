# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Queued checkpoint workers inherit lifecycle context, never the API caller's principal."""

from uuid import UUID

from test_git_delivery_local import delivery_case as delivery_case
from test_git_delivery_local import git
from test_git_delivery_operational import operational as operational
from test_git_failed_unit_adoption_native import execute_as_background_owner

from firefly_forge.git_delivery_models import GitDeliveryInputV1
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


async def test_background_owner_retains_actor_revocation_without_inheriting_api_reader(operational):
    c = operational
    body = GitDeliveryInputV1(
        version=1, request_id="a" * 32, expected_head=c.base, expected_source_tree=git(c.root, "write-tree")
    )
    await c.controller.request("1" * 32, body)
    caller = CURRENT_PRINCIPAL.get()
    actor = c.rows[UUID("a" * 32)].data["request"]["actor"].copy()
    read = c.service.journal.entries
    readers = []

    async def protected_reader(run_id):
        principal = CURRENT_PRINCIPAL.get()
        readers.append(principal)
        if principal is not None:
            raise AccessDenied("The API caller is suspended")
        return await read(run_id)

    c.service.journal.entries = protected_reader
    c.state["allowed"] = False
    await execute_as_background_owner(c.requests.git_deliveries, body.request_id)
    row = c.rows[UUID("a" * 32)]
    assert row.status == "failed" and row.data["request"]["actor"] == actor
    assert readers and all(principal is None for principal in readers)
    assert CURRENT_PRINCIPAL.get() is caller
    assert git(c.root, "rev-parse", "HEAD") == c.base and not c.state["entries"]
    assert (await c.requests.events(body.request_id))[-1]["kind"] == "worker_failed"
