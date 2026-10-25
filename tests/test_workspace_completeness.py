# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pages are bounded views; native source evidence must remain complete."""

import hashlib
import os

import pytest

from firefly_forge.workspace_tools import WorkspaceTools


async def populate(root, count=2003):
    for i in range(count):
        (root / f"file-{i:05}.txt").write_text("plain source\n")
    target = root / "zz-final"
    target.mkdir()
    (target / "service.py").write_text("late-needle\n")


@pytest.mark.asyncio
async def test_legacy_internal_listing_is_complete_beyond_old_2000(tmp_path):
    await populate(tmp_path)
    files = await WorkspaceTools(tmp_path).list_files()
    assert len(files) == 2004
    assert "zz-final/service.py" in files


@pytest.mark.asyncio
async def test_search_prefix_reaches_source_after_old_listing_limit(tmp_path):
    await populate(tmp_path)
    result = await WorkspaceTools(tmp_path).search_text("late-needle", path_prefix="zz-final")
    assert [x["path"] for x in result["matches"]] == ["zz-final/service.py"]
    assert result["complete"] is True


@pytest.mark.asyncio
async def test_versioned_file_pages_reach_all_source_and_bind_scope(tmp_path):
    for i in range(7):
        (tmp_path / f"{i}.py").write_text("source\n")
    tools = WorkspaceTools(tmp_path)
    first = await tools.list_files_page(page_size=3)
    assert first["version"] == 1 and first["complete"] is False and len(first["files"]) == 3
    second = await tools.list_files_page(cursor=first["next_cursor"], page_size=3)
    third = await tools.list_files_page(cursor=second["next_cursor"], page_size=3)
    assert first["files"] + second["files"] + third["files"] == await tools.list_files()
    assert third["complete"] is True and third["next_cursor"] is None
    with pytest.raises(ValueError, match="scope|cursor"):
        await tools.list_files_page(path_prefix="elsewhere", cursor=first["next_cursor"])
    (tmp_path / "new.py").write_text("changed selection")
    with pytest.raises(ValueError, match="changed|stale"):
        await tools.list_files_page(cursor=first["next_cursor"], page_size=3)


@pytest.mark.asyncio
async def test_large_text_search_and_binary_classification_are_explicit(tmp_path):
    large = b"x" * 1_100_000 + b"\nlarge-needle\n"
    (tmp_path / "large.py").write_bytes(large)
    (tmp_path / "image.bin").write_bytes(b"\x00\xff\x01")
    result = await WorkspaceTools(tmp_path).search_text("large-needle")
    assert result["matches"][0]["sha256"] == hashlib.sha256(large).hexdigest()
    assert result["classifications"]["binary"] == 1
    assert result["complete"] is True


@pytest.mark.asyncio
async def test_source_manifest_hashes_large_binary_without_text_or_aggregate_limit(tmp_path):
    data = b"\x00\xff" * 600_000
    (tmp_path / "binary.dat").write_bytes(data)
    (tmp_path / "source.py").write_text("print(1)\n")
    actual = await WorkspaceTools(tmp_path).source_manifest()
    assert actual == {
        "binary.dat": hashlib.sha256(data).hexdigest(),
        "source.py": hashlib.sha256(b"print(1)\n").hexdigest(),
    }


