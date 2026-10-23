# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Existing private objects receive literal native denies without reading their contents."""

import os
from dataclasses import replace

import pytest
from source_edit_qualification_fixtures import api_configuration, register_fixture_profile

from firefly_forge import source_edit_filesystem_policy as module
from firefly_forge import source_edit_qualification as qualification
from firefly_forge.journal import digest
from firefly_forge.source_edit_cli_policy import configuration_projection


@pytest.mark.parametrize("suffix", ["EXAMPLE", "SAMPLE", "TEMPLATE"])
def test_actual_private_suffix_gets_absolute_deny_while_public_example_remains_allowed(tmp_path, suffix):
    private = tmp_path / "private"
    private.mkdir()
    target = private / (".env." + suffix)
    target.write_text("PUBLIC_TEST_PRIVATE_CONTENT")
    public = tmp_path / "public"
    public.mkdir()
    (public / (".env." + suffix.lower())).write_text("PUBLIC_EXAMPLE")
    filesystem = module.build_named_permissions(tmp_path, ())["forge_source_edit"]["filesystem"]
    assert filesystem[str(target)] == "deny"
    assert str(public / (".env." + suffix.lower())) not in filesystem


def test_private_directories_and_dependency_links_are_not_descended_or_read(tmp_path, monkeypatch):
    private = tmp_path / ".env.EXAMPLE"
    private.mkdir()
    (private / "inside").write_text("PUBLIC_TEST_PRIVATE_CONTENT")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (outside / ".env.SECRET").write_text("PUBLIC_TEST_OUTSIDE")
    (tmp_path / "node_modules").symlink_to(outside, target_is_directory=True)
    original_scandir, original_open = os.scandir, os.open
    visited = []

    def scan(fd):
        assert isinstance(fd, int)
        visited.append(os.fstat(fd).st_ino)
        return original_scandir(fd)

    def open_directory(path, flags, *args, **kwargs):
        assert flags & os.O_DIRECTORY, "No regular-file contents may be opened"
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "scandir", scan)
        patch.setattr(module.os, "open", open_directory)
        filesystem = module.build_named_permissions(tmp_path, ())["forge_source_edit"]["filesystem"]
    assert private.stat().st_ino not in visited and outside.stat().st_ino not in visited
    assert filesystem[str(private)] == "deny"
    assert str(outside / ".env.SECRET") not in filesystem


