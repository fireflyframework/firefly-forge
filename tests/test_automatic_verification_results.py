# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from uuid import uuid4

import pytest
from test_automatic_verification_journal import intent, setup  # noqa: F401

from firefly_forge.agent_roles import WorkerResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [7, 20])
async def test_noisy_results_fit_the_native_dialogue_contract(setup, count):  # noqa: F811
    memory, journal = setup
    expected = intent(memory)
    await journal.verification_begin(memory.id.hex, expected, check_current=lambda current: None)
    expected["commands_count"] = count
    expected["ordinals"] = list(range(1, count + 1))
    memory.run.data["verification_handoff"].update(commands_count=count, completed_commands=count)

    async def executions(*args):
        return [
            {
                "execution_id": uuid4().hex,
                "task_id": "implement",
                "parent_attempt": 1,
                "ordinal": ordinal,
                "effects": {"start": {"status": "done", "evidence": {"exit_code": 0, "output": "x" * 5000}}},
            }
            for ordinal in expected["ordinals"]
        ]

    journal.sandbox_executions = executions
    await journal.verification_finish(memory.id.hex, expected, check_current=lambda current: None)
    response = memory.run.data["worker_response"]
    validated = WorkerResponse.model_validate({key: response[key] for key in ("id", "actor", "answer")})
    assert validated.answer == response["answer"]
    assert len(response["answer"]) <= 8000
