# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import hashlib
import importlib
import os

import pytest


def module():
    try:
        return importlib.import_module("firefly_forge.workspace_tools")
    except ModuleNotFoundError as exc:
        raise AssertionError("Confined search, editing and diff tools are missing") from exc


@pytest.mark.asyncio
async def test_search_and_hash_checked_line_edit_change_real_source(tmp_path):
    source = tmp_path / "inventory.py"
    source.write_text("def available(qty):\n    return qty > 0\n")
    tools = module().WorkspaceTools(tmp_path)
    found = await tools.search_text("return qty")
    hit = found["matches"][0]
    assert hit["path"] == "inventory.py" and hit["line"] == 2
    assert hit["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    edited = await tools.edit_lines("inventory.py", 2, 2, "    return qty >= 1\n", hit["sha256"])
    assert source.read_text() == "def available(qty):\n    return qty >= 1\n"
    assert edited["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert tools.files_written == {"inventory.py"}


@pytest.mark.asyncio
async def test_stale_line_edit_cannot_overwrite_new_source(tmp_path):
    source = tmp_path / "service.py"
    source.write_text("VALUE = 2\n")
    with pytest.raises(ValueError, match="hash"):
        await module().WorkspaceTools(tmp_path).edit_lines("service.py", 1, 1, "VALUE = 3\n", "0" * 64)
    assert source.read_text() == "VALUE = 2\n"


@pytest.mark.asyncio
async def test_search_excludes_credentials_links_and_dependency_trees(tmp_path):
    (tmp_path / "service.py").write_text("needle\n")
    (tmp_path / ".env").write_text("needle-secret\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "index.js").write_text("needle-dependency\n")
    (tmp_path / "link.py").symlink_to(tmp_path / ".env")
    result = await module().WorkspaceTools(tmp_path).search_text("needle")
    assert [hit["path"] for hit in result["matches"]] == ["service.py"]
    with pytest.raises(ValueError):
        await module().WorkspaceTools(tmp_path).search_text("needle", path_prefix="../")


@pytest.mark.asyncio
async def test_line_edit_does_not_modify_hardlinks(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-private.py"
    outside.write_text("preserve\n")
    os.link(outside, tmp_path / "linked.py")
    with pytest.raises(ValueError, match="link"):
        await (
            module()
            .WorkspaceTools(tmp_path)
            .edit_lines("linked.py", 1, 1, "changed\n", hashlib.sha256(outside.read_bytes()).hexdigest())
        )
    assert outside.read_text() == "preserve\n"


@pytest.mark.asyncio
async def test_search_reports_truncation_instead_of_unbounded_output(tmp_path):
    (tmp_path / "service.py").write_text("needle\n" * 100)
    result = await module().WorkspaceTools(tmp_path).search_text("needle", max_results=3)
    assert len(result["matches"]) == 3 and result["truncated"]


@pytest.mark.asyncio
async def test_component_diff_is_read_only_and_does_not_execute_git_config(tmp_path):
    from firefly_forge.process import checked

    repository = tmp_path / "repository"
    repository.mkdir()
    component = repository / "inventory"
    component.mkdir()
    sibling = repository / "payments"
    sibling.mkdir()
    (component / "service.py").write_text("VALUE = 1\n")
    (sibling / "other.py").write_text("PRIVATE = 1\n")
    await checked(["git", "init", "-b", "main"], repository)
    await checked(["git", "add", "."], repository)
    identity = {
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    await checked(
        ["git", "-c", "core.hooksPath=/dev/null", "commit", "--no-gpg-sign", "-m", "fixture"], repository, env=identity
    )
    marker = tmp_path / "external-hook-ran"
    hook = repository / "unsafe-hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    await checked(["git", "config", "core.fsmonitor", str(hook)], repository)
    await checked(["git", "config", "diff.external", str(hook)], repository)
    (component / "service.py").write_text("VALUE = 2\n")
    (sibling / "other.py").write_text("PRIVATE = 2\n")
    before = {
        p.name: p.read_bytes()
        for p in [repository / ".git" / "config", repository / ".git" / "index", repository / ".git" / "HEAD"]
    }
    diff = await module().WorkspaceTools(component, repository_root=repository).git_diff()
    assert "VALUE = 2" in diff["diff"]
    assert "PRIVATE" not in diff["diff"]
    assert not marker.exists()
    assert before == {
        p.name: p.read_bytes()
        for p in [repository / ".git" / "config", repository / ".git" / "index", repository / ".git" / "HEAD"]
    }


def test_catalog_distinguishes_configured_workspace_tools_from_optional_runners():
    tools = {item["id"]: item for item in module().tool_catalog()}
    assert tools["edit_lines"]["availability"] == "available"
    assert tools["execute_command"]["availability"] == "not_configured"
    assert tools["browser_check"]["availability"] == "not_configured"
    assert tools["internet_search"]["availability"] == "not_configured"
    assert all(item["availability"] == "unsupported" for item in module().tool_catalog(provider="codex"))


@pytest.mark.asyncio
async def test_line_edit_checks_hash_again_on_the_open_write_descriptor(tmp_path, monkeypatch):
    source = tmp_path / "service.py"
    source.write_text("VALUE=1\n")
    tools = module().WorkspaceTools(tmp_path)
    original = tools.write_file

    async def concurrent_change(path, content, **kwargs):
        source.write_text("VALUE=9\n")
        return await original(path, content, **kwargs)

    monkeypatch.setattr(tools, "write_file", concurrent_change)
    with pytest.raises(ValueError, match="hash"):
        await tools.edit_lines("service.py", 1, 1, "VALUE=2\n", hashlib.sha256(b"VALUE=1\n").hexdigest())
    assert source.read_text() == "VALUE=9\n"
