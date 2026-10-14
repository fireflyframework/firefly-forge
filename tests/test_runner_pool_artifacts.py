# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Artifact custody uses the existing hardened archive implementation."""

import hashlib
import importlib
from uuid import uuid4

import pytest

from firefly_forge.runner_pool_artifacts import source_tree_digest
from firefly_forge.runner_pool_models import RunnerInvocation, RunnerPoolProfile
from firefly_forge.sandbox import pack_workspace

IMAGE = "example/worker@sha256:" + "a" * 64
TRANSFER = "example/transfer@sha256:" + "b" * 64


def artifact_service(root):
    try:
        module = importlib.import_module("firefly_forge.runner_pool_artifacts")
    except ModuleNotFoundError:
        pytest.fail("Private runner artifact custody is not implemented")
    return module.RunnerPoolArtifacts(root)


def fixture_request(tmp_path):
    workspace = tmp_path / "input"
    workspace.mkdir()
    (workspace / "main.py").write_text('print("safe")\n')
    archive, digest = pack_workspace(workspace, 1_048_576)
    pool = RunnerPoolProfile(
        profile_id="test", daemon_id="test-engine", approved_images=(IMAGE,), transfer_image=TRANSFER
    )
    invocation = RunnerInvocation(
        execution_id=uuid4().hex,
        profile_id=pool.profile_id,
        profile_revision=pool.revision,
        repository_id=uuid4(),
        run_id=uuid4(),
        image=IMAGE,
        argv=("python", "main.py"),
        source_digest=source_tree_digest(workspace, 1_048_576),
        archive_digest=digest,
        source_bytes=len(archive),
    )
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    return pool, invocation, archive, root


def test_source_archive_is_hash_checked_retained_and_unpacked_without_private_files(tmp_path):
    pool, request, archive, root = fixture_request(tmp_path)
    service = artifact_service(root)
    source = service.prepare(pool, request, archive)
    assert (source / "main.py").read_text() == 'print("safe")\n'
    assert service.prepare(pool, request, archive) == source
    with pytest.raises(ValueError, match="digest"):
        service.prepare(pool, request, archive[:-1] + b"x")
    assert not (source / ".git").exists()
    assert not (source / ".env").exists()


def test_results_are_immutable_private_and_have_no_host_paths_in_receipt(tmp_path):
    pool, request, archive, root = fixture_request(tmp_path)
    service = artifact_service(root)
    service.prepare(pool, request, archive)
    receipt = service.retain(pool, request, "output", b"completed\n")
    assert receipt == {"kind": "output", "sha256": hashlib.sha256(b"completed\n").hexdigest(), "bytes": 10}
    assert service.retain(pool, request, "output", b"completed\n") == receipt
    with pytest.raises(RuntimeError, match="immutable"):
        service.retain(pool, request, "output", b"changed\n")
    assert all(
        path.stat().st_mode & 0o077 == 0
        for path in root.rglob("*")
        if path.is_file() and path.parent.name == request.execution_id
    )
    with pytest.raises(ValueError):
        service.retain(pool, request, "../peer", b"evil")
    with pytest.raises(ValueError, match="limit"):
        service.retain(pool, request, "output", b"x" * (request.max_output_bytes + 1))


def test_artifacts_reject_configured_credentials_and_symlinked_retention_root(tmp_path):
    pool, request, archive, root = fixture_request(tmp_path)
    service = artifact_service(root)
    service.credential_registry = lambda: {"FORGE_TEST_API_TOKEN": "secret-canary-not-for-project"}
    with pytest.raises(ValueError, match="credential"):
        service.retain(pool, request, "output", b"secret-canary-not-for-project")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="private"):
        artifact_service(alias)


