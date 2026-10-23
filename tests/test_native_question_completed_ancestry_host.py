# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Question provenance uses completed-output consent as history, never renewed execution authority."""
# ruff: noqa: F401, F811

import copy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_native_question_publication_host import pending
from test_scm_polling import poll_case

from firefly_forge.journal import digest
from firefly_forge.native_question_publications import NativeQuestionPublications
from firefly_forge.project_access import AccessDenied
from firefly_forge.source_result_readmission import SourceCompletedOutputInput, validate_completed_output_parent


async def completed_entries(c):
    journal = c.collaboration.deployments.engine.journal
    entries = await c.collaboration.source_edit_authority.workspaces.recoveries._entries(c.run_id)
    parent = next(e for e in entries if e["kind"] == "workspace_reconciliation_receipt")
    prior = parent["payload"]
    actor = prior["actor"]
    candidate = copy.deepcopy(prior["candidate"])
    candidate.pop("digest")
    candidate.update(previous_task={"attempt": 3}, plan={}, cut={}, previous_run_digest="a" * 64)
    marker = {"request_id": parent["idempotency_key"], "receipt_digest": parent["digest"]}
    observation = {"implementation_success": False, "process_exit_attested": False}
    contract = "b" * 64
    review = dict(
        parent_request_id=marker["request_id"],
        parent_receipt_digest=marker["receipt_digest"],
        previous_task=prior["previous_task"],
        admission_contract_digest=contract,
        observation=observation,
        **{k: candidate[k] for k in ("source_edit", "plan", "cut", "previous_run_digest")},
    )
    review["digest"] = digest(review)
    key = uuid4().hex
    body = SourceCompletedOutputInput(
        version=1,
        request_id=key,
        candidate_digest=review["digest"],
        admission_contract_digest=contract,
        previous_catalog_path="/public/fixture/catalog.json",
        valid_for_seconds=60,
        acknowledge_output_not_success=True,
        acknowledge_no_exit_attestation=True,
        acknowledge_original_budgets=True,
        acknowledge_no_replay=True,
        acknowledge_new_finite_attempt=True,
    )
    candidate["completed_output_readmission"] = dict(
        parent_marker=marker,
        previous_task=prior["previous_task"],
        review_digest=review["digest"],
        admission_contract_digest=contract,
        observation_digest=digest(observation),
        from_attempt=2,
        to_attempt=3,
    )
    candidate["digest"] = digest(candidate)
    expires = int(datetime.now(UTC).timestamp() * 1000) + 60_000
    proof = dict(
        review=review,
        request=body.model_dump(),
        actor=actor,
        parent_marker=marker,
        previous_task=prior["previous_task"],
        new_candidate_digest=candidate["digest"],
        observation=observation,
        expires_at_ms=expires,
        no_environment_effect=True,
        no_provider_dispatch=True,
    )
    observed = await journal._append(c.run_id, "implement", key, "source_output_readmission", proof)
    request = {**prior["request"], "request_id": key, "candidate_digest": candidate["digest"]}
    payload = dict(
        request=request,
        actor=actor,
        candidate=candidate,
        previous_task=candidate["previous_task"],
        expires_at_ms=expires,
        completed_output_request=body.model_dump(),
    )
    intent = await journal._append(c.run_id, "implement", key, "workspace_reconciliation_intent", payload)
    event = await journal._append(
        c.run_id,
        "implement",
        key,
        "workspace_reconciliation_receipt",
        {
            **payload,
            "intent_digest": intent["digest"],
            "readmission_digest": observed["digest"],
            "effect": {
                "version": 1,
                "no_environment_effect": True,
                "completed_output_observation": digest(observation),
            },
        },
    )
    entries = await c.collaboration.source_edit_authority.workspaces.recoveries._entries(c.run_id)
    run = {"id": c.run_id, "source_edit_continuation": {"request_id": key, "receipt_digest": event["digest"]}}
    return entries, run, parent, event


async def test_direct_scm_parent_survives_valid_completed_output_with_historical_expiry(poll_case, monkeypatch):
    import firefly_forge.source_result_readmission as output

    async with poll_case() as c:
        await pending(c, monkeypatch)
        entries, run, parent, event = await completed_entries(c)
        monkeypatch.setattr(output, "clock_ms", lambda: event["payload"]["expires_at_ms"] + 7_200_000)
        assert validate_completed_output_parent(entries, event, event["payload"]["actor"]) == parent
        activation, evidence = NativeQuestionPublications._source(entries, run)
        assert activation == event and evidence == parent["payload"]["scm_source_activation"]


@pytest.mark.parametrize("change", ["review", "observation", "expiry", "intent", "parent"])
async def test_completed_question_ancestry_refuses_unvalidated_marker_or_changed_proof(poll_case, monkeypatch, change):
    async with poll_case() as c:
        await pending(c, monkeypatch)
        entries, run, _, event = await completed_entries(c)
        event = next(e for e in entries if e["digest"] == event["digest"])
        observed = next(e for e in entries if e["kind"] == "source_output_readmission")
        if change == "review":
            observed["payload"]["review"]["parent_receipt_digest"] = "f" * 64
        elif change == "observation":
            observed["payload"]["observation"]["implementation_success"] = True
        elif change == "expiry":
            event["created_at"] = datetime.fromtimestamp(event["payload"]["expires_at_ms"] / 1000, UTC).isoformat()
        elif change == "intent":
            event["payload"]["intent_digest"] = "f" * 64
        else:
            event["payload"]["candidate"]["completed_output_readmission"]["parent_marker"]["receipt_digest"] = "f" * 64
        with pytest.raises((ValueError, KeyError, AccessDenied)):
            NativeQuestionPublications._source(entries, run)
