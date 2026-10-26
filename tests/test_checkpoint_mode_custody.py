# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import copy
import importlib

import pytest
from test_known_worker_completion_proof_host import known_worker as known_worker

from firefly_forge.journal import digest


def module():
    return importlib.import_module("firefly_forge.checkpoint_mode_custody")


def chain(rows):
    out = []
    for row in rows:
        e = copy.deepcopy(row)
        e.update(sequence=len(out) + 1, previous_hash=out[-1]["digest"] if out else "0" * 64)
        e["digest"] = digest({k: v for k, v in e.items() if k != "digest"})
        out.append(e)
    return out


def test_empty_history_needs_no_configuration_read():
    def forbidden(_):
        raise AssertionError("Empty custody read configuration")

    assert module().validate_closed_custody([], "1" * 32, forbidden, project_id=None, repository_id="2" * 32) == []


async def test_real_controlled_sdk_closed_history_is_immutable_and_hash_only(known_worker):
    e = known_worker
    before = copy.deepcopy(e.known_entries)
    refs = module().validate_closed_custody(
        e.known_entries,
        e.known_run["id"],
        e.store.configuration.load_snapshot,
        project_id=e.known_run.get("project_id"),
        repository_id=e.known_run["repository_id"],
    )
    assert len(refs) == 2 and all(len(v) == 64 for r in refs for v in r.values())
    assert e.known_entries == before


@pytest.mark.parametrize(
    "mutation",
    [
        "orphan",
        "duplicate",
        "missing",
        "order",
        "body",
        "consume",
        "checkpoint",
        "response",
        "scope",
        "task",
        "endpoint",
        "model",
        "invocation",
        "repository",
        "config",
    ],
)
async def test_rehashed_invalid_custody_refuses(known_worker, mutation):
    e = known_worker
    rows = copy.deepcopy(e.known_entries)
    kinds = {
        k: next(x for x in rows if x["kind"] == k)
        for k in ("bridge_request_intent", "agent_checkpoint", "bridge_request_response", "agent_consume")
    }
    if mutation == "orphan":
        extra = copy.deepcopy(kinds["agent_consume"])
        extra["idempotency_key"] = "f" * 32
        rows.append(extra)
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(kinds["bridge_request_intent"]))
    elif mutation == "missing":
        rows.remove(kinds["agent_checkpoint"])
    elif mutation == "order":
        a, b = rows.index(kinds["agent_checkpoint"]), rows.index(kinds["bridge_request_response"])
        rows[a], rows[b] = rows[b], rows[a]
    elif mutation == "body":
        kinds["bridge_request_response"]["payload"]["response_sha256"] = "0" * 64
    elif mutation == "consume":
        kinds["agent_consume"]["payload"]["response_record_digest"] = "0" * 64
    elif mutation == "checkpoint":
        kinds["agent_checkpoint"]["payload"]["intent_digest"] = "0" * 64
    elif mutation == "response":
        kinds["bridge_request_response"]["payload"]["intent_digest"] = "0" * 64
    elif mutation == "scope":
        kinds["bridge_request_intent"]["payload"]["run_id"] = "f" * 32
    elif mutation == "task":
        kinds["bridge_request_intent"]["payload"]["task_key"] = "f" * 32
    elif mutation == "invocation":
        kinds["agent_checkpoint"]["payload"]["invocation"]["ordinal"] = 9
    elif mutation == "repository":
        kinds["bridge_request_intent"]["payload"]["repository_id"] = "f" * 32
    elif mutation == "config":
        kinds["bridge_request_intent"]["payload"]["config_revision"] = "f" * 32
    else:
        kinds["bridge_request_intent"]["payload"]["descriptor"][mutation] = (
            "https://example.test/v1" if mutation == "endpoint" else "different-model"
        )
    with pytest.raises(ValueError):
        module().validate_closed_custody(
            chain(rows),
            e.known_run["id"],
            e.store.configuration.load_snapshot,
            project_id=e.known_run.get("project_id"),
            repository_id=e.known_run["repository_id"],
        )
