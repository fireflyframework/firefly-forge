# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""An authenticated reader reuses the native receipt builder without execution authority."""

import copy
import json
from types import SimpleNamespace
from uuid import UUID

from linked_verification_fixtures import candidate, consent
from test_linked_verification_host import append

from firefly_forge.execution_requests import digest
from firefly_forge.linked_verifications import LinkedVerifications


async def test_native_receipt_builder_validates_ledger_and_screens_failure_without_executing():
    commands = [dict(argv=["approved-check"], timeout=10)]
    proof = candidate(1)
    proof.update(
        run_digest=digest({}),
        actor_digest=digest({"actor": {}, "identity": {}}),
        commands_digest=digest({"commands": commands}),
    )
    proof["digest"] = digest({k: v for k, v in proof.items() if k != "digest"})
    body = consent(proof)
    data = dict(
        id=body["request_id"],
        run_id=proof["run_id"],
        kind="linked_verify",
        status="failed",
        actor={},
        identity={},
        parent_run={},
        candidate=proof,
        request=body,
        request_digest=digest(body),
        commands=commands,
        commands_digest=proof["commands_digest"],
        inputs_digest=proof["input_scope_digest"],
        effect_budget=1,
    )
    row = SimpleNamespace(
        data=data, id=UUID(data["id"]), run_id=UUID(data["run_id"]), kind=data["kind"], status=data["status"]
    )
    events = []
    append(events, "linked_verification_admitted", {"request_digest": data["request_digest"]})
    append(events, "linked_verification_claimed", {"request_digest": data["request_digest"]})
    intent = append(
        events,
        "linked_verification_command_intent",
        dict(ordinal=1, command_digest=digest(commands[0]), inputs_digest=data["inputs_digest"]),
    )
    append(
        events,
        "linked_verification_command_result",
        dict(ordinal=1, intent_digest=intent["digest"], exit_code=2, output="private-fixture-token: failed assertion"),
    )
    append(events, "linked_verification_held", dict(reason="check_failed"))
    admitted = []

    async def screen(value):
        assert "private-fixture-token" not in json.dumps(value)
        admitted.append(copy.deepcopy(value))

    service = LinkedVerifications(
        SimpleNamespace(admit_credentials=screen),
        None,
        None,
        SimpleNamespace(credential_registry=lambda: {"token": "private-fixture-token"}),
    )
    before = copy.deepcopy(data)
    result = await service._receipt(row, events)
    assert result["status"] == "failed" and result["progress"]["completed"] == 1
    assert result["last_failure"]["exit_code"] == 2 and "failed assertion" in result["last_failure"]["output_preview"]
    assert admitted == [result] and row.data == before