def test_private_symlink_is_denied_by_its_literal_name_without_following(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    link = tmp_path / ".env.EXAMPLE"
    link.symlink_to(outside, target_is_directory=True)
    filesystem = module.build_named_permissions(tmp_path, ())["forge_source_edit"]["filesystem"]
    assert filesystem[str(link)] == "deny"
    assert str(outside) not in filesystem


def test_hardlinked_public_file_refuses_before_native_policy(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("PUBLIC_SOURCE")
    (tmp_path / "copy.py").hardlink_to(source)
    with pytest.raises(ValueError, match="scan"):
        module.build_named_permissions(tmp_path, ())


def test_special_file_refuses_before_native_policy(tmp_path):
    os.mkfifo(tmp_path / "public.pipe")
    with pytest.raises(ValueError, match="scan"):
        module.build_named_permissions(tmp_path, ())


@pytest.mark.parametrize(
    "bound,layout", [("MAX_SCAN_ENTRIES", "file"), ("MAX_SCAN_DEPTH", "directory"), ("MAX_PRIVATE_PATHS", "private")]
)
def test_scan_bounds_refuse_instead_of_partial_policy(tmp_path, monkeypatch, bound, layout):
    if layout == "directory":
        (tmp_path / "child").mkdir()
    else:
        (tmp_path / (".env.SECRET" if layout == "private" else "source.py")).write_text("PUBLIC_SOURCE")
    monkeypatch.setattr(module, bound, 0, raising=False)
    with pytest.raises(ValueError, match="scan"):
        module.build_named_permissions(tmp_path, ())


def test_child_directory_replacement_is_refused(tmp_path, monkeypatch):
    child = tmp_path / "child"
    child.mkdir()
    original_open = os.open
    replaced = False

    def open_directory(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "child" and kwargs.get("dir_fd") is not None and not replaced:
            child.rename(tmp_path / "old")
            child.mkdir()
            replaced = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", open_directory)
    with pytest.raises(ValueError, match="scan"):
        module.build_named_permissions(tmp_path, ())


def test_regular_file_metadata_change_during_scan_is_refused(tmp_path, monkeypatch):
    target = tmp_path / "source.py"
    target.write_text("PUBLIC_SOURCE")
    original_stat = os.stat
    changed = False

    def named_stat(path, *args, **kwargs):
        nonlocal changed
        if path == "source.py" and kwargs.get("dir_fd") is not None and not changed:
            target.write_text("PUBLIC_CHANGED_SOURCE")
            changed = True
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "stat", named_stat)
    with pytest.raises(ValueError, match="scan"):
        module.build_named_permissions(tmp_path, ())


def test_unreadable_directory_fails_closed(tmp_path, monkeypatch):
    def unavailable(*args):
        raise PermissionError("PUBLIC_ENUMERATION_DENIAL")

    monkeypatch.setattr(module.os, "scandir", unavailable)
    with pytest.raises(ValueError, match="scan"):
        module.build_named_permissions(tmp_path, ())


def test_unrepresentable_private_literal_refuses_instead_of_glob_expansion(tmp_path):
    (tmp_path / ".env.[unsafe]").write_text("PUBLIC_SOURCE")
    with pytest.raises(ValueError):
        module.build_named_permissions(tmp_path, ())


def test_new_private_object_changes_permissions_and_rejects_stale_metadata(tmp_path, monkeypatch):
    executable = tmp_path / "public-fixture"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    profile = register_fixture_profile(monkeypatch, executable)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    configuration = api_configuration(profile, workspace, ())
    before = digest(module.build_named_permissions(workspace, ()))
    configuration_projection(configuration, None, profile=profile, workspace=workspace, private_roots=())
    (workspace / ".env.EXAMPLE").write_text("PUBLIC_TEST_PRIVATE_CONTENT")
    assert digest(module.build_named_permissions(workspace, ())) != before
    with pytest.raises(ValueError, match="permissions"):
        configuration_projection(configuration, None, profile=profile, workspace=workspace, private_roots=())


def test_filesystem_version_two_invalidates_prior_native_proof(tmp_path, monkeypatch):
    assert module.FILESYSTEM_POLICY_VERSION == 2
    executable = tmp_path / "public-fixture"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    profile = register_fixture_profile(monkeypatch, executable)
    old = replace(profile, proof=replace(profile.proof, filesystem_policy_version=1))
    old = replace(old, proof=replace(old.proof, contract_digest=qualification.profile_contract_digest(old)))
    monkeypatch.setattr(qualification, "_profiles", lambda: (old,))
    with pytest.raises(ValueError, match="qualified"):
        qualification.require_profile(old)


def test_workspace_replacement_after_descriptor_open_is_refused(tmp_path, monkeypatch):
    original_open = os.open
    replaced = False

    def open_directory(path, flags, *args, **kwargs):
        nonlocal replaced
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == str(tmp_path) and kwargs.get("dir_fd") is None and not replaced:
            replaced = True
            tmp_path.rename(tmp_path.with_name(tmp_path.name + "-old"))
            tmp_path.mkdir()
            (tmp_path / ".env.EXAMPLE").write_text("PUBLIC_TEST_PRIVATE_CONTENT")
        return descriptor

    monkeypatch.setattr(module.os, "open", open_directory)
    with pytest.raises(ValueError, match="scan"):
        module.build_named_permissions(tmp_path, ())