def test_canonical_source_identity_ignores_uid_but_binds_paths_content_and_executable_mode(tmp_path):
    module = importlib.import_module("firefly_forge.runner_pool_artifacts")
    digest = getattr(module, "source_tree_digest", None)
    assert digest is not None, "Cross-UID source identity is not implemented"
    folder = tmp_path / "tree"
    folder.mkdir()
    (folder / "main.py").write_text("print(1)\n")
    first = digest(folder, 1_048_576)
    (folder / "main.py").chmod(0o755)
    assert digest(folder, 1_048_576) != first
    (folder / "main.py").chmod(0o644)
    (folder / "main.py").rename(folder / "other.py")
    assert digest(folder, 1_048_576) != first
    (folder / "other.py").rename(folder / "main.py")
    (folder / "main.py").write_text("print(2)\n")
    assert digest(folder, 1_048_576) != first


def test_cross_uid_archive_retains_raw_hash_and_verifies_canonical_tree(tmp_path):
    import io
    import tarfile

    pool, request, archive, root = fixture_request(tmp_path)
    changed = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as incoming,
        tarfile.open(fileobj=changed, mode="w") as outgoing,
    ):
        for item in incoming:
            item.uid, item.gid = 10001, 10001
            outgoing.addfile(item, incoming.extractfile(item) if item.isfile() else None)
    data = changed.getvalue()
    request = RunnerInvocation.model_validate(
        {**request.model_dump(), "archive_digest": hashlib.sha256(data).hexdigest(), "source_bytes": len(data)}
    )
    source = artifact_service(root).prepare(pool, request, data)
    assert (source / "main.py").read_text() == 'print("safe")\n'
    assert (root / request.execution_id / "source.bin").read_bytes() == data


def test_bounded_stdin_and_carrier_do_not_use_docker_command_arguments(tmp_path):
    import io
    import json
    import tarfile

    pool, request, archive, root = fixture_request(tmp_path)
    payload = b"x" * 200_000
    request = RunnerInvocation.model_validate(
        {**request.model_dump(), "stdin_bytes": len(payload), "stdin_digest": hashlib.sha256(payload).hexdigest()}
    )
    service = artifact_service(root)
    service.prepare(pool, request, archive)
    service.retain(pool, request, "stdin", payload)
    carrier = service.input_archive(pool, request)
    with tarfile.open(fileobj=io.BytesIO(carrier), mode="r:") as stream:
        assert stream.extractfile("stdin.bin").read() == payload
        command = json.load(stream.extractfile("invocation.json"))
        assert command == {
            "argv": ["python", "main.py"],
            "cwd": ".",
            "timeout": 300,
            "execution_id": request.execution_id,
        }
        assert stream.extractfile("source/main.py").read() == b'print("safe")\n'
    assert (
        service.read(pool, request, "stdin", {"kind": "stdin", "bytes": len(payload), "sha256": request.stdin_digest})
        == payload
    )
    with pytest.raises(ValueError, match="digest"):
        service.retain(pool, request, "stdin", b"changed")


async def test_project_bundle_is_staged_separately_and_git_revalidated_before_transfer(tmp_path):
    from pathlib import Path

    from firefly_forge.runner_git_snapshot import RunnerGitSnapshot
    from firefly_forge.runner_pool_bundle import pack_project_bundle
    from firefly_forge.runner_pool_models import RunnerProjectInvocation
    from firefly_forge.sandbox import unpack_workspace

    pool, old, _, root = fixture_request(tmp_path)
    builder = RunnerGitSnapshot(Path("/usr/bin/git"))
    raw, _ = pack_workspace(tmp_path / "input", pool.max_source_bytes)
    metadata = await builder.build(raw, root, pool.max_source_bytes)
    git = tmp_path / "metadata"
    unpack_workspace(metadata, git, pool.max_source_bytes)
    archive, identity = pack_project_bundle(tmp_path / "input", git, pool.max_source_bytes)
    request = RunnerProjectInvocation(
        **{**old.model_dump(), "source_bytes": len(archive), "archive_digest": identity.archive_digest}, bundle=identity
    )
    service = artifact_service(root)
    source = service.prepare(pool, request, archive)
    assert (source / "main.py").read_text() == 'print("safe")\n'
    assert not (source / ".git").exists()
    actual = await service.minimum_git(pool, request, builder)
    assert actual == metadata
    (source.parent / "git" / "config").write_text("[core]\n hooksPath=/outside\n")
    with pytest.raises(ValueError, match="metadata|Git"):
        await service.minimum_git(pool, request, builder)
