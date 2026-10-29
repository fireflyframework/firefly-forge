# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline maintenance never sends a remote request or initializes missing state."""

from click.testing import CliRunner


def test_native_location_commands_are_explicit_offline_operations():
    from firefly_forge.cli import main

    result = CliRunner().invoke(main, ["native-locations", "--help"])
    assert result.exit_code == 0
    assert "prepare" in result.output and "commit" in result.output and "status" in result.output
    assert "offline" in result.output.lower()


def test_missing_existing_identity_refuses_before_database_or_creation(tmp_path, monkeypatch):
    from firefly_forge.cli import main

    missing = tmp_path / "missing"
    monkeypatch.delenv("FORGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("PYFLY_DATA_URL", raising=False)
    result = CliRunner().invoke(main, ["native-locations", "status", "a" * 32, "--data-dir", str(missing)])
    assert result.exit_code != 0
    assert not missing.exists()
    assert "identity" in result.output.lower()