@pytest.mark.asyncio
async def test_manifest_refuses_unreadable_and_hardlinked_selected_source(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("private")
    os.link(outside, tmp_path / "alias.py")
    with pytest.raises(ValueError, match="link|alias"):
        await WorkspaceTools(tmp_path).source_manifest()


@pytest.mark.asyncio
async def test_search_cursor_exposes_remaining_matches_and_rejects_other_query(tmp_path):
    (tmp_path / "service.py").write_text("needle\n" * 7)
    tools = WorkspaceTools(tmp_path)
    first = await tools.search_text("needle", max_results=3)
    second = await tools.search_text("needle", max_results=3, cursor=first["next_cursor"])
    third = await tools.search_text("needle", max_results=3, cursor=second["next_cursor"])
    assert [m["line"] for page in (first, second, third) for m in page["matches"]] == list(range(1, 8))
    assert third["complete"] is True and third["next_cursor"] is None
    with pytest.raises(ValueError, match="scope|cursor"):
        await tools.search_text("different", max_results=3, cursor=first["next_cursor"])


@pytest.mark.asyncio
async def test_manifest_streams_beyond_old_aggregate_without_large_allocation(tmp_path):
    size = 17 * 1024 * 1024
    with (tmp_path / "large.bin").open("wb") as target:
        target.truncate(size)
    expected = hashlib.sha256()
    for _ in range(size // 65536):
        expected.update(bytes(65536))
    assert (await WorkspaceTools(tmp_path).source_manifest())["large.bin"] == expected.hexdigest()


@pytest.mark.asyncio
async def test_unreadable_selected_source_refuses_instead_of_silently_skipping(tmp_path):
    target = tmp_path / "locked.py"
    target.write_text("hidden needle")
    target.chmod(0)
    try:
        with pytest.raises(ValueError, match="unreadable"):
            await WorkspaceTools(tmp_path).source_manifest()
    finally:
        target.chmod(0o600)


def test_native_reader_paging_options_are_explicit_strict_and_source_bound():
    from firefly_forge.source_edit_models import SourceReadRequest

    request = SourceReadRequest(tool="list_files", request_id="a" * 32, path="services", cursor=None, page_size=2)
    assert request.page_size == 2
    with pytest.raises(ValueError):
        SourceReadRequest(tool="read_file", request_id="a" * 32, path="service.py", cursor="bad")


@pytest.mark.asyncio
async def test_page_response_window_never_becomes_a_project_total(tmp_path):
    for i in range(20):
        (tmp_path / (str(i).zfill(2) + "-source-name.py")).write_text("source")
    tools = WorkspaceTools(tmp_path, max_page_bytes=800)
    cursor, found = None, []
    while True:
        page = await tools.list_files_page(cursor=cursor)
        import json

        assert len(json.dumps(page).encode()) <= 800
        found.extend(page["files"])
        if page["complete"]:
            break
        assert page["next_cursor"] and page["files"]
        cursor = page["next_cursor"]
    assert found == await tools.list_files() and len(found) == 20


@pytest.mark.asyncio
async def test_invalid_utf8_is_reported_and_long_line_excerpts_disclose_truncation(tmp_path):
    (tmp_path / "binary.dat").write_bytes(b"\xff\xfe")
    (tmp_path / "long.txt").write_text("x" * 70000 + "needle\n")
    result = await WorkspaceTools(tmp_path).search_text("needle")
    assert result["classifications"]["invalid_utf8"] == 1
    assert result["matches"][0]["snippet_truncated"] is True
    assert len(result["matches"][0]["snippet"]) == 300


@pytest.mark.asyncio
async def test_native_mcp_exposes_versioned_paging_without_new_tools():
    import time

    import httpx

    from firefly_forge.coding_check_transport import CodingCheckServer

    async def read(request):
        raise AssertionError("A schema read must not inspect project source")

    server = CodingCheckServer(None, expires_at_ms=int(time.time() * 1000) + 30000, source_callback=read)
    app = server.application(19433)
    headers = {"authorization": "Bearer " + server.token, "accept": "application/json, text/event-stream"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:19433") as client,
    ):
        response = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {x["name"]: x for x in response.json()["result"]["tools"]}
    assert set(tools) == {"list_files", "read_file", "search_text"}
    assert {"path", "cursor", "page_size"} <= set(tools["list_files"]["inputSchema"]["properties"])
    assert "cursor" in tools["search_text"]["inputSchema"]["properties"]
    assert "complete=true" in tools["list_files"]["description"]


@pytest.mark.asyncio
async def test_git_selection_includes_ignored_tracked_source_and_excludes_reproducible_dependencies(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("node_modules/\nignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored/tracked.py").write_text("tracked needle\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/cache.js").write_text("dependency")
    (tmp_path / "new.py").write_text("new source")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", ".gitignore", "ignored/tracked.py"], check=True)
    tools = WorkspaceTools(tmp_path)
    assert await tools.list_files() == [".gitignore", "ignored/tracked.py", "new.py"]
    assert "node_modules/cache.js" not in await tools.source_manifest()
    found = await tools.search_text("tracked needle", path_prefix="ignored")
    assert found["matches"][0]["path"] == "ignored/tracked.py"


@pytest.mark.asyncio
async def test_git_component_scope_stays_confined_to_its_declared_repository(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    component = tmp_path / "component"
    component.mkdir()
    (component / "service.py").write_text("inside")
    (tmp_path / "outside.py").write_text("sibling")
    tools = WorkspaceTools(component, repository_root=tmp_path)
    assert await tools.list_files() == ["service.py"]
    page = await tools.list_files_page()
    assert page["files"] == ["service.py"] and page["complete"] is True


@pytest.mark.asyncio
async def test_present_malformed_git_metadata_never_falls_back_to_filesystem(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "service.py").write_text("source")
    with pytest.raises(ValueError):
        await WorkspaceTools(tmp_path).list_files_page()


@pytest.mark.asyncio
async def test_selected_git_link_refuses_instead_of_disappearing_from_manifest(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "source.py").write_text("source")
    (tmp_path / "link.py").symlink_to("source.py")
    subprocess.run(["git", "-C", str(tmp_path), "add", "source.py", "link.py"], check=True)
    with pytest.raises(ValueError, match="link|regular"):
        await WorkspaceTools(tmp_path).source_manifest()


@pytest.mark.asyncio
async def test_boolean_cursor_version_is_not_an_integer_protocol_version(tmp_path):
    import base64
    import json

    (tmp_path / "a.py").write_text("A")
    (tmp_path / "b.py").write_text("B")
    tools = WorkspaceTools(tmp_path)
    page = await tools.list_files_page(page_size=1)
    token = json.loads(base64.urlsafe_b64decode(page["next_cursor"]))
    token["version"] = True
    encoded = base64.urlsafe_b64encode(
        json.dumps(token, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).decode()
    with pytest.raises(ValueError, match="cursor|scope"):
        await tools.list_files_page(cursor=encoded, page_size=1)


@pytest.mark.asyncio
async def test_real_agentic_command_tool_tracks_large_binary_effects_completely(tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.agentic_runtime import execute_agentic
    from firefly_forge.process import ProcessResult
    from firefly_forge.workspace_tools import ToolRuntimeContext

    target = tmp_path / "large.bin"
    with target.open("wb") as stream:
        stream.truncate(17 * 1024 * 1024)
    calls = []

    async def command(argv, timeout):
        calls.append((argv, timeout))
        with target.open("r+b") as stream:
            stream.seek(16 * 1024 * 1024 + 3)
            stream.write(b"\xff")
        return ProcessResult(argv, 0, "Controlled command completed")

    def model(messages, info):
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returned:
            return ModelResponse(
                parts=[ToolCallPart("execute_command", {"argv": ["fixture", "write-binary"], "timeout": 1})]
            )
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Observed a binary source change"})]
        )

    async def emit(kind, message):
        pass

    summary, usage = await execute_agentic(
        tmp_path,
        "Observe source",
        "pyfly",
        "none",
        emit,
        model=FunctionModel(model),
        runtime_context=ToolRuntimeContext(
            tmp_path, tmp_path, allowed_tools=frozenset({"execute_command"}), command_executor=command
        ),
    )
    assert summary == "Observed a binary source change"
    assert usage["files_written"] == ["large.bin"]
    assert calls == [(["fixture", "write-binary"], 1)]


@pytest.mark.asyncio
async def test_unreadable_standalone_directory_cannot_look_like_complete_empty_source(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "source.py").write_text("source")
    locked.chmod(0)
    try:
        with pytest.raises(ValueError, match="unreadable"):
            await WorkspaceTools(tmp_path).list_files_page()
    finally:
        locked.chmod(0o700)


@pytest.mark.asyncio
async def test_file_page_refuses_selection_change_during_listing(tmp_path, monkeypatch):
    (tmp_path / "first.py").write_text("first")
    tools = WorkspaceTools(tmp_path)
    original = tools._inventory
    calls = 0

    async def changing(*args):
        nonlocal calls
        value = await original(*args)
        calls += 1
        if calls == 1:
            (tmp_path / "late.py").write_text("late")
        return value

    monkeypatch.setattr(tools, "_inventory", changing)
    with pytest.raises(ValueError, match="changed"):
        await tools.list_files_page()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, "replacement.py"])
@pytest.mark.parametrize("git_repository", [False, True])
async def test_real_agentic_command_tool_records_deleted_source_paths(tmp_path, replacement, git_repository):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.agentic_checkpoint_boundary import _State
    from firefly_forge.agentic_runtime import execute_agentic
    from firefly_forge.process import ProcessResult
    from firefly_forge.workspace_tools import ToolRuntimeContext

    target = tmp_path / "obsolete.py"
    target.write_text("obsolete = True\n")
    if git_repository:
        import subprocess

        (tmp_path / "keep.py").write_text("KEEP = True\n")
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "keep.py", "obsolete.py"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "Seed source",
            ],
            check=True,
        )
    calls = []

    async def command(argv, timeout):
        calls.append((argv, timeout))
        if replacement is None:
            target.unlink()
        else:
            target.rename(tmp_path / replacement)
        return ProcessResult(argv, 0, "Controlled refactor completed")

    def model(messages, info):
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returned:
            return ModelResponse(
                parts=[ToolCallPart("execute_command", {"argv": ["fixture", "remove-obsolete"], "timeout": 1})]
            )
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Removed obsolete source"})])

    async def emit(kind, message):
        pass

    summary, usage = await execute_agentic(
        tmp_path,
        "Remove obsolete source",
        "pyfly",
        "none",
        emit,
        model=FunctionModel(model),
        runtime_context=ToolRuntimeContext(
            tmp_path, tmp_path, allowed_tools=frozenset({"execute_command"}), command_executor=command
        ),
    )
    expected = sorted(["obsolete.py", *([replacement] if replacement is not None else [])])
    assert summary == "Removed obsolete source"
    assert usage["files_written"] == expected
    assert _State.model_validate({"files_written": expected}).files_written == expected
    assert not target.exists()
    assert calls == [(["fixture", "remove-obsolete"], 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["obsolete.py", "removed/obsolete.py"])
async def test_git_tracked_absence_is_omitted_from_current_source_views(tmp_path, path):
    import subprocess

    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("obsolete = True\n")
    (tmp_path / "keep.py").write_text("KEEP = True\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    target.unlink()
    if target.parent != tmp_path:
        target.parent.rmdir()
    tools = WorkspaceTools(tmp_path)
    assert await tools.list_files() == ["keep.py"]
    assert (await tools.list_files_page())["files"] == ["keep.py"]
    assert await tools.source_manifest() == {"keep.py": hashlib.sha256(b"KEEP = True\n").hexdigest()}
    assert (await tools.search_text("obsolete"))["matches"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("transient", [False, True])
async def test_git_tracked_absence_reappearing_during_inventory_is_not_hidden(tmp_path, monkeypatch, transient):
    import subprocess

    from firefly_forge import checkpoint_source_identity

    target = tmp_path / "obsolete.py"
    target.write_text("source")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "obsolete.py"], check=True)
    target.unlink()
    original = checkpoint_source_identity.stream_source_entries

    async def changed(workspace, consume):
        await original(workspace, consume)
        target.write_text("replacement")
        if transient:
            target.unlink()

    monkeypatch.setattr(checkpoint_source_identity, "stream_source_entries", changed)
    with pytest.raises(ValueError, match="absen|changed"):
        await WorkspaceTools(tmp_path).source_manifest()


@pytest.mark.asyncio
async def test_disappearing_untracked_git_source_still_refuses(tmp_path, monkeypatch):
    import subprocess

    from firefly_forge import checkpoint_source_identity

    target = tmp_path / "new.py"
    target.write_text("source")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    original = checkpoint_source_identity.stream_source_entries

    async def changed(workspace, consume):
        async def disappear(entry):
            target.unlink()
            await consume(entry)

        await original(workspace, disappear)

    monkeypatch.setattr(checkpoint_source_identity, "stream_source_entries", changed)
    with pytest.raises(ValueError, match="unreadable|changed"):
        await WorkspaceTools(tmp_path).source_manifest()


@pytest.mark.asyncio
async def test_missing_tracked_file_under_replaced_symlink_parent_still_refuses(tmp_path):
    import subprocess

    folder = tmp_path / "removed"
    folder.mkdir()
    target = folder / "obsolete.py"
    target.write_text("source")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "removed/obsolete.py"], check=True)
    target.unlink()
    folder.rmdir()
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    folder.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unreadable|linked"):
        await WorkspaceTools(tmp_path).source_manifest()
