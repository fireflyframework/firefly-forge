# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Configuration failures must refuse adoption before any process dispatch."""

from pathlib import Path

import pytest

from firefly_forge.provider_executable_adoption import executable_identity


def test_denied_entrypoint_metadata_is_an_adoption_configuration_error(tmp_path, monkeypatch):
    def denied(path):
        raise PermissionError("fixture denies entrypoint metadata")

    monkeypatch.setattr(Path, "is_symlink", denied)
    with pytest.raises(ValueError, match="configured CLI executable is unavailable"):
        executable_identity(str(tmp_path / "cli"), tmp_path / "workspaces")
