# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Only causal saved evidence may satisfy post-admission reporting criteria."""

import copy

import pytest


def fixture():
    from firefly_forge.campaign_delivery_plans import CampaignDeliveryPlanInput

    body = CampaignDeliveryPlanInput(
        request_id="a" * 32,
        expected_previous_digest=None,
        expected_campaign_request_digest="b" * 64,
        expected_project_digest="c" * 64,
        title="Evidence",
        waves=[{"id": "one", "name": "First", "component_ids": ["api"]}],
        milestones=[
            {
                "id": "prepared",
                "name": "Workspace prepared",
                "wave_id": "one",
                "conditions": [
                    {"kind": "task_receipted", "component_id": "api", "task_id": "prepare"},
                ],
            }
        ],
    )
    plan = {
        "request_id": "a" * 32,
        "revision": 1,
        "digest": "d" * 64,
        "request": body.model_dump(),
        "prepared_at": "2026-10-15T00:00:00+00:00",
        "project_id": "e" * 32,
        "campaign_id": "f" * 32,
    }
    report = {
        "digest": "1" * 64,
        "components": [
            {
                "component_id": "api",
                "run_id": "2" * 32,
                "attempt": 1,
                "state": "in_progress",
                "verification": "not_verified",
                "plan_digest": "3" * 64,
                "tasks": [
                    {
                        "id": "prepare",
                        "attempt": 1,
                        "state": "receipted",
                        "evidence_digest": "4" * 64,
                        "receipt": {"sequence": 3, "digest": "5" * 64},
                    }
                ],
            }
        ],
    }
    archive = {
        "request_id": "6" * 32,
        "digest": "7" * 64,
        "prepared_at": "2026-10-15T00:01:00+00:00",
        "source": {
            "campaign_journal_head": "8" * 64,
            "campaign_event_count": 1,
            "project_digest": "c" * 64,
            "request_digest": "b" * 64,
        },
        "report": report,
    }
    return plan, archive


def test_task_receipt_can_satisfy_a_milestone_without_claiming_verified_delivery():
    from firefly_forge.campaign_delivery_plans import derive_progress

    plan, archive = fixture()
    result = derive_progress(plan, archive)
    assert result["milestones"][0]["state"] == "satisfied"
    assert result["waves"][0]["state"] == "in_progress"
    assert result["criteria_binding"] == "reporting_only_post_admission"
    assert result["enforced_at_admission"] is False
    assert derive_progress(plan, archive) == result
    assert "percent" not in str(result)


def test_exact_prepare_receipt_survives_a_later_unknown_effect_without_verifying_the_component():
    from firefly_forge.campaign_delivery_plans import derive_progress

    plan, archive = fixture()
    archive["report"]["components"][0]["state"] = "unknown"
    plan["request"]["milestones"].append(
        {
            "id": "delivery",
            "name": "API delivery",
            "wave_id": "one",
            "conditions": [
                {"kind": "component_verified", "component_id": "api", "task_id": None},
            ],
            "feature_ids": [],
            "journey_refs": [],
            "acceptance_refs": [],
        }
    )
    result = derive_progress(plan, archive)
    assert result["milestones"][0]["conditions"][0]["state"] == "satisfied"
    assert result["milestones"][1]["conditions"][0]["state"] == "unknown"
    assert result["waves"][0]["state"] == result["state"] == "unknown"


def test_current_component_retry_and_current_task_attempt_are_distinct_counters():
    from firefly_forge.campaign_delivery_plans import derive_progress

    plan, archive = fixture()
    archive["report"]["components"][0]["attempt"] = 2
    result = derive_progress(plan, archive)
    condition = result["milestones"][0]["conditions"][0]
    assert condition["state"] == "satisfied"
    assert condition["component_attempt"] == 2
    assert condition["task_attempt"] == 1
    assert result["waves"][0]["state"] == "in_progress"


@pytest.mark.parametrize("mutation", ["unknown", "missing_receipt", "missing_evidence", "missing_task", "old_attempt"])
def test_missing_or_uncertain_current_evidence_never_satisfies_conditions(mutation):
    from firefly_forge.campaign_delivery_plans import derive_progress

    plan, archive = fixture()
    component = archive["report"]["components"][0]
    if mutation == "unknown":
        component["state"] = "unknown"
        plan["request"]["milestones"][0]["conditions"] = [
            {"kind": "component_verified", "component_id": "api", "task_id": None}
        ]
    elif mutation == "missing_task":
        component["tasks"] = []
    elif mutation == "old_attempt":
        component["tasks"][0]["state"] = "not_started"
        component["tasks"][0]["receipt"] = None
        component["tasks"][0]["attempt"] = 2
    else:
        component["tasks"][0]["receipt" if mutation == "missing_receipt" else "evidence_digest"] = None
    result = derive_progress(plan, archive)
    assert result["milestones"][0]["state"] != "satisfied"
    assert result["waves"][0]["state"] != "satisfied"


def test_later_wave_retains_observed_completion_but_reports_unsatisfied_predecessor():
    from firefly_forge.campaign_delivery_plans import derive_progress

    plan, archive = fixture()
    plan["request"]["waves"].append({"id": "two", "name": "Second", "component_ids": ["web"]})
    plan["request"]["milestones"].append(
        {
            "id": "web",
            "name": "Web verified",
            "wave_id": "two",
            "conditions": [
                {"kind": "component_verified", "component_id": "web", "task_id": None},
            ],
            "feature_ids": [],
            "journey_refs": [],
            "acceptance_refs": [],
        }
    )
    second = copy.deepcopy(archive["report"]["components"][0])
    second.update(component_id="web", state="verified", verification="verified")
    archive["report"]["components"].append(second)
    result = derive_progress(plan, archive)
    assert result["waves"][1]["observed_state"] == "satisfied"
    assert result["waves"][1]["state"] == "dependency_blocked"
    assert result["waves"][1]["blocking_wave_ids"] == ["one"]


@pytest.mark.parametrize(
    "mutation", ["boolean", "duplicate_component", "missing_milestone", "wrong_wave", "extra", "task_id"]
)
def test_plan_input_rejects_ambiguous_or_executable_conditions(mutation):
    from firefly_forge.campaign_delivery_plans import CampaignDeliveryPlanInput

    body = fixture()[0]["request"]
    if mutation == "boolean":
        body["request_id"] = True
    elif mutation == "duplicate_component":
        body["waves"][0]["component_ids"].append("api")
    elif mutation == "missing_milestone":
        body["milestones"] = []
    elif mutation == "wrong_wave":
        body["milestones"][0]["wave_id"] = "other"
    elif mutation == "extra":
        body["enforced_at_admission"] = True
    else:
        body["milestones"][0]["conditions"][0]["task_id"] = "run-shell-command"
    with pytest.raises(ValueError):
        CampaignDeliveryPlanInput.model_validate(body)
