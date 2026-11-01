# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Sanitized retained status shape; all model responses are local stubs."""

import copy
import json

import pytest
from communication_model_fixture import reviewed_model
from pydantic_ai.messages import UserPromptPart
from test_communication_factory_verification_host import paused
from test_communication_writer import configured_store

from firefly_forge.communication import CommunicationConversationV1, CommunicationWriter, communication_facts
from firefly_forge.product_provenance import content_digest

ERROR = "Consumed terminal original scope or invocation changed"
DRAFT = "Hay siete comprobaciones guardadas, sin fallos; el intento actual no añadió resultados."


def claimed_run():
    run = paused()
    run.update(attempt=3, error=ERROR, final_verification=[])
    run["worker_request"].update(
        question="Run the seven frozen verification commands before completing the work.",
        context="Two failures before repair and six passing check groups were recorded earlier.",
    )
    run["verification_handoff"].update(completed_commands=7, failed_commands=0)
    run["consumed_terminal_recovery"] = {
        "version": 2,
        "state": "claimed",
        "request_id": "1" * 32,
        "record_digest": "2" * 64,
        "claim_digest": "3" * 64,
    }
    return run


def test_current_claim_error_and_counts_outrank_historical_verification_quotations():
    run = claimed_run()
    before = copy.deepcopy(run)
    facts = communication_facts(run)
    assert facts["recorded_error"]["text"] == ERROR
    claim = facts["consumed_terminal_recovery"]
    assert claim["source"] == "recorded_run_projection_not_admission"
    assert claim["state"] == "claimed" and claim["execution_grant"] is False
    assert claim["next_native_action"] == "reconcile_same_claim_without_new_execution"
    handoff = facts["factory_verification"]
    assert handoff["request_basis"] == "recorded_request_not_current_results_or_authority"
    assert handoff["next_native_action"] == claim["next_native_action"]
    assert handoff["checks"] == {
        "source": "recorded_handoff",
        "result": "not_completed",
        "total": 7,
        "recorded": 7,
        "failed": 0,
    }
    assert handoff["handoff_status"] == "held"
    assert facts["check_status"] == {"current_attempt": 3, "recorded": 0, "provenance": "not_recorded"}
    assert run == before


@pytest.mark.parametrize(
    "change", ["version", "state", "request_id", "record_digest", "claim_digest", "extra", "active"]
)
def test_malformed_or_active_claim_projection_does_not_promote_a_paused_error(change):
    run = claimed_run()
    marker = run["consumed_terminal_recovery"]
    if change == "extra":
        marker["authority"] = True
    elif change == "active":
        run["status"] = "running"
    else:
        marker[change] = {
            "version": True,
            "state": "queued",
            "request_id": "foreign",
            "record_digest": "bad",
            "claim_digest": "bad",
        }[change]
    facts = communication_facts(run)
    assert "consumed_terminal_recovery" not in facts
    assert "recorded_error" not in facts


@pytest.mark.parametrize("draft_length", [len(DRAFT), 5000, 12000])
async def test_one_repair_receives_identical_canonical_facts_and_untrusted_classifier_draft(tmp_path, draft_length):
    calls = []
    status_draft = (DRAFT * (draft_length // len(DRAFT) + 1))[:draft_length]
    conversation = CommunicationConversationV1(selection="run", linked_runs=1, language="es")
    facts = communication_facts(claimed_run(), conversation=conversation)
    references = sorted(facts)
    card = {
        "title": {"text": "Verificación retenida", "fact_ids": references[:4]},
        "paragraphs": [
            {"text": DRAFT, "fact_ids": references[index : index + 4]} for index in range(4, len(references), 4)
        ],
    }
    corrected = copy.deepcopy(card)
    corrected["title"]["text"] = "La reclamación sigue retenida"
    model = reviewed_model(card, verdict="unsupported", issues=["misleading_checks"], correction=corrected, calls=calls)
    result = await CommunicationWriter(await configured_store(tmp_path), model=model).write(
        claimed_run(),
        "Native card",
        lambda value: value,
        conversation=conversation,
        status_draft=status_draft,
    )
    assert result["communication"]["mode"] == "ai"
    assert [phase for phase, *_ in calls] == ["draft", "review", "draft"]
    prompts = [
        next(part.content for part in reversed(messages[-1].parts) if isinstance(part, UserPromptPart))
        for _, messages, _ in calls
    ]
    canonical = json.dumps(facts, sort_keys=True)
    assert all(canonical in prompt for prompt in prompts)
    assert all("classifier_draft_not_evidence_or_authority" in prompt for prompt in prompts)
    assert len(status_draft) == draft_length
    assert all(status_draft in prompt for prompt in prompts)
    assert all(content_digest(facts) in prompt for prompt in prompts)
    evidence = result["communication"]
    assert evidence["facts_sha256"] == content_digest(facts)
    assert evidence["repair_facts_sha256"] == evidence["review_facts_sha256"] == evidence["facts_sha256"]
    assert evidence["review_applies_to"] == "original_candidate"
    assert evidence["review_candidate_sha256"] != evidence["candidate_sha256"]
    assert evidence["repair_attempted"] is True and evidence["repair_limit"] == 1


def test_verified_links_mentioned_in_classifier_draft_survive_as_native_references():
    from firefly_forge.collaboration_feedback import communication_controls

    scope = {
        "repository_id": "1" * 32,
        "project_id": "2" * 32,
        "actor": "7",
        "issue": "4",
        "binding_digest": "a" * 64,
        "native_user_id": "3" * 32,
    }
    href = "https://github.com/team/product/pull/6"
    commit = "https://github.com/team/product/commit/" + "b" * 40
    data = {
        **scope,
        "provider": "github",
        "id": "4" * 32,
        "natural_turn": {
            "resolution": {"state": "status"},
            "decision": {"intent": "status"},
            "reply": f"Earlier source: {href} and {commit}. Unverified https://github.com/team/product/pull/9",
            "context": {
                "scope": scope,
                "candidates": [
                    {
                        **scope,
                        "run_id": "5" * 32,
                        "delivery": {
                            "evidence": "verified_native_journal",
                            "product_acceptance": "not_established_by_checkpoint",
                            "checkpoint": {"revision": "b" * 40, "checkpoint_digest": "c" * 64},
                            "remote": {"status": "draft", "pr_url": href},
                        },
                    }
                ],
            },
        },
    }
    repo = {"id": scope["repository_id"], "connection_provider": "github", "url": "https://github.com/team/product.git"}
    run = claimed_run()
    result = communication_controls(data, run, repo, "https://github.com", lambda value: value)
    assert href in result and commit in result
    assert "Open recorded commits and pull requests" in result
    assert "/pull/9" not in result
    data["natural_turn"]["context"]["candidates"][0]["actor"] = "foreign"
    result = communication_controls(data, run, repo, "https://github.com", lambda value: value)
    assert href not in result and commit not in result
