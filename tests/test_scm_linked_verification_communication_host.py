# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual linked check results cannot be replaced by the parent's historical passes."""

import copy

import pytest
from linked_verification_fixtures import candidate, receipt

from firefly_forge.communication import CommunicationConversationV1, communication_facts
from firefly_forge.journal import digest
from firefly_forge.scm_linked_verification_records import SCMVerificationCommunicationV1
from firefly_forge.scm_natural_runtime import catalog


def failed():
    command = {"argv": ["python", "-m", "pytest", "tests/test_contacts.py"], "timeout": 300}
    value = receipt(status="failed")
    value["last_failure"] = dict(
        ordinal=1,
        command_digest=digest(command),
        exit_code=1,
        output_preview="AssertionError: contact was not retained\n",
        output_truncated=False,
        event_digest="f" * 64,
    )
    return value, command


def test_current_linked_failure_keeps_exact_result_and_never_imports_old_parent_checks():
    value, command = failed()
    current = SCMVerificationCommunicationV1(
        phase="current", candidate=value["candidate"], receipt=value, failure_command=command
    )
    parent = {"status": "paused", "final_verification": [{"exit_code": 0}] * 7}
    facts = communication_facts(
        parent,
        conversation=CommunicationConversationV1(selection="run", language="es", linked_runs=1),
        linked_verification=current,
    )
    assert facts["state"] == {"status": "failed"}
    assert "factory_verification" not in facts and "verification" not in facts
    assert facts["linked_failure"]["exit_code"] == 1 and facts["linked_failure"]["ordinal"] == 1
    assert facts["check_diagnostics"]["check-1-output"]["text"] == value["last_failure"]["output_preview"]
    assert facts["check_diagnostics"]["check-1-command"]["text"] == "python -m pytest tests/test_contacts.py"
    assert facts["linked_verification"]["original_effect_count"] == 18


def test_unknown_result_has_no_invented_failure_command_or_success():
    value = SCMVerificationCommunicationV1(phase="unknown", candidate=candidate(), request_id="a" * 32)
    facts = communication_facts(
        None,
        conversation=CommunicationConversationV1(selection="clarify", language="en", linked_runs=0),
        linked_verification=value,
    )
    assert facts["state"] == {"status": "unknown"}
    assert facts["linked_verification"]["request_id"] == "a" * 32
    assert not {"linked_failure", "check_diagnostics"} & facts.keys()
    assert facts["linked_verification"]["unknown_admission_action"] == "read_same_request_without_post_retry"


def test_failure_command_and_receipt_must_be_bound_and_public_output_is_privacy_filtered():
    value, command = failed()
    with pytest.raises(ValueError):
        SCMVerificationCommunicationV1(
            phase="current",
            candidate=value["candidate"],
            receipt=value,
            failure_command=command | {"argv": ["invented"]},
        )
    value["last_failure"]["output_preview"] = "API_KEY=private-canary"
    current = SCMVerificationCommunicationV1(phase="current", candidate=value["candidate"], receipt=value)
    facts = communication_facts(
        None,
        conversation=CommunicationConversationV1(selection="clarify", language="en", linked_runs=0),
        linked_verification=current,
    )
    assert "private-canary" not in repr(facts) and "check_diagnostics" not in facts
    changed = copy.deepcopy(value["candidate"])
    changed["effect_budget"] += 1
    changed["digest"] = digest({k: v for k, v in changed.items() if k != "digest"})
    with pytest.raises(ValueError):
        SCMVerificationCommunicationV1(phase="current", candidate=changed, receipt=value)


def test_native_templates_preserve_historical_versions_and_declare_no_retry_or_coding():
    import hashlib
    from importlib.resources import files

    root = files("firefly_forge").joinpath("resources/prompts/scm-natural-v1")
    assert hashlib.sha256(root.joinpath("system-1.0.3.yaml").read_bytes()).hexdigest() == (
        "959e468494e67cd67f3f1074dfec7b11b550f4937f122e040554bc7b123e9214"
    )
    assert "No magic" not in catalog().text("scm-natural/system", version="1.0.3")
    prompt = catalog().text("scm-natural/system", version="1.0.4")
    assert "sí, ejecuta esas comprobaciones" in prompt and "multiple pending reviews" in prompt
    wording = catalog().text("scm-natural/verification-communication")
    assert "never send it again" in wording and "Failed work remains held" in wording


def test_unknown_readback_requires_request_identity_and_current_receipt_cannot_replace_it():
    with pytest.raises(ValueError):
        SCMVerificationCommunicationV1(phase="unknown", candidate=candidate())
    value = receipt()
    with pytest.raises(ValueError):
        SCMVerificationCommunicationV1(
            phase="current", candidate=value["candidate"], receipt=value, request_id="f" * 32
        )
