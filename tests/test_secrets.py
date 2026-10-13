# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import os
import sys

from firefly_forge.engine import redact
from firefly_forge.process import execute


def test_database_uri_password_is_redacted_decoded_and_encoded(monkeypatch):
    monkeypatch.setenv("FORGE_DATABASE_URL", "postgresql+asyncpg://forge:special%40password@localhost/forge")
    value = redact("special@password special%40password postgresql+asyncpg://forge:special%40password@localhost/forge")
    assert "special" not in value


async def test_verification_child_does_not_inherit_control_plane_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_DATABASE_URL", "postgresql+asyncpg://forge:private@localhost/forge")
    monkeypatch.setenv("FORGE_BRIDGE_TOKEN", "private-bridge")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-api")
    monkeypatch.setenv("PGPASSWORD", "private-database")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/ssh-agent")
    monkeypatch.setenv("DOCKER_CONFIG", "/private/registry-login")
    result = await execute(
        [
            sys.executable,
            "-c",
            "import os; print([k for k in os.environ if k.startswith('FORGE_') "
            "or k in ('ANTHROPIC_API_KEY','PGPASSWORD','SSH_AUTH_SOCK','DOCKER_CONFIG')]); "
            "assert os.environ.get('PATH'); assert os.environ.get('HOME')",
        ],
        tmp_path,
    )
    assert result.exit_code == 0
    assert result.output.strip() == "[]"
    assert os.environ["FORGE_BRIDGE_TOKEN"] == "private-bridge"


def test_model_token_budget_is_not_a_credential_but_access_tokens_remain_protected():
    from firefly_forge.secrets import contains_known_credential, sensitive_name

    assert not sensitive_name("FORGE_AGENTIC_MAX_TOKENS")
    assert not contains_known_credential("Use a 16384 token context budget", {"FORGE_AGENTIC_MAX_TOKENS": "16384"})
    assert contains_known_credential("private-access-token", {"FORGE_BRIDGE_TOKEN": "private-access-token"})


def test_namespaced_model_token_budgets_are_not_registered_as_credentials():
    from firefly_forge.secrets import contains_known_credential

    for name in [
        "AGENTIC_FORGE_AGENTIC_MAX_TOKENS",
        "REVIEW_FORGE_AGENTIC_MAX_TOKENS",
        "functional_FORGE_AGENTIC_MAX_TOKENS",
    ]:
        assert not contains_known_credential("The maximum context is 16384 tokens", {name: "16384"})
    assert contains_known_credential("private-key", {"functional_FORGE_AGENTIC_API_KEY": "private-key"})
