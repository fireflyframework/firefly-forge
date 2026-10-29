# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Runtime aliases cannot expand a build measurement into private owner inputs."""

import os
import sys

import pytest
from test_verification_scope_commit_host import build_scope_case as build_scope_case

from firefly_forge import verification_scope_attestation as scope


def runtime(prefix):
    (prefix / "bin").mkdir(parents=True)
    executable = prefix / "bin" / "python"
    executable.write_text("fictional executable; never launched\n")
    library = prefix / "lib" / "python3.13"
    (library / "encodings").mkdir(parents=True)
    (library / "os.py").write_text("fictional runtime metadata\n")
    (library / "encodings" / "__init__.py").write_text("fictional runtime metadata\n")
    return executable


def check(executable):
    return scope.BuildCheckV1(
        command_index=1,
        kind="python_static",
        argv=[str(executable), "-B", "-m", "ruff"],
        wrapped_argv=["wrapper", "-p", "profile", str(executable)],
    )


def test_private_owner_alias_cannot_become_a_runtime_root(tmp_path, monkeypatch):
    home, workspace = tmp_path / "owner", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    outside = tmp_path / "outside"
    runtime(outside)
    (home / ".ssh").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(ValueError, match="private|ancestor"):
        scope._roots([check(home / ".ssh" / "bin" / "python")], workspace, {"HOME": str(home)})


@pytest.mark.skipif(sys.platform != "darwin", reason="Native case aliases depend on filesystem identity")
@pytest.mark.parametrize("boundary", ["owner", "private"])
def test_native_case_alias_cannot_bypass_runtime_boundary(tmp_path, monkeypatch, boundary):
    home, workspace = tmp_path / "owner", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if boundary == "owner":
        runtime(home)
        alias = home.with_name("oWNER")
        if not alias.is_dir():
            pytest.skip("This volume is case-sensitive")
        executable = alias / "bin" / "python"
    else:
        executable = runtime(home / ".SSH" / "runtime")
        if not (home / ".ssh").is_dir():
            pytest.skip("This volume is case-sensitive")
    with pytest.raises(ValueError, match="private|ancestor"):
        scope._roots([check(executable)], workspace, {"HOME": str(home)})


@pytest.mark.skipif(sys.platform != "darwin", reason="Confined source-check scope requires Darwin host")
async def test_scope_refuses_nested_private_target_before_payload_open(build_scope_case, tmp_path, monkeypatch):
    c = build_scope_case
    home = tmp_path / "owner"
    private = home / ".ssh"
    private.mkdir(parents=True)
    secret = private / "fixture-private-key"
    secret.write_text("fictional private input\n")
    (c.dependencies / "linked-key").symlink_to(secret)
    monkeypatch.setenv("HOME", str(home))
    original_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        assert path != secret.name, "Private target payload was opened before refusal"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(ValueError, match="private|ancestor"):
        await scope.prepare_build_scope(c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current)


