# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native peer scoping and late-publication selection; requires isolated PostgreSQL."""

import uuid

from test_collaboration import configured

from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity
from firefly_forge.store import identifier


async def test_native_feedback_peer_scope_and_post_success_publication_watch(store):
    service, repo = await configured(store)
    run_id = uuid.uuid4().hex
    selected = []
    async with store._unit():
        for number, kind, run, pending in [
            (12, "issue", run_id, False),
            (12, "issue", run_id, True),
            (13, "issue", run_id, True),
            (12, "pull_request", run_id, True),
            (12, "issue", uuid.uuid4().hex, True),
        ]:
            value = uuid.uuid4()
            data = dict(
                id=value.hex,
                status="processed",
                repository_id=repo["id"],
                provider="github",
                run_id=run,
                issue=dict(kind=kind, number=number),
                feedback_status="sent",
                feedback_stage="succeeded",
                feedback_publication_pending=pending,
            )
            await service.inbox.save(
                ForgeWebhookInboxEntity(
                    id=value,
                    repository_id=identifier(repo["id"]),
                    provider="github",
                    delivery_id=value.hex,
                    source_key=value.hex,
                    status="processed",
                    data=data,
                )
            )
            selected.append(data)
    async with store._unit():
        peers = await service.inbox.feedback_context(selected[0])
        pending = await service.inbox.find_pending()
    assert [row.id.hex for row in peers] == [selected[1]["id"], selected[0]["id"]]
    assert selected[1]["id"] in {row.id.hex for row in pending}
    assert selected[0]["id"] not in {row.id.hex for row in pending}
