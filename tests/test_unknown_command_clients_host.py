# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""SDK consent and exact readback never retry or manufacture execution results."""
# ruff: noqa: F811

import json
from uuid import uuid4

import httpx
import pytest
from test_original_recovery_sdk import connection
from test_unknown_command_acknowledgement_host import (  # noqa: F401
    command_case,
    consent,
    service,
    unknown_case,
)


async def test_sdk_sends_one_exact_acknowledgement_and_reads_same_identity(unknown_case, monkeypatch):
    case = unknown_case
    api = service(case, monkeypatch)
    future = uuid4().hex
    candidate = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=future)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"review": "controlled immutable receipt"})

    request_id = uuid4().hex
    with connection(respond) as client:
        client.unknown_command_acknowledgement_candidate(
            case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=future
        )
        client.acknowledge_unknown_command(case.run_id, case.unknown.id.hex, request_id, consent(candidate))
        client.unknown_command_acknowledgement(case.run_id, case.unknown.id.hex, request_id)
        bad = {**consent(candidate).model_dump(), "exit_code": 0}
        with pytest.raises(ValueError):
            client.acknowledge_unknown_command(case.run_id, case.unknown.id.hex, request_id, bad)
    assert [request.method for request in calls] == ["GET", "POST", "GET"]
    assert calls[1].url.path == calls[2].url.path
    assert calls[0].url.params["adoption_request_id"] == future
    assert json.loads(calls[1].content) == consent(candidate).model_dump()


async def test_lost_acknowledgement_response_has_no_automatic_post_retry(unknown_case, monkeypatch):
    case = unknown_case
    api = service(case, monkeypatch)
    candidate = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    calls = []

    def lost(request):
        calls.append(request)
        raise httpx.ReadTimeout("Controlled missing acknowledgement response", request=request)

    with connection(lost) as client, pytest.raises(httpx.ReadTimeout):
        client.acknowledge_unknown_command(case.run_id, case.unknown.id.hex, uuid4().hex, consent(candidate))
    assert len(calls) == 1 and calls[0].method == "POST"
