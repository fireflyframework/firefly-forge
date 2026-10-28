# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional consumed pure read; no actual provider or original-state access."""

import json
from uuid import uuid4

import httpx2
from provider_rejection_native_fixtures import rejection_input, reviewed_rejection, seed_claimed_refusal
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import result

from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
from firefly_forge.readonly_observation_models import ReadonlyObservationRecoveryInputV1
from firefly_forge.run_evidence import EvidenceUnavailable, RunEvidence


def enable_observation(c):
    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(
            version=1,
            enabled=True,
            max_window_seconds=180,
            allow_pre_admission_recovery=True,
            allow_readonly_observation_recovery=True,
        ),
    )


def observation_input(candidate):
    return ReadonlyObservationRecoveryInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        valid_for_seconds=180,
        acknowledge_unavailable_observation=True,
        acknowledge_no_original_callback_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )


async def seed_observation_failure(c, monkeypatch):
    with models.override_allow_model_requests(True):
        ticket = await seed_claimed_refusal(c)
        enable_observation(c)
        proof = await reviewed_rejection(c, ticket)
        candidate = await c.service.rejections.candidate(c.run_id, ticket["id"], proof)
        await c.service.rejections.request(c.run_id, ticket["id"], uuid4().hex, rejection_input(candidate, proof))
        client = c.engine.provider_factory("bridge").model().client._client
        original = client._transport
        c.missing_observations = []

        async def missing(self, run_id, query, *, task_id):
            c.missing_observations.append(query.model_dump())
            raise EvidenceUnavailable("evidence_integrity")

        def transport(request):
            if request.method == "GET":
                return original.handler(request)
            c.posts.append(request.content)
            body = result(tool=True)
            body["choices"][0]["message"]["tool_calls"] = [
                {
                    "id": "retained-source-list",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": "{}"},
                },
                {
                    "id": "unavailable-evidence",
                    "type": "function",
                    "function": {
                        "name": "read_run_evidence",
                        "arguments": json.dumps({"query": {"kind": "journal", "limit": 2}}),
                    },
                },
            ]
            return httpx2.Response(200, json=body)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(RunEvidence, "for_worker", missing)
                client._transport = httpx2.MockTransport(transport)
                await c.service.execute(ticket["id"])
        finally:
            client._transport = original
        failed = await c.service.get(c.run_id, ticket["id"])
        assert failed["status"] == "needs_reconciliation" and len(c.missing_observations) == 1
        assert len(c.posts) == 3 and len(await c.requests.events(ticket["id"])) == 8
        return failed
