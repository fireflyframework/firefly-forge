# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Archive runners must not import untrusted paths or overwrite concurrent host edits."""

import io
import os
import tarfile

import pytest

from firefly_forge.sandbox_models import SandboxInput, capture_sandbox, validate_sandbox_snapshot


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as stream:
        for name, kind, content in entries:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.size = len(content) if kind == tarfile.REGTYPE else 0
            item.linkname = "/outside"
            stream.addfile(item, io.BytesIO(content) if item.isfile() else None)
    return output.getvalue()


def test_archive_policy_freezes_explicit_transport_and_rejects_host_mode():
    config = SandboxInput(
        mode="docker", image="runner@sha256:" + "a" * 64, context="private", workspace_transport="archive"
    )
    frozen = capture_sandbox(config)
    assert validate_sandbox_snapshot(frozen).workspace_transport == "archive"
    assert "workspace_archive" in frozen["enforced_controls"]
    with pytest.raises(ValueError):
        SandboxInput(workspace_transport="archive")


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../outside", tarfile.REGTYPE),
        ("/outside", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("link", tarfile.LNKTYPE),
        ("pipe", tarfile.FIFOTYPE),
    ],
)
def test_archive_import_rejects_escape_links_and_special_files(tmp_path, name, kind):
    from firefly_forge.sandbox import unpack_workspace

    with pytest.raises(ValueError):
        unpack_workspace(archive([(name, kind, b"payload")]), tmp_path, 1024)
    assert not list(tmp_path.iterdir())


def test_archive_import_validates_whole_archive_before_writing(tmp_path):
    from firefly_forge.sandbox import unpack_workspace

    data = archive([("safe.py", tarfile.REGTYPE, b"safe"), ("../escape", tarfile.REGTYPE, b"unsafe")])
    with pytest.raises(ValueError):
        unpack_workspace(data, tmp_path, 1024)
    assert not list(tmp_path.iterdir())


def test_archive_input_excludes_credentials_and_git_and_rejects_links(tmp_path):
    from firefly_forge.sandbox import pack_workspace

    (tmp_path / "main.py").write_text("print('ok')")
    (tmp_path / ".env").write_text("PASSWORD=private")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("private")
    data, _ = pack_workspace(tmp_path, 1024)
    with tarfile.open(fileobj=io.BytesIO(data)) as stream:
        assert [name for name in stream.getnames() if name != "."] == ["main.py"]
    (tmp_path / "escape").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="regular"):
        pack_workspace(tmp_path, 1024)


def test_archive_import_preserves_metadata_and_rejects_stale_host(tmp_path):
    from firefly_forge.sandbox import collect_workspace, pack_workspace

    (tmp_path / "main.py").write_text("original")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("original-git")
    _, before = pack_workspace(tmp_path, 1024)
    data = archive([("main.py", tarfile.REGTYPE, b"result"), (".git/config", tarfile.REGTYPE, b"evil")])
    collect_workspace(data, tmp_path, 1024, before)
    assert (tmp_path / "main.py").read_text() == "result"
    assert (tmp_path / ".git" / "config").read_text() == "original-git"
    (tmp_path / "main.py").write_text("concurrent host edit")
    with pytest.raises(ValueError, match="changed"):
        collect_workspace(data, tmp_path, 1024, before)
    assert (tmp_path / "main.py").read_text() == "concurrent host edit"


def test_archive_input_and_output_sizes_are_bounded(tmp_path):
    from firefly_forge.sandbox import pack_workspace, unpack_workspace

    (tmp_path / "large").write_bytes(b"a" * 1025)
    with pytest.raises(ValueError, match="limit"):
        pack_workspace(tmp_path, 1024)
    with pytest.raises(ValueError, match="limit"):
        unpack_workspace(archive([("large", tarfile.REGTYPE, b"a" * 1025)]), tmp_path / "output", 1024)


