# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Finite operator continuation windows retain every previously spent handoff."""
# ruff: noqa: F811

from uuid import uuid4

import pytest
from test_unknown_command_acknowledgement_host import (  # noqa: F401
    adopt_and_begin,
    command_case,
    consent,
    service,
    unknown_case,
)

from firefly_forge.tool_broker import FactoryToolPolicyInput, tool_policy_snapshot
from firefly_forge.verification_handoffs import verification_intent
from firefly_forge.verification_models import AutomaticVerificationPolicy


def test_positive_finite_configured_verification_window_above_three():
    assert AutomaticVerificationPolicy(max_continuations=17).max_continuations == 17


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, None])
def test_continuation_window_keeps_exact_positive_integer_validation(value):
    with pytest.raises(ValueError):
        AutomaticVerificationPolicy(max_continuations=value)


async def test_renewed_window_seventeen_preserves_two_spent_handoffs(unknown_case, monkeypatch):
    case = unknown_case
    api = service(case, monkeypatch)
    policy = FactoryToolPolicyInput(automatic_verification={"enabled": True, "max_continuations": 17})
    case.store.configuration._write(
        case.store.configuration.directory / "tool-policy.json",
        {"revision": uuid4().hex, "settings": policy.model_dump()},
    )
    candidate = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    await api.request(case.run_id, case.unknown.id.hex, uuid4().hex, consent(candidate))
    intent = await adopt_and_begin(case, candidate, monkeypatch)
    assert intent["continuation"] == 3 and intent["max_continuations"] == 17
    assert len([row for row in case.store.records.rows if row.kind == "verification_handoff_intent"]) == 3
    assert len(case.store.sandboxes.rows) == 2


async def test_window_seventeen_exhaustion_then_explicit_renewal_retains_all_spent(unknown_case, monkeypatch):
    case = unknown_case
    api = service(case, monkeypatch)

    def renew(limit):
        policy = FactoryToolPolicyInput(automatic_verification={"enabled": True, "max_continuations": limit})
        case.store.configuration._write(
            case.store.configuration.directory / "tool-policy.json",
            {"revision": uuid4().hex, "settings": policy.model_dump()},
        )

    renew(17)
    for _ in range(15):
        run = await case.store.get_run(case.run_id)
        intent = verification_intent(
            case.store,
            {**run, "verification_handoff": {}, "tool_policy_snapshot": tool_policy_snapshot(case.store.configuration)},
            case.workspace,
            await case.journal.entries(case.run_id),
            await case.journal.sandbox_executions(case.run_id),
        )
        await case.journal._append(case.run_id, "implement", intent["id"], "verification_handoff_intent", intent)
        await case.journal._append(
            case.run_id,
            "implement",
            intent["id"] + ":held",
            "verification_handoff_outcome",
            {"id": intent["id"], "status": "held"},
        )
        case.store.run.data["verification_handoff"] = {**intent, "status": "held"}
    with pytest.raises(ValueError, match="exhausted"):
        await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    renew(18)
    candidate = await api.candidate(case.run_id, case.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    assert candidate["followup"]["continuation"] == 18
    assert sum(r.kind == "verification_handoff_intent" for r in case.store.records.rows) == 17
    assert len(case.store.sandboxes.rows) == 2
