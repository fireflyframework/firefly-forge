# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""SDK recovery calls preserve explicit identities and never choose a follow-up write."""

import copy
import json
from contextlib import contextmanager

import httpx
import pytest

from firefly_forge.client import ForgeClient

RUN, REQUEST, REPOSITORY, PROJECT, ACTOR = (character * 32 for character in "abcde")
COMPLETION = "fbr1_" + "f" * 32
ENVELOPE = {"version": 1, "input": {"repository_id": REPOSITORY, "brief": "A fictional product"}}
CANCEL = {
    "version": 1,
    "request_digest": "1" * 64,
    "repository_id": REPOSITORY,
    "project_id": PROJECT,
    "component_id": "api",
}
SOURCE = {"kind": "http_response", "record_digest": "2" * 64}
RESTORE = {
    "request_id": REQUEST,
    "expected_checkpoint_digest": "3" * 64,
    "expected_intent_digest": "4" * 64,
    "source": SOURCE,
}
RECEIPT = {
    "version": 1,
    "request_id": REQUEST,
    "original_actor": {"user_id": ACTOR, "authentication_method": "oidc_bearer"},
    "request_digest": "1" * 64,
    "input": ENVELOPE["input"],
    "repository_id": REPOSITORY,
    "project_id": None,
    "component_id": None,
    "run_id": RUN,
    "admitted_at": "2026-10-17T22:00:00Z",
    "admission_digest": "5" * 64,
    "digest": "6" * 64,
}
DECISION = {"version": 1, "outcome": "admitted", "receipt": RECEIPT}
CANCELLED_DECISION = {
    "version": 1,
    "outcome": "cancelled",
    "cancellation": {
        **CANCEL,
        "request_id": REQUEST,
        "original_actor": {"user_id": ACTOR, "authentication_method": "oidc_bearer"},
        "cancelled_by": {"user_id": ACTOR, "authentication_method": "oidc_session"},
        "cancelled_at": "2026-10-17T22:00:01Z",
        "digest": "0" * 64,
    },
}
TICKET = {
    "id": REQUEST,
    "run_id": RUN,
    "kind": "agent_restore",
    "status": "queued",
    "request": {
        "run_id": RUN,
        "completion_request_id": COMPLETION,
        "input": RESTORE,
        "actor": {"user_id": ACTOR, "authentication_method": "oidc_bearer"},
    },
    "request_digest": "7" * 64,
    "generation": None,
    "result": None,
    "created_at": "2026-10-17T22:00:00Z",
    "updated_at": "2026-10-17T22:00:00Z",
}


@contextmanager
def connection(handler):
    with ForgeClient("https://factory.invalid", "retained-caller-token") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="https://factory.invalid/api/v1/",
            headers={"Authorization": "Bearer retained-caller-token"},
            transport=httpx.MockTransport(handler),
        )
        yield client


def test_original_creation_preserves_wire_presence_caller_and_immutable_receipt_content():
    calls = []
    receipt_bytes = json.dumps(RECEIPT, sort_keys=True, separators=(",", ":")).encode()

    def handle(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer retained-caller-token"
        return httpx.Response(201 if request.method == "POST" else 200, content=receipt_bytes)

    original = copy.deepcopy(ENVELOPE)
    with connection(handle) as client:
        created = client.request_run_creation(REQUEST, original)
        read = client.run_creation_request(REQUEST)
    assert original == ENVELOPE
    assert created == read == RECEIPT
    assert json.dumps(created, sort_keys=True, separators=(",", ":")).encode() == receipt_bytes
    assert [(call.method, call.url.path) for call in calls] == [
        ("POST", f"/api/v1/run-creation-requests/{REQUEST}"),
        ("GET", f"/api/v1/run-creation-requests/{REQUEST}"),
    ]
    assert json.loads(calls[0].content) == ENVELOPE
    assert calls[1].content == b""


def test_typed_creation_keeps_explicit_empty_skills_without_materializing_other_defaults():
    from firefly_forge.run_creation_models import RunCreationInput

    calls = []
    body = {"version": 1, "input": {**ENVELOPE["input"], "skills": []}}
    model = RunCreationInput.model_validate(body)
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=RECEIPT)) as client:
        client.request_run_creation(REQUEST, model)
    assert json.loads(calls[0].content) == body
    assert model.model_dump(exclude_unset=True) == body


