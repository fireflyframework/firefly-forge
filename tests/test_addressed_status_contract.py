# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded direct status addresses, never a general conversational executor."""

import hashlib

import pytest

from firefly_forge.collaboration import RepositoryCommand


@pytest.mark.parametrize(
    "body,language",
    [
        ("@forge how is the CRM going?", "en"),
        ("@forge how is this run doing?", "en"),
        ("@forge what's the status of this run?", "en"),
        ("@forge, what's the status of this work?", "en"),
        ("@FORGE what happened?", "en"),
        ("@forge ¿cómo va el CRM?", "es"),
        ("@forge, ¿cuál es el estado?", "es"),
        ("forge, ¿qué pasó con esta ejecución?", "es"),
    ],
)
def test_direct_status_address_has_read_only_intent_and_exact_original_body(body, language):
    parsed = RepositoryCommand.parse(body)
    assert parsed.action == "status"
    assert parsed.adapter == "addressed_status_v1"
    assert parsed.intent == "status"
    assert parsed.language == language
    assert parsed.address == "forge"
    assert parsed.source_body_sha256 == hashlib.sha256(body.encode()).hexdigest()
    assert "run_id" not in parsed.model_dump(exclude_none=True)


@pytest.mark.parametrize("address", ["forge", "configured-app"])
def test_native_forge_alias_is_primary_and_configured_app_is_optional_compatible_alias(address):
    body = "@" + address + " how is this going?"
    parsed = RepositoryCommand.parse(body, bot_addresses=("configured-app",))
    assert parsed.intent == "status"
    assert parsed.address == address


@pytest.mark.parametrize(
    "body",
    [
        "> @forge how is this going?",
        "```text\n@forge how is this going?\n```",
        "~~~\n@forge ¿cómo va?\n~~~",
        "    @forge how is this going?",
        '"@forge how is this going?"',
        "Alice wrote:\n@forge how is this going?",
        "@forgery how is this going?",
        "@another-app how is this going?",
        "Here is an example: @forge how is this going?",
    ],
)
def test_quoted_code_forwarded_and_foreign_addresses_are_not_direct_invocations(body):
    with pytest.raises(ValueError):
        RepositoryCommand.parse(body)


@pytest.mark.parametrize(
    "body",
    [
        "@forge resume the run",
        "@forge reanuda la ejecución",
        "@forge how is this going? Deploy it now.",
        "@forge ¿cómo va? Y cambia el producto.",
        "@forge tell me a joke",
        "@forge /forge run {}",
    ],
)
def test_mutating_multiple_or_unknown_intent_is_only_a_read_only_clarification(body):
    parsed = RepositoryCommand.parse(body)
    assert parsed.action == "status"
    assert parsed.intent == "clarify"
    assert "run_id" not in parsed.model_dump(exclude_none=True)
    assert not {"commands", "target_id", "answer", "resume"}.intersection(parsed.model_dump())


def test_untrusted_run_identifier_in_status_prose_does_not_select_a_run():
    untrusted = "a" * 32
    body = "@forge what is the status of run " + untrusted + "?"
    parsed = RepositoryCommand.parse(body)
    assert parsed.intent == "status"
    assert "run_id" not in parsed.model_dump(exclude_none=True)
    assert untrusted not in str(parsed.model_dump())


def test_existing_explicit_status_command_remains_supported():
    run_id = "b" * 32
    parsed = RepositoryCommand.parse('/forge status {"run_id":"' + run_id + '"}')
    assert parsed.action == "status" and parsed.run_id == run_id
