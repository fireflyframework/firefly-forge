# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Complete, rehashed fictional custody still cannot turn a refusal or tool turn into final output."""

import copy
import hashlib
import json

import pytest
from test_known_final_outcome_host import consumed_normal_result

from firefly_forge.journal import Journal, digest
from firefly_forge.known_final_outcome_proof import prepare_final_outcome


@pytest.mark.parametrize("kind", ["refusal", "tool_calls", "empty_choices"])
async def test_known_final_requires_a_real_nonrefusal_final_report(tmp_path, monkeypatch, kind):
    value = await consumed_normal_result(tmp_path, monkeypatch)
    entries = copy.deepcopy(value["entries"])
    response, consume = entries[-2:]
    assert response["kind"] == "bridge_request_response" and consume["kind"] == "agent_consume"
    raw = json.loads(response["payload"]["response"])
    if kind == "empty_choices":
        raw["choices"] = []
    elif kind == "refusal":
        raw["choices"][0]["message"].update(refusal="Controlled refusal", content=None)
    else:
        raw["choices"][0]["finish_reason"] = "tool_calls"
        raw["choices"][0]["message"].update(
            content=None,
            tool_calls=[
                dict(
                    id="unconsumed-next-tool",
                    type="function",
                    function=dict(name="read_file", arguments='{"path":"service.py"}'),
                )
            ],
        )
    encoded = json.dumps(raw, separators=(",", ":"))
    size, sha = len(encoded.encode()), hashlib.sha256(encoded.encode()).hexdigest()
    response["payload"].update(response=encoded, response_bytes=size, response_sha256=sha)
    response["digest"] = digest({k: v for k, v in response.items() if k != "digest"})
    consume["previous_hash"] = response["digest"]
    consume["payload"].update(response_record_digest=response["digest"], response_bytes=size, response_sha256=sha)
    consume["digest"] = digest({k: v for k, v in consume.items() if k != "digest"})
    assert Journal.verify_chain(entries), "The negative must exercise semantic custody, not merely a broken hash"
    with pytest.raises(ValueError):
        await prepare_final_outcome(value["run"], value["task"], entries)
    assert len(value["environment"].posts) == 2