def test_archive_root_is_owned_by_nonroot_runner_account(tmp_path):
    import os

    from firefly_forge.sandbox import pack_workspace

    data, _ = pack_workspace(tmp_path, 1024)
    with tarfile.open(fileobj=io.BytesIO(data)) as stream:
        root = next((item for item in stream if item.name == "."), None)
        assert root is not None and root.isdir()
        assert root.uid == os.getuid() and root.gid == os.getgid()
        assert root.mode == 0o755


def test_archive_input_and_output_reject_configured_credentials(tmp_path):
    from firefly_forge.sandbox import pack_workspace, unpack_workspace

    registry = {"PROJECT_TOKEN": "private-project-token"}
    (tmp_path / "source.txt").write_text("private-project-token")
    with pytest.raises(ValueError, match="credentials"):
        pack_workspace(tmp_path, 1024, registry=registry)
    with pytest.raises(ValueError, match="credentials"):
        unpack_workspace(
            archive([("source.txt", tarfile.REGTYPE, b"private-project-token")]),
            tmp_path / "result",
            1024,
            registry=registry,
        )
    assert not (tmp_path / "result").exists()


def test_archive_input_rejects_hardlinks(tmp_path):
    import os

    from firefly_forge.sandbox import pack_workspace

    (tmp_path / "source.txt").write_text("safe")
    os.link(tmp_path / "source.txt", tmp_path / "alias.txt")
    with pytest.raises(ValueError, match="regular"):
        pack_workspace(tmp_path, 1024)


def test_archive_input_rejects_credential_in_filename(tmp_path):
    from firefly_forge.sandbox import pack_workspace

    (tmp_path / "private-project-token").write_text("safe content")
    with pytest.raises(ValueError, match="credentials"):
        pack_workspace(tmp_path, 1024, registry={"PROJECT_TOKEN": "private-project-token"})


def test_archive_import_checks_existing_destination_links_before_writing(tmp_path):
    from firefly_forge.sandbox import unpack_workspace

    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "escape").symlink_to(outside, target_is_directory=True)
    data = archive([("safe.py", tarfile.REGTYPE, b"safe"), ("escape/file", tarfile.REGTYPE, b"unsafe")])
    with pytest.raises(ValueError, match="link"):
        unpack_workspace(data, destination, 1024)
    assert not (destination / "safe.py").exists()
    assert not (outside / "file").exists()


