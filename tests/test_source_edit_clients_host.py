# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""An explicit strict activation is submitted once and only read back after uncertainty."""

import json

import httpx
import pytest
from test_original_recovery_sdk import connection

RUN, REQUEST = "a" * 32, "b" * 32
BODY = {
    "version": 1,
    "request_id": REQUEST,
    "candidate_digest": "c" * 64,
    "acknowledge_environment_only": True,
    "acknowledge_resume_existing_task": True,
    "acknowledge_existing_read_scope": True,
    "acknowledge_no_commands": True,
}


def test_source_edit_sdk_preserves_explicit_scope_and_exact_readback():
    calls = []

    def reply(request):
        calls.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json={"receipt": BODY})

    with connection(reply) as client:
        assert hasattr(client, "source_edit_candidate")
        assert client.source_edit_candidate(RUN) == {"receipt": BODY}
        assert client.activate_source_edit(RUN, BODY) == {"receipt": BODY}
        assert client.source_edit_continuation(RUN, REQUEST) == {"receipt": BODY}
        with pytest.raises(ValueError):
            client.activate_source_edit(RUN, BODY | {"command_limit": 99})
    assert [(m, p) for m, p, _ in calls] == [
        ("GET", f"/api/v1/runs/{RUN}/source-edit-continuations/candidate"),
        ("POST", f"/api/v1/runs/{RUN}/source-edit-continuations"),
        ("GET", f"/api/v1/runs/{RUN}/source-edit-continuations/{REQUEST}"),
    ]
    assert json.loads(calls[1][2]) == BODY
