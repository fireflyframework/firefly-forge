# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Known final output closes custody without model replay or a free graph reset."""

import copy
import importlib
import importlib.util
import json

import httpx2
import pytest
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import activated, result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests
from test_bridge_inflight_cancel import RUN


@pytest.fixture
async def known_worker(tmp_path, monkeypatch):
    env = await activated(tmp_path, monkeypatch)
    accept_feedback = env.journal.worker_feedback

    async def historical_feedback(*args, **kwargs):
        # Reconstruct the preautomatic writer representation retained by Phase A operators.
        env.store.run.data.get("usage", {}).pop("runtime_report", None)
        return await accept_feedback(*args, **kwargs)

    monkeypatch.setattr(env.journal, "worker_feedback", historical_feedback)
    posts = []
    feedback = dict(
        id="known-final-worker",
        role_id="implementer",
        task_id="implement",
        attempt=1,
        disposition="blocked",
        summary="A recorded decision is required",
        evidence_paths=[],
        request=dict(
            id="known-final-question",
            kind="other",
            question="Keep the existing value?",
            context="This local fixture retains its exact source.",
            choices=["Keep", "Review"],
        ),
    )

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        posts.append(request.content)
        assert len(posts) <= 2
        return httpx2.Response(
            200,
            json=result(tool=True)
            if len(posts) == 1
            else result(
                json.dumps(
                    {"summary": "Known completed worker response", "worker_feedback": feedback, "qa_report": None}
                )
            ),
        )

    env.model.client._client._transport = httpx2.MockTransport(transport)
    env.model.client._client._mounts = {}
    with models.override_allow_model_requests(False), mock_model_requests(env.model):
        await env.engine.execute_run(RUN)
    run = await env.store.get_run(RUN)
    assert run["status"] == "paused", run.get("error")
    assert run["bridge_completion_hold"] and run["worker_feedback"] == feedback
    entries = [copy.deepcopy(row.data) for row in env.journal.records.rows]
    task = next(t for t in (await env.journal.plan(RUN))["tasks"] if t["id"] == "implement")
    env.known_run, env.known_task, env.known_entries, env.posts = run, task, entries, posts
    try:
        yield env
    finally:
        await env.model.client.close()


def proof_module():
    assert importlib.util.find_spec("firefly_forge.known_worker_completion_proof") is not None, (
        "Native known-worker completion proof is missing"
    )
    return importlib.import_module("firefly_forge.known_worker_completion_proof")


async def test_real_sdk_known_worker_output_has_a_settleable_proof_without_any_response_replay(known_worker):
    env = known_worker
    before = copy.deepcopy((env.known_run, env.known_task, env.known_entries))
    prepared = proof_module().prepare_worker_completion(env.known_run, env.known_task, env.known_entries)
    proof = proof_module().validate_worker_completion(prepared, env.known_run, env.known_task, env.known_entries)
    assert proof["completion_count"] == 2
    assert proof["feedback_record_digest"] == next(
        e["digest"] for e in env.known_entries if e["kind"] == "worker_feedback"
    )
    assert proof["verification"] == "worker_output_known_only"
    assert len(env.posts) == 2
    assert (env.known_run, env.known_task, env.known_entries) == before
    assert "summary" not in json.dumps(proof)


def rechain(entries):
    from firefly_forge.journal import ZERO_HASH, digest

    prior = ZERO_HASH
    moved = {}

    def resolve(value):
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return moved.get(value, value) if isinstance(value, str) else value

    for sequence, entry in enumerate(entries, 1):
        old = entry["digest"]
        entry["payload"] = resolve(entry["payload"])
        entry.update(sequence=sequence, previous_hash=prior)
        entry["digest"] = digest({key: value for key, value in entry.items() if key != "digest"})
        moved[old] = prior = entry["digest"]
    return entries


@pytest.mark.parametrize(
    "change",
    [
        "missing_consume",
        "duplicate_intent",
        "wrong_hold",
        "wrong_profile",
        "wrong_task",
        "feedback_changed",
        "old_contract",
        "restored_consume",
    ],
)
async def test_unknown_ambiguous_or_changed_custody_never_produces_a_settlement(known_worker, monkeypatch, change):
    env = known_worker
    run, task, entries = copy.deepcopy((env.known_run, env.known_task, env.known_entries))
    module = proof_module()
    if change == "missing_consume":
        entries.remove(next(e for e in entries if e["kind"] == "agent_consume"))
    elif change == "duplicate_intent":
        entries.append(copy.deepcopy(next(e for e in entries if e["kind"] == "bridge_request_intent")))
    elif change == "wrong_hold":
        run["bridge_completion_hold"]["intent_digest"] = "f" * 64
    elif change == "wrong_profile":
        run["model_selection"] = {**run.get("model_selection", {}), "profile_id": "another-profile"}
    elif change == "wrong_task":
        task["idempotency_key"] = "f" * 32
    elif change == "feedback_changed":
        run["worker_feedback"]["summary"] = "An unsupported replacement"
    elif change == "old_contract":
        monkeypatch.setattr(module, "runtime_contract", lambda: "f" * 64)
    else:
        next(e for e in entries if e["kind"] == "agent_consume")["payload"]["version"] = 2
    rechain(entries)
    with pytest.raises(ValueError):
        module.prepare_worker_completion(run, task, entries)
    assert env.store.run.data["bridge_completion_hold"]
    assert len(env.posts) == 2


async def test_final_predicate_needs_no_filesystem_or_sdk_decode_inside_writer(known_worker, monkeypatch):
    env = known_worker
    module = proof_module()
    prepared = module.prepare_worker_completion(env.known_run, env.known_task, env.known_entries)

    def forbidden(*args, **kwargs):
        raise AssertionError("No metadata or decode in the final writer predicate")

    monkeypatch.setattr(module, "runtime_contract", forbidden)
    monkeypatch.setattr(module.BoundaryCheckpoint, "decode", forbidden)
    async with env.store._unit():
        assert (
            module.validate_worker_completion(prepared, env.known_run, env.known_task, env.known_entries)[
                "completion_count"
            ]
            == 2
        )


async def test_mutable_prepared_data_cannot_replace_the_proven_final_output(known_worker):
    env = known_worker
    module = proof_module()
    prepared = module.prepare_worker_completion(env.known_run, env.known_task, env.known_entries)
    prepared.feedback["summary"] = "Changed after proof"
    with pytest.raises(ValueError, match="Prepared completion evidence changed"):
        module.validate_worker_completion(prepared, env.known_run, env.known_task, env.known_entries)


def test_external_dynamic_schema_reference_is_refused_before_any_resolver(monkeypatch):
    module = proof_module()

    def forbidden(*args, **kwargs):
        raise AssertionError("External schema resolution must not begin")

    monkeypatch.setattr(module, "Draft202012Validator", forbidden)
    with pytest.raises(ValueError, match="external references"):
        module._schema({"$dynamicRef": "https://example.invalid/schema"}, {})
