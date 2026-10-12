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
    result = await execute(
        [
            sys.executable,
            "-c",
            "import os; print([k for k in os.environ if k.startswith('FORGE_') "
            "or k in ('ANTHROPIC_API_KEY','PGPASSWORD')]); "
            "assert os.environ.get('PATH'); assert os.environ.get('HOME')",
        ],
        tmp_path,
    )
    assert result.exit_code == 0
    assert result.output.strip() == "[]"
    assert os.environ["FORGE_BRIDGE_TOKEN"] == "private-bridge"
