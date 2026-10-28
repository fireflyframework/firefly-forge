# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional native pre-admission refusal; only the database owner executes these helpers."""

from uuid import uuid4

import httpx2
from pydantic_ai import models
from test_invalid_read_recovery_host import recovery_input
from test_invalid_read_unclaimed_recovery_native import failed

from firefly_forge.provider_rejection_models import RejectedBoundaryRecoveryInputV1, ReviewedProviderRejectionV1


async def seed_claimed_refusal(c):
    request_id = await failed(c)
    candidate = await c.service.recoveries.candidate(c.run_id, request_id)
    await c.service.recoveries.request(c.run_id, request_id, uuid4().hex, recovery_input(candidate))
    provider = c.engine.provider_factory("bridge")
    client = provider.model().client._client
    original = client._transport

    def refuse(request):
        if request.method == "GET":
            return original.handler(request)
        c.posts.append(request.content)
        return httpx2.Response(400, json={"error": {"type": "invalid_request_error", "message": "Fictional refusal"}})

    try:
        client._transport = httpx2.MockTransport(refuse)
        with models.override_allow_model_requests(True):
            await c.service.execute(request_id)
    finally:
        client._transport = original
    ticket = await c.service.get(c.run_id, request_id)
    assert ticket["status"] == "needs_reconciliation" and ticket["generation"] and ticket["owner_pid"]
    assert len(c.posts) == 2 and len(await c.journal.sandbox_executions(c.run_id)) == 11
    return ticket


async def reviewed_rejection(c, ticket):
    entries = await c.journal.entries(c.run_id)
    intent, checkpoint = entries[-2:]
    descriptor = intent["payload"]["descriptor"]
    return ReviewedProviderRejectionV1(
        version=1,
        provider="bridge",
        classification="request_refused_before_admission",
        completion_request_id=intent["idempotency_key"],
        intent_digest=intent["digest"],
        checkpoint_record_digest=checkpoint["digest"],
        wire_sha256=descriptor["wire_sha256"],
        wire_bytes=descriptor["wire_bytes"],
        endpoint=descriptor["endpoint"],
        principal_id=descriptor["principal_id"],
        model=descriptor["model"],
        executed_epoch=dict(
            executable_sha256="1" * 64, source_revision="2" * 40, source_tree="3" * 40, attestation_sha256="4" * 64
        ),
        validation_epoch=dict(
            executable_sha256="1" * 64,
            source_revision="2" * 40,
            source_tree="3" * 40,
            validator_sha256="5" * 64,
            route_sha256="6" * 64,
            report_sha256="7" * 64,
            binary_probe_report_sha256="8" * 64,
        ),
        http_status=400,
        admissions=0,
        jobs=0,
        provider_dispatches=0,
        tool_callbacks=0,
    )


def rejection_input(candidate, evidence):
    return RejectedBoundaryRecoveryInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        evidence=evidence,
        valid_for_seconds=180,
        acknowledge_reviewed_provider_epoch=True,
        acknowledge_no_original_response_or_tool_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )
