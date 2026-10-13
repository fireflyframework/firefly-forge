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


def test_pack_rejects_link_swapped_at_file_open(tmp_path, monkeypatch):
    from pathlib import Path

    from firefly_forge.sandbox import pack_workspace

    source = tmp_path / "source"
    source.mkdir()
    target = source / "main.py"
    target.write_text("inside")
    outside = tmp_path / "outside"
    outside.write_text("secret")
    path_open, fd_open = Path.open, os.open
    attacked = False

    def attack():
        nonlocal attacked
        if not attacked:
            attacked = True
            target.unlink()
            target.symlink_to(outside)

    def path_read(path, *args, **kwargs):
        if path == target:
            attack()
        return path_open(path, *args, **kwargs)

    def fd_read(path, flags, *args, **kwargs):
        if str(path) in {"main.py", str(target)} and not flags & os.O_DIRECTORY:
            attack()
        return fd_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(Path, "open", path_read)
    monkeypatch.setattr(os, "open", fd_read)
    with pytest.raises(ValueError, match="regular|link|changed"):
        pack_workspace(source, 1024)
    assert attacked and outside.read_text() == "secret"


def test_collect_link_swap_after_digest_never_writes_outside(tmp_path, monkeypatch):
    from firefly_forge import sandbox

    workspace = tmp_path / "source"
    workspace.mkdir()
    target = workspace / "main.py"
    target.write_text("original")
    outside = tmp_path / "outside"
    outside.write_text("preserved")
    original_pack = sandbox.pack_workspace
    _, original = original_pack(workspace, 1024)
    attacked = False

    def pack(path, *args, **kwargs):
        nonlocal attacked
        result = original_pack(path, *args, **kwargs)
        if path == workspace and not attacked:
            attacked = True
            target.unlink()
            target.symlink_to(outside)
        return result

    monkeypatch.setattr(sandbox, "pack_workspace", pack)
    with pytest.raises(ValueError, match="regular|link|changed"):
        sandbox.collect_workspace(archive([("main.py", tarfile.REGTYPE, b"runner edit")]), workspace, 1024, original)
    assert attacked and outside.read_text() == "preserved"


def test_collect_failed_copy_preserves_original_tree(tmp_path, monkeypatch):
    import errno

    from firefly_forge.sandbox import collect_workspace, pack_workspace

    (tmp_path / "obsolete").write_text("preserve original")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("preserve metadata")
    _, original = pack_workspace(tmp_path, 1024)

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "injected filesystem full")

    monkeypatch.setattr(os, "fsync", disk_full)
    with pytest.raises(OSError, match="filesystem full"):
        collect_workspace(archive([("main.py", tarfile.REGTYPE, b"runner edit")]), tmp_path, 1024, original)
    assert (tmp_path / "obsolete").read_text() == "preserve original"
    assert (tmp_path / ".git" / "config").read_text() == "preserve metadata"
    assert not (tmp_path / "main.py").exists()


def test_collect_rolls_back_concurrent_change_after_atomic_exchange(tmp_path, monkeypatch):
    from firefly_forge import sandbox

    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "main.py").write_text("original")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("git retained")
    _, original = sandbox.pack_workspace(workspace, 1024)
    exchange = sandbox._exchange_trees
    attacked = False

    def exchange_and_edit(parent, first, second):
        nonlocal attacked
        exchange(parent, first, second)
        if not attacked:
            attacked = True
            (tmp_path / second / "main.py").write_text("concurrent host edit")

    monkeypatch.setattr(sandbox, "_exchange_trees", exchange_and_edit)
    with pytest.raises(ValueError, match="changed"):
        sandbox.collect_workspace(archive([("main.py", tarfile.REGTYPE, b"runner edit")]), workspace, 1024, original)
    assert (workspace / "main.py").read_text() == "concurrent host edit"
    assert (workspace / ".git" / "config").read_text() == "git retained"


def test_collect_rolls_back_failed_publication_sync(tmp_path, monkeypatch):
    import errno

    from firefly_forge import sandbox

    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "main.py").write_text("original")
    _, original = sandbox.pack_workspace(workspace, 1024)
    exchange, sync = sandbox._exchange_trees, os.fsync
    exchanged = failed = False

    def tracked_exchange(*args):
        nonlocal exchanged
        exchange(*args)
        exchanged = not exchanged

    def publication_sync(descriptor):
        nonlocal failed
        if exchanged and not failed:
            failed = True
            raise OSError(errno.ENOSPC, "publication filesystem full")
        return sync(descriptor)

    monkeypatch.setattr(sandbox, "_exchange_trees", tracked_exchange)
    monkeypatch.setattr(os, "fsync", publication_sync)
    with pytest.raises(OSError, match="filesystem full"):
        sandbox.collect_workspace(archive([("main.py", tarfile.REGTYPE, b"runner edit")]), workspace, 1024, original)
    assert failed and not exchanged
    assert (workspace / "main.py").read_text() == "original"


def test_collect_retains_original_backup_when_filesystem_rejects_rollback(tmp_path, monkeypatch):
    import errno

    from firefly_forge import sandbox

    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "main.py").write_text("original")
    _, original = sandbox.pack_workspace(workspace, 1024)
    exchange, sync = sandbox._exchange_trees, os.fsync
    exchanged = False

    def failed_rollback(*args):
        nonlocal exchanged
        if exchanged:
            raise OSError(errno.EIO, "rollback filesystem unavailable")
        exchange(*args)
        exchanged = True

    def failed_sync(descriptor):
        if exchanged:
            raise OSError(errno.ENOSPC, "publication filesystem full")
        return sync(descriptor)

    monkeypatch.setattr(sandbox, "_exchange_trees", failed_rollback)
    monkeypatch.setattr(os, "fsync", failed_sync)
    with pytest.raises(sandbox.SandboxReconciliationRequired) as caught:
        sandbox.collect_workspace(archive([("main.py", tarfile.REGTYPE, b"runner edit")]), workspace, 1024, original)
    assert (caught.value.backup_path / "main.py").read_text() == "original"
    assert (workspace / "main.py").read_text() == "runner edit"


def test_collect_rejects_root_substitution_between_identity_check_and_exchange(tmp_path, monkeypatch):
    from firefly_forge import sandbox

    workspace, saved = tmp_path / "source", tmp_path / "saved-original"
    workspace.mkdir()
    (workspace / "main.py").write_text("original")
    _, original = sandbox.pack_workspace(workspace, 1024)
    exchange = sandbox._exchange_trees
    attacked = False

    def replace_root_and_exchange(parent, first, second):
        nonlocal attacked
        if not attacked:
            attacked = True
            workspace.rename(saved)
            workspace.mkdir()
            (workspace / "unrelated").write_text("retained replacement root")
        exchange(parent, first, second)

    monkeypatch.setattr(sandbox, "_exchange_trees", replace_root_and_exchange)
    with pytest.raises(ValueError, match="root changed"):
        sandbox.collect_workspace(archive([("main.py", tarfile.REGTYPE, b"runner edit")]), workspace, 1024, original)
    assert (saved / "main.py").read_text() == "original"
    assert (workspace / "unrelated").read_text() == "retained replacement root"