@pytest.mark.parametrize("decision", [DECISION, CANCELLED_DECISION])
def test_cancellation_is_one_explicit_digest_scoped_write_and_decision_is_a_separate_read(decision):
    calls = []
    original = copy.deepcopy(CANCEL)

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=decision)

    with connection(handle) as client:
        assert client.cancel_run_creation_request(REQUEST, original) == decision
        assert client.run_creation_decision(REQUEST) == decision
    assert original == CANCEL
    assert [(call.method, call.url.path) for call in calls] == [
        ("POST", f"/api/v1/run-creation-requests/{REQUEST}/cancel"),
        ("GET", f"/api/v1/run-creation-requests/{REQUEST}/decision"),
    ]
    assert json.loads(calls[0].content) == CANCEL
    assert calls[1].content == b""


@pytest.mark.parametrize(
    "source",
    [
        SOURCE,
        {
            "kind": "bridge_observation",
            "record_digest": "2" * 64,
            "observation_id": "8" * 32,
            "terminal_digest": "9" * 64,
        },
    ],
)
def test_discovery_restore_and_exact_readback_keep_selected_references_and_caller(source):
    calls = []
    body = {**RESTORE, "source": source}
    original = copy.deepcopy(body)
    ticket = copy.deepcopy(TICKET)
    ticket["request"]["input"] = body
    candidates = {
        "version": 1,
        "actor_id": ACTOR,
        "run_id": RUN,
        "repository_id": REPOSITORY,
        "project_id": PROJECT,
        "state": "available",
        "items": [
            {
                "completion_id": COMPLETION,
                "checkpoint_digest": "3" * 64,
                "intent_digest": "4" * 64,
                "task": "implement",
                "attempt": 1,
                "ordinal": 2,
                "deadline_ms": 1890000000000,
                "created_at": "2026-10-17T22:00:00Z",
                "state": "ready",
                "result": source,
                "observation": None,
                "ticket": None,
            }
        ],
    }

    def handle(request):
        assert request.headers["Authorization"] == "Bearer retained-caller-token"
        calls.append(request)
        return httpx.Response(200, json=candidates if request.url.path.endswith("/candidates") else ticket)

    with connection(handle) as client:
        assert client.agent_checkpoint_candidates(RUN) == candidates
        assert len(calls) == 1
        assert client.request_agent_checkpoint_restore(RUN, COMPLETION, original) == ticket
        assert client.agent_checkpoint_restore(RUN, COMPLETION, REQUEST) == ticket
    assert original == body
    prefix = f"/api/v1/runs/{RUN}/agent-checkpoints"
    assert [(call.method, call.url.path) for call in calls] == [
        ("GET", prefix + "/candidates"),
        ("POST", prefix + f"/{COMPLETION}/restores"),
        ("GET", prefix + f"/{COMPLETION}/restores/{REQUEST}"),
    ]
    assert calls[0].content == calls[2].content == b""
    assert json.loads(calls[1].content) == body


OPERATIONS = [
    ("request_run_creation", (REQUEST, ENVELOPE), "POST"),
    ("run_creation_request", (REQUEST,), "GET"),
    ("cancel_run_creation_request", (REQUEST, CANCEL), "POST"),
    ("run_creation_decision", (REQUEST,), "GET"),
    ("agent_checkpoint_candidates", (RUN,), "GET"),
    ("request_agent_checkpoint_restore", (RUN, COMPLETION, RESTORE), "POST"),
    ("agent_checkpoint_restore", (RUN, COMPLETION, REQUEST), "GET"),
]


@pytest.mark.parametrize("method,args,http_method", OPERATIONS)
@pytest.mark.parametrize("failure", ["missing", "transport"])
def test_missing_or_lost_result_never_retries_falls_back_or_submits_another_identity(
    method, args, http_method, failure
):
    calls = []

    def handle(request):
        calls.append(request)
        if failure == "transport":
            raise httpx.ReadTimeout("Fictional response lost", request=request)
        return httpx.Response(404, json={"detail": "Original result not found"})

    error = httpx.ReadTimeout if failure == "transport" else RuntimeError
    with connection(handle) as client, pytest.raises(error):
        getattr(client, method)(*args)
    assert len(calls) == 1
    assert calls[0].method == http_method
    assert (
        str(REQUEST) in calls[0].url.path
        or method == "agent_checkpoint_candidates"
        or method == "request_agent_checkpoint_restore"
    )
    assert calls[0].url.path != "/api/v1/runs"