def test_public_alias_into_private_owner_runtime_is_refused(tmp_path, monkeypatch):
    home, workspace = tmp_path / "owner", tmp_path / "workspace"
    workspace.mkdir()
    runtime(home / ".ssh" / "runtime")
    alias = tmp_path / "public-alias"
    alias.symlink_to(home / ".ssh" / "runtime", target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(ValueError, match="private|ancestor"):
        scope._roots([check(alias / "bin" / "python")], workspace, {"HOME": str(home)})


def test_relative_path_lookup_cannot_hash_a_different_cwd_runtime(tmp_path, monkeypatch):
    launcher, workspace = tmp_path / "launcher", tmp_path / "workspace"
    workspace.mkdir()
    executable = runtime(launcher)
    executable.chmod(0o700)
    monkeypatch.chdir(launcher)
    monkeypatch.setenv("PATH", "bin")
    with pytest.raises(ValueError, match="explicit executable"):
        scope._roots([check("python")], workspace, {"PATH": "bin"})


@pytest.mark.skipif(sys.platform != "darwin", reason="Confined source-check scope requires Darwin host")
async def test_explicit_private_prerequisite_stays_hmac_bound_without_dependency_read_grant(
    build_scope_case, tmp_path, monkeypatch
):
    c = build_scope_case
    home = tmp_path / "owner"
    private = home / ".ssh"
    private.mkdir(parents=True)
    secret = private / "fixture-private-key"
    secret.write_text("explicit fictional private prerequisite\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("QA_BUILD_INPUT_FILE", str(secret))
    prepared = await scope.prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert prepared is not None
    assert all(not root.startswith(str(private)) for root in prepared.witness.dependency_roots)
    assert "explicit fictional private prerequisite" not in prepared.witness.model_dump_json()
    await prepared.fence()


@pytest.mark.skipif(sys.platform != "darwin", reason="Confined source-check scope requires Darwin host")
async def test_declared_public_link_is_supported_but_late_private_retarget_refuses_before_open(
    build_scope_case, tmp_path, monkeypatch
):
    c = build_scope_case
    home, public = tmp_path / "owner", tmp_path / "public-runtime"
    private = home / ".ssh"
    private.mkdir(parents=True)
    public.mkdir()
    target = public / "module.py"
    target.write_text("VALUE = 1\n")
    secret = private / "fixture-private-key"
    secret.write_text("fictional private input\n")
    alias = c.dependencies / "linked-module"
    alias.symlink_to(target)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(scope, "_roots", lambda *args: [c.dependencies, public])
    prepared = await scope.prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert prepared is not None
    await prepared.fence()
    alias.unlink()
    alias.symlink_to(secret)
    original_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        assert path != secret.name, "Retargeted private payload was opened before refusal"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(ValueError):
        await prepared.fence()
    with pytest.raises(ValueError, match="private|ancestor"):
        await prepared.revalidate()


@pytest.mark.skipif(sys.platform != "darwin", reason="Confined source-check scope requires Darwin host")
@pytest.mark.parametrize("location", ["state", "source", "dependency"])
async def test_measurement_refuses_private_state_source_and_nested_configuration_before_open(
    build_scope_case, monkeypatch, location
):
    c = build_scope_case
    secret = {
        "state": c.store.path,
        "source": c.workspace / ".env",
        "dependency": c.dependencies / ".npmrc",
    }[location]
    secret.write_text("fictional private input\n")
    if location != "dependency":
        (c.dependencies / "linked-private-input").symlink_to(secret)
    original_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        assert path != secret.name, "Private dependency input opened before refusal"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(ValueError, match="private|ancestor"):
        await scope.prepare_build_scope(c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current)


@pytest.mark.skipif(sys.platform != "darwin", reason="Confined source-check scope requires Darwin host")
async def test_selected_workspace_in_state_and_bound_public_package_data_remain_supported(
    build_scope_case, monkeypatch
):
    c = build_scope_case
    destination = c.store.path.parent / "workspaces" / "selected"
    destination.parent.mkdir()
    c.workspace.rename(destination)
    c.workspace = destination
    c.intent["workspace"] = str(destination)
    package = destination / "frontend" / "node_modules" / "fixture-package"
    package.mkdir(parents=True)
    certificate = package / "public-ca.pem"
    certificate.write_text("fictional declared package data; no public-certificate claim\n")
    (destination / ".env.example").write_text("PUBLIC_EXAMPLE=1\n")
    monkeypatch.setattr(scope, "_roots", lambda *args: [package.parent])
    prepared = await scope.prepare_build_scope(
        c.store, c.journal, c.engine, c.run, c.intent, dict(digest="7" * 64), c.current
    )
    assert prepared is not None
    await prepared.fence()
    certificate.write_text("changed declared package data\n")
    with pytest.raises(ValueError):
        await prepared.fence()