def test_collect_closes_parent_descriptor_when_root_open_fails(tmp_path, monkeypatch):
    from firefly_forge.sandbox import collect_workspace

    source = tmp_path / "source"
    source.symlink_to(tmp_path, target_is_directory=True)
    opened, closed = set(), set()
    open_fd, close_fd = os.open, os.close

    def tracked_open(*args, **kwargs):
        descriptor = open_fd(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def tracked_close(descriptor):
        closed.add(descriptor)
        return close_fd(descriptor)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    try:
        with pytest.raises(ValueError):
            collect_workspace(archive([]), source, 1024, "0" * 64)
        assert opened <= closed
    finally:
        for descriptor in opened - closed:
            close_fd(descriptor)


@pytest.mark.parametrize("filename", [".npmrc", "id_ed25519", "server.pem"])
def test_archive_input_excludes_known_private_file_patterns(tmp_path, filename):
    from firefly_forge.sandbox import pack_workspace

    (tmp_path / filename).write_text("unregistered private credential")
    data, _ = pack_workspace(tmp_path, 1024)
    with tarfile.open(fileobj=io.BytesIO(data)) as stream:
        assert filename not in stream.getnames()


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


@pytest.mark.skipif(not os.environ.get("FORGE_TEST_DOCKER_IMAGE"), reason="Native Docker image not configured")
async def test_actual_archive_failed_publication_retains_command_receipt_without_replay(native_archive, monkeypatch):
    import errno
    from pathlib import Path

    from firefly_forge import sandbox

    runner, run, workspace, config, journal = native_archive
    exchange = sandbox._exchange_trees

    def disk_full(*args):
        raise OSError(errno.ENOSPC, "publication filesystem full")

    monkeypatch.setattr(sandbox, "_exchange_trees", disk_full)
    command = [
        "/app/.venv/bin/python",
        "-c",
        "import pathlib;pathlib.Path('counter').write_text('1');print('command completed once')",
    ]
    with pytest.raises(OSError, match="filesystem full"):
        await runner.execute(run["id"], workspace, workspace, command, 10, "verify", ordinal=1)
    assert (workspace / "main.py").read_text() == "original" and not (workspace / "counter").exists()
    row = (await journal.sandbox_executions(run["id"]))[0]
    receipt = row["effects"]["start"]["evidence"]
    assert row["status"] == "removed"
    assert receipt["exit_code"] == 0 and receipt["output"].strip() == "command completed once"
    assert receipt["workspace_import"] == "requires_reconciliation"
    retained = Path(receipt["archive_path"])
    import hashlib

    assert hashlib.sha256(retained.read_bytes()).hexdigest() == receipt["archive_sha256"]
    with tarfile.open(retained) as stream:
        counter = next(item for item in stream if item.name.rstrip("/").endswith("counter"))
        assert stream.extractfile(counter).read() == b"1"
    monkeypatch.setattr(sandbox, "_exchange_trees", exchange)
    with pytest.raises(sandbox.SandboxReconciliationRequired, match="publication reconciliation"):
        await runner.execute(run["id"], workspace, workspace, command, 10, "verify", ordinal=1)
    assert not (workspace / "counter").exists()
    result = await runner._docker(config, ["ps", "-aq", "--filter", f"label=org.firefly.forge.run={run['id']}"])
    assert not result.output.strip() and journal.verify_chain(await journal.entries(run["id"]))


@pytest.mark.skipif(not os.environ.get("FORGE_TEST_DOCKER_IMAGE"), reason="Native Docker image not configured")
async def test_actual_linux_runner_supports_atomic_directory_exchange(native_archive):
    import inspect

    from firefly_forge.sandbox import _exchange_trees

    runner, run, workspace, _, _ = native_archive
    script = "import ctypes,errno,os,sys,tempfile\n" + inspect.getsource(_exchange_trees) + "\n"
    script += (
        "with tempfile.TemporaryDirectory() as root:\n"
        " os.mkdir(root+'/a');os.mkdir(root+'/b')\n"
        " open(root+'/a/value','w').write('first');open(root+'/b/value','w').write('second')\n"
        " parent=os.open(root,os.O_RDONLY|os.O_DIRECTORY)\n"
        " _exchange_trees(parent,'a','b')\n"
        " assert open(root+'/a/value').read()=='second' and open(root+'/b/value').read()=='first'\n"
        " os.close(parent)\n print('linux atomic exchange verified')\n"
    )
    result = await runner.execute(
        run["id"], workspace, workspace, ["/app/.venv/bin/python", "-I", "-S", "-c", script], 10, "verify"
    )
    assert result.exit_code == 0 and result.output.strip() == "linux atomic exchange verified"