def test_response_loss_followed_by_missing_readback_preserves_the_same_identity_until_an_explicit_read():
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("Fictional response lost", request=request)
        return httpx.Response(404, json={"detail": "Not confirmed"})

    with connection(handle) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.request_run_creation(REQUEST, ENVELOPE)
        assert len(calls) == 1
        with pytest.raises(RuntimeError, match="HTTP 404"):
            client.run_creation_decision(REQUEST)
    assert [(call.method, call.url.path) for call in calls] == [
        ("POST", f"/api/v1/run-creation-requests/{REQUEST}"),
        ("GET", f"/api/v1/run-creation-requests/{REQUEST}/decision"),
    ]


@pytest.mark.parametrize(
    "method,args",
    [
        ("request_run_creation", ("../private-marker", ENVELOPE)),
        ("run_creation_request", ("B" * 32,)),
        ("cancel_run_creation_request", ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", CANCEL)),
        ("run_creation_decision", (True,)),
        ("agent_checkpoint_candidates", ("../private-marker",)),
        ("request_agent_checkpoint_restore", (RUN, "fbr1_" + "F" * 32, RESTORE)),
        ("agent_checkpoint_restore", (RUN, COMPLETION + "/../private-marker", REQUEST)),
        ("agent_checkpoint_restore", (RUN, COMPLETION, "../private-marker")),
        ("request_run_creation", (REQUEST, {**ENVELOPE, "version": True})),
        ("request_run_creation", (REQUEST, {**ENVELOPE, "version": 1.0})),
        ("request_run_creation", (REQUEST, {**ENVELOPE, "actor": "private-marker"})),
        ("cancel_run_creation_request", (REQUEST, {**CANCEL, "request_digest": "private-marker"})),
        ("cancel_run_creation_request", (REQUEST, {**CANCEL, "component_id": None})),
        ("cancel_run_creation_request", (REQUEST, {**CANCEL, "force": True})),
        ("request_agent_checkpoint_restore", (RUN, COMPLETION, {**RESTORE, "request_id": True})),
        ("request_agent_checkpoint_restore", (RUN, COMPLETION, {**RESTORE, "version": 1})),
        (
            "request_agent_checkpoint_restore",
            (RUN, COMPLETION, {**RESTORE, "source": {**SOURCE, "response": "private-marker"}}),
        ),
        (
            "request_agent_checkpoint_restore",
            (RUN, COMPLETION, {**RESTORE, "source": {"kind": "bridge_observation", "record_digest": "2" * 64}}),
        ),
    ],
)
def test_invalid_or_forged_identity_scope_and_source_are_rejected_before_io_without_echo(method, args):
    calls = []
    with (
        connection(lambda request: calls.append(request) or httpx.Response(200, json={})) as client,
        pytest.raises(ValueError) as error,
    ):
        getattr(client, method)(*args)
    assert "private-marker" not in str(error.value)
    assert calls == []


def test_typed_cancel_and_restore_use_the_same_native_contract_and_keep_original_ids():
    from firefly_forge.agent_checkpoint_models import RestoreInput
    from firefly_forge.run_creation_models import RunCreationCancelInput

    calls = []
    cancel = RunCreationCancelInput.model_validate(CANCEL)
    restore = RestoreInput.model_validate(RESTORE)
    with connection(lambda request: calls.append(request) or httpx.Response(200, json={})) as client:
        client.cancel_run_creation_request(REQUEST, cancel)
        client.request_agent_checkpoint_restore(RUN, COMPLETION, restore)
    assert [json.loads(call.content) for call in calls] == [CANCEL, RESTORE]
    assert cancel.model_dump(exclude_unset=True) == CANCEL
    assert restore.model_dump(exclude_unset=True) == RESTORE


def test_constructed_model_cannot_bypass_exact_version_revalidation_before_io():
    from firefly_forge.run_creation_models import RunCreationInput

    calls = []
    body = RunCreationInput.model_construct(version=True, input=ENVELOPE["input"])
    with (
        connection(lambda request: calls.append(request) or httpx.Response(200, json={})) as client,
        pytest.raises(ValueError),
    ):
        client.request_run_creation(REQUEST, body)
    assert calls == []