def test_legacy_bind_snapshots_remain_readable_without_archive_fields():
    import copy
    import hashlib
    import json

    config = SandboxInput(mode="docker", image="runner@sha256:" + "a" * 64, context="private")
    legacy = copy.deepcopy(capture_sandbox(config))
    legacy["configuration"].pop("workspace_transport")
    legacy["configuration"].pop("workspace_max_bytes")
    legacy["digest"] = hashlib.sha256(
        json.dumps(legacy["configuration"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert validate_sandbox_snapshot(legacy).workspace_transport == "bind"


def test_archive_collection_tracks_deletes_and_executable_mode(tmp_path):
    from firefly_forge.sandbox import collect_workspace, pack_workspace

    (tmp_path / "obsolete").write_text("remove")
    (tmp_path / ".env").write_text("preserve local")
    _, original = pack_workspace(tmp_path, 1024)
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as stream:
        item = tarfile.TarInfo("command.sh")
        item.size, item.mode = 5, 0o755
        stream.addfile(item, io.BytesIO(b"run()"))
    collect_workspace(data.getvalue(), tmp_path, 1024, original)
    assert not (tmp_path / "obsolete").exists()
    assert (tmp_path / ".env").read_text() == "preserve local"
    assert (tmp_path / "command.sh").stat().st_mode & 0o111


@pytest.fixture
async def native_archive(store, tmp_path):
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor

    await store.acquire_ownership()
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "main.py").write_text("original")
    repo = await store.add_repository(RepositoryInput(name="archive", url="https://github.com/team/archive.git"))
    config = SandboxInput(
        mode="docker",
        context=os.environ.get("FORGE_TEST_DOCKER_CONTEXT", "colima-forge-tests"),
        image=os.environ["FORGE_TEST_DOCKER_IMAGE"],
        workspace_transport="archive",
        workspace_max_bytes=1_048_576,
    )
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Archive boundary", sandbox=config))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    return SandboxExecutor(store, journal), run, workspace, config, journal


@pytest.mark.skipif(not os.environ.get("FORGE_TEST_DOCKER_IMAGE"), reason="Native Docker image not configured")
@pytest.mark.parametrize(
    "script,reason",
    [
        ("import pathlib;pathlib.Path('escape').symlink_to('/etc/passwd')", "regular"),
        ("import pathlib;pathlib.Path('large').write_bytes(b'x'*1048577)", "limit"),
    ],
)
async def test_actual_archive_rejects_untrusted_results_and_removes_volumes(native_archive, script, reason):
    runner, run, workspace, config, journal = native_archive
    with pytest.raises(ValueError, match=reason):
        await runner.execute(run["id"], workspace, workspace, ["/app/.venv/bin/python", "-c", script], 10, "verify")
    assert (workspace / "main.py").read_text() == "original"
    assert not (workspace / "escape").exists() and not (workspace / "large").exists()
    assert (await journal.sandbox_executions(run["id"]))[0]["status"] == "removed"
    result = await runner._docker(config, ["ps", "-aq", "--filter", f"label=org.firefly.forge.run={run['id']}"])
    assert result.output.strip() == ""
    assert journal.verify_chain(await journal.entries(run["id"]))


@pytest.mark.skipif(not os.environ.get("FORGE_TEST_DOCKER_IMAGE"), reason="Native Docker image not configured")
async def test_actual_archive_preserves_concurrent_host_edit_and_cleans_up(native_archive):
    import asyncio

    runner, run, workspace, config, journal = native_archive
    command = asyncio.create_task(
        runner.execute(
            run["id"],
            workspace,
            workspace,
            [
                "/app/.venv/bin/python",
                "-c",
                "import pathlib,time;time.sleep(2);pathlib.Path('main.py').write_text('runner edit')",
            ],
            10,
            "verify",
        )
    )
    try:
        async with asyncio.timeout(10):
            while True:
                result = await runner._docker(
                    config,
                    ["ps", "-q", "--filter", "status=running", "--filter", f"label=org.firefly.forge.run={run['id']}"],
                )
                if result.output.strip():
                    break
                await asyncio.sleep(0.05)
        (workspace / "main.py").write_text("concurrent host edit")
        with pytest.raises(ValueError, match="changed"):
            await command
        assert (workspace / "main.py").read_text() == "concurrent host edit"
        assert (await journal.sandbox_executions(run["id"]))[0]["status"] == "removed"
    finally:
        if not command.done():
            command.cancel()
            with __import__("contextlib").suppress(BaseException):
                await command


@pytest.mark.skipif(not os.environ.get("FORGE_TEST_DOCKER_IMAGE"), reason="Native Docker image not configured")
async def test_actual_archive_failed_worker_create_cleans_helper_and_records_receipt(store, tmp_path):
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor

    await store.acquire_ownership()
    repo = await store.add_repository(RepositoryInput(name="missing", url="https://github.com/team/missing.git"))
    config = SandboxInput(
        mode="docker",
        context=os.environ.get("FORGE_TEST_DOCKER_CONTEXT", "colima-forge-tests"),
        image="missing-runner@sha256:" + "f" * 64,
        workspace_transport="archive",
    )
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Missing image", sandbox=config))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    runner = SandboxExecutor(store, journal)
    with pytest.raises(RuntimeError, match="create"):
        await runner.execute(run["id"], tmp_path, tmp_path, ["touch", "host-fallback"], 10, "verify")
    assert not (tmp_path / "host-fallback").exists()
    row = (await journal.sandbox_executions(run["id"]))[0]
    assert row["status"] == "removed" and row["effects"]["cleanup"]["status"] == "done"
    result = await runner._docker(config, ["ps", "-aq", "--filter", f"label=org.firefly.forge.run={run['id']}"])
    assert result.output.strip() == ""
    assert journal.verify_chain(await journal.entries(run["id"]))
