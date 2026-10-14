# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned source/Git framing never imports metadata into a consumer tree."""

import importlib
import subprocess

import pytest


def bundle_module():
    try:
        return importlib.import_module("firefly_forge.runner_pool_bundle")
    except ModuleNotFoundError:
        pytest.fail("Explicit project bundle framing is not implemented")


def fixture_workspace(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('source')\n")
    (source / "tool.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "tool.sh").chmod(0o755)
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    (source / ".env").write_text("PRIVATE_TEST_VALUE=excluded\n")
    return source


def test_framed_bundle_binds_source_and_git_modes_bytes_and_paths(tmp_path):
    module = bundle_module()
    source = fixture_workspace(tmp_path)
    data, identity = module.pack_project_bundle(source, source / ".git", 1_048_576)
    target = tmp_path / "received"
    workspace, metadata = module.unpack_project_bundle(data, target, 1_048_576, identity)
    assert (workspace / "main.py").read_text() == (source / "main.py").read_text()
    assert (workspace / "tool.sh").stat().st_mode & 0o111
    assert not (workspace / ".env").exists() and not (workspace / ".git").exists()
    assert (metadata / "HEAD").read_bytes() == (source / ".git" / "HEAD").read_bytes()
    assert identity.protocol == "forge-project-bundle-v1"
    assert str(source) not in identity.model_dump_json()
    with pytest.raises(ValueError, match="digest"):
        module.unpack_project_bundle(data + b"changed", tmp_path / "tampered", 1_048_576, identity)
    assert not (tmp_path / "tampered").exists()


def test_git_metadata_framing_rejects_links_and_configured_credentials(tmp_path):
    module = bundle_module()
    source = fixture_workspace(tmp_path)
    sentinel = tmp_path / "outside"
    sentinel.write_text("outside source")
    (source / ".git" / "unsafe-link").symlink_to(sentinel)
    with pytest.raises(ValueError, match="link|regular"):
        module.pack_project_bundle(source, source / ".git", 1_048_576)
    (source / ".git" / "unsafe-link").unlink()
    (source / ".git" / "credential-fixture").write_text("known-private-test-credential-value")
    with pytest.raises(ValueError, match="credentials"):
        module.pack_project_bundle(
            source, source / ".git", 1_048_576, registry={"PRIVATE_TEST_TOKEN": "known-private-test-credential-value"}
        )
    assert sentinel.read_text() == "outside source"


@pytest.mark.parametrize("unexpected", [".env", "../outside", "unknown.tar", "source.tar"])
def test_framing_rejects_unknown_private_escape_and_duplicate_entries_before_publication(tmp_path, unexpected):
    import hashlib
    import io
    import tarfile

    module = bundle_module()
    source = fixture_workspace(tmp_path)
    data, identity = module.pack_project_bundle(source, None, 1_048_576)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as target:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as incoming:
            for member in incoming:
                target.addfile(member, incoming.extractfile(member))
        item = tarfile.TarInfo(unexpected)
        item.size = 4
        target.addfile(item, io.BytesIO(b"evil"))
    forged = output.getvalue()
    changed = identity.model_copy(
        update={"archive_digest": hashlib.sha256(forged).hexdigest(), "archive_bytes": len(forged)}
    )
    with pytest.raises(ValueError, match="framing|path|duplicate"):
        module.unpack_project_bundle(forged, tmp_path / "received", 1_048_576, changed)
    assert not (tmp_path / "received").exists() and not (tmp_path / "outside").exists()
