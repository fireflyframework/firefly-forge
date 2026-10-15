# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Observation requests use the actual native service graph and shared authority."""

import asyncio
from uuid import uuid4

from starlette.testclient import TestClient
from test_learning_observation_clients import canon_reads as canon_reads_fixture
from test_learning_observation_receipts import admitted_projection

from firefly_forge.api import create_application
from firefly_forge.engine import Engine
from firefly_forge.learning import LearningLedger
from firefly_forge.learning_controllers import LearningController
from firefly_forge.learning_observation_attempts import LearningObservationAttempts
from firefly_forge.learning_projection import LearningProjection
from firefly_forge.store import Store

canon_reads = canon_reads_fixture


def test_native_observation_routes_use_the_injected_service_graph(tmp_path, postgres_schema, monkeypatch, canon_reads):
    async def idle(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_poll", idle)
    token = "owned-native-observation-di-token-32chars"
    with TestClient(
        create_application(tmp_path / "api", token, role="combined"),
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 52000),
    ) as http:
        context = http.app.state.runtime.context
        attempts = context.get_bean(LearningObservationAttempts)
        controller = context.get_bean(LearningController)
        projection = context.get_bean(LearningProjection)
        assert controller.attempts is attempts
        assert attempts.projection is projection
        assert attempts.store is context.get_bean(Store) is http.app.state.store
        assert attempts.ledger is context.get_bean(LearningLedger) is projection.ledger

        async def seed():
            return await admitted_projection(http.app.state.store, tmp_path, remote_service=canon_reads)

        _, record, _ = http.portal.call(seed)
        status_response = http.get(f"learning/records/{record['id']}/projection")
        assert status_response.status_code == 200
        status = status_response.json()
        body = {
            "attempt_id": "obs2_" + uuid4().hex,
            "source_id": "owned-source",
            "projection_id": status["id"],
            "submission_request_id": status["request_id"],
            "workspace_id": status["workspace_id"],
            "target_revision": status["target"]["revision"],
            "payload_digest": status["payload_digest"],
            "content_sha256": status["content_sha256"],
        }
        observed = []
        original_observe, original_receipt = attempts.observe, attempts.receipt

        async def observe(record_id, request):
            observed.append("observe")
            return await original_observe(record_id, request)

        async def receipt(record_id, attempt_id):
            observed.append("receipt")
            return await original_receipt(record_id, attempt_id)

        monkeypatch.setattr(attempts, "observe", observe)
        monkeypatch.setattr(attempts, "receipt", receipt)
        result = http.post(f"learning/records/{record['id']}/projection/observation-attempts", json=body)
        assert result.status_code == 200, result.text
        assert result.json()["state"] == "verified"
        exact = http.get(f"learning/records/{record['id']}/projection/observation-attempts/{body['attempt_id']}")
        assert exact.status_code == 200 and exact.json() == result.json()
        assert observed == ["observe", "receipt", "receipt"]
        assert canon_reads["calls"] == [("GET", "/api/v1/sources/owned-source"), ("POST", "/api/v1/search")]
        assert not canon_reads["errors"]
