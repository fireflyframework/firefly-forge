# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native registrar body decoding only; no store, provider, or execution effects."""

import copy
from types import SimpleNamespace

import httpx
import pytest
from pyfly.container import Container
from pyfly.web.adapters.starlette.controller import ControllerRegistrar
from starlette.applications import Starlette

from firefly_forge.controllers import OperatorErrors
from firefly_forge.known_worker_completion_controllers import KnownWorkerCompletionController, WorkerCompletionBody
from firefly_forge.known_worker_completion_models import ConsumedTerminalInputV2, WorkerCompletionInputV1
from firefly_forge.project_access import AccessDenied

RUN = "1" * 32
REQUEST = "2" * 32


def body(version):
    value = dict(
        version=version,
        candidate_digest="a" * 64,
        policy_digest="b" * 64,
        expected_run_digest="c" * 64,
        journal_digest="d" * 64,
        proof_digest="e" * 64,
        acknowledge_known_worker_output=True,
        acknowledge_no_execution_grant=True,
    )
    value.update(feedback_record_digest="f" * 64) if version == 1 else value.update(
        acknowledge_local_postprocessing=True
    )
    return value


@pytest.fixture
async def routed():
    received = []

    async def request(run_id, request_id, value):
        assert type(value) in (WorkerCompletionInputV1, ConsumedTerminalInputV2)
        if run_id != RUN or request_id != REQUEST:
            raise AccessDenied("Controlled service scope refuses this identity")
        received.append((run_id, request_id, type(value), value.model_dump(mode="json")))
        return {"version": value.version, "run_id": run_id, "request_id": request_id}

    container = Container()
    container.register_instance(
        KnownWorkerCompletionController, KnownWorkerCompletionController(SimpleNamespace(request=request))
    )
    container.register_instance(OperatorErrors, OperatorErrors())
    context = SimpleNamespace(container=container, get_bean=container.resolve)
    registrar = ControllerRegistrar()
    app = Starlette(routes=registrar.collect_routes(context))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid") as client:
        yield SimpleNamespace(client=client, received=received, context=context, registrar=registrar)


@pytest.mark.parametrize("version", [1, 2])
async def test_native_registrar_forwards_exact_typed_worker_completion_body(routed, version):
    value = body(version)
    response = await routed.client.post(f"/api/v1/runs/{RUN}/worker-completion-settlements/{REQUEST}", json=value)
    assert response.status_code == 201, response.json()
    assert response.json() == {"version": version, "run_id": RUN, "request_id": REQUEST}
    assert routed.received == [
        (RUN, REQUEST, WorkerCompletionInputV1 if version == 1 else ConsumedTerminalInputV2, value)
    ]


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "mutation", ["missing_ack", "false_ack", "integer_ack", "string_ack", "extra", "coerced_digest", "wrapped"]
)
async def test_native_registrar_rejects_invalid_consent_without_dispatch(routed, version, mutation):
    value = body(version)
    if mutation == "missing_ack":
        del value["acknowledge_no_execution_grant"]
    elif mutation == "false_ack":
        value["acknowledge_no_execution_grant"] = False
    elif mutation == "integer_ack":
        value["acknowledge_no_execution_grant"] = 1
    elif mutation == "string_ack":
        value["acknowledge_no_execution_grant"] = "true"
    elif mutation == "extra":
        value["unreviewed_grant"] = "private-fixture-sentinel"
    elif mutation == "coerced_digest":
        value["candidate_digest"] = 7
    else:
        value = {"root": value}
    response = await routed.client.post(f"/api/v1/runs/{RUN}/worker-completion-settlements/{REQUEST}", json=value)
    assert response.status_code == 422
    assert "private-fixture-sentinel" not in response.text
    assert routed.received == []


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("invalid", [None, True, 1.0, 2.0, "1", "2", 3])
async def test_native_registrar_requires_exact_supported_integer_version(routed, version, invalid):
    value = body(version)
    value["version"] = invalid
    response = await routed.client.post(f"/api/v1/runs/{RUN}/worker-completion-settlements/{REQUEST}", json=value)
    assert response.status_code == 422
    assert routed.received == []


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("run_id,request_id", [("3" * 32, REQUEST), (RUN, "4" * 32)])
async def test_native_registrar_preserves_service_scope_refusal(routed, version, run_id, request_id):
    value = copy.deepcopy(body(version))
    response = await routed.client.post(f"/api/v1/runs/{run_id}/worker-completion-settlements/{request_id}", json=value)
    assert response.status_code == 403
    assert routed.received == []


@pytest.mark.parametrize(
    "version,acknowledgment",
    [
        (1, "acknowledge_known_worker_output"),
        (1, "acknowledge_no_execution_grant"),
        (2, "acknowledge_known_worker_output"),
        (2, "acknowledge_no_execution_grant"),
        (2, "acknowledge_local_postprocessing"),
    ],
)
async def test_native_registrar_requires_each_versioned_acknowledgment(routed, version, acknowledgment):
    value = body(version)
    del value[acknowledgment]
    response = await routed.client.post(f"/api/v1/runs/{RUN}/worker-completion-settlements/{REQUEST}", json=value)
    assert response.status_code == 422
    assert routed.received == []


async def test_native_registrar_exposes_original_versioned_wire_schema(routed):
    metadata = next(
        value
        for value in routed.registrar.collect_route_metadata(routed.context)
        if value.http_method == "POST" and value.request_body_model is WorkerCompletionBody
    )
    schema = metadata.request_body_model.model_json_schema()
    assert schema["anyOf"] == [{"$ref": "#/$defs/WorkerCompletionInputV1"}, {"$ref": "#/$defs/ConsumedTerminalInputV2"}]
    for name, version in (("WorkerCompletionInputV1", 1), ("ConsumedTerminalInputV2", 2)):
        definition = schema["$defs"][name]
        assert definition["properties"]["version"]["const"] == version
        assert definition["additionalProperties"] is False
        assert "version" in definition["required"]
    assert "root" not in schema.get("properties", {})
