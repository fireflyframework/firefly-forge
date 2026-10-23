# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Private-file boundaries apply to the opened object, including filesystem aliases."""

import os

import pytest

from firefly_forge.workspace_tools import WorkspaceTools


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [".ENV", ".NPMRC", "CREDENTIALS.JSON", "KEY.PEM", "KEY.KEY"])
async def test_private_names_are_case_insensitive_even_on_case_sensitive_filesystems(tmp_path, name):
    source = tmp_path / name
    source.write_text("public-test-sentinel")
    tools = WorkspaceTools(tmp_path)
    with pytest.raises(ValueError, match="unavailable"):
        await tools.read_file(name)
    with pytest.raises(ValueError, match="unavailable"):
        await tools.write_file(name, "changed")
    assert source.read_text() == "public-test-sentinel"
    assert name not in await tools.list_files()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "write"])
async def test_public_spelling_cannot_alias_a_private_env_suffix(tmp_path, operation):
    actual = tmp_path / ".env.EXAMPLE"
    actual.write_text("public-test-sentinel")
    alias = tmp_path / ".env.example"
    if not alias.exists() or not os.path.samefile(actual, alias):
        pytest.skip("Requires a case-insensitive filesystem")
    tools = WorkspaceTools(tmp_path)
    with pytest.raises(ValueError, match="unavailable"):
        if operation == "read":
            await tools.read_file(alias.name)
        else:
            await tools.write_file(alias.name, "changed")
    assert actual.read_text() == "public-test-sentinel"
    assert not tools.files_written


@pytest.mark.asyncio
async def test_private_directory_alias_is_checked_before_creating_descendants(tmp_path):
    private = tmp_path / ".env.EXAMPLE"
    private.mkdir()
    alias = tmp_path / ".env.example"
    if not alias.exists() or not os.path.samefile(private, alias):
        pytest.skip("Requires a case-insensitive filesystem")
    tools = WorkspaceTools(tmp_path)
    with pytest.raises(ValueError, match="unavailable"):
        await tools.write_file(".env.example/new/subdir/file.py", "changed")
    assert list(private.iterdir()) == []


@pytest.mark.asyncio
async def test_actual_public_env_example_remains_readable_and_writable(tmp_path):
    (tmp_path / ".env.example").write_text("PUBLIC_SETTING=example\n")
    tools = WorkspaceTools(tmp_path)
    assert await tools.read_file(".env.example") == "PUBLIC_SETTING=example\n"
    await tools.write_file(".env.example", "PUBLIC_SETTING=changed\n")
    assert (tmp_path / ".env.example").read_text() == "PUBLIC_SETTING=changed\n"


@pytest.mark.asyncio
async def test_root_case_alias_still_confines_to_the_actual_directory(tmp_path):
    actual = tmp_path / "Workspace"
    actual.mkdir()
    (actual / "source.py").write_text("VALUE=1\n")
    alias = tmp_path / "workspace"
    if not alias.exists() or not os.path.samefile(actual, alias):
        pytest.skip("Requires a case-insensitive filesystem")
    tools = WorkspaceTools(alias)
    assert tools.root.name == "Workspace"
    assert await tools.read_file("source.py") == "VALUE=1\n"
