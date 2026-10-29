# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Empty npm builtins are measured without granting access to private npm config."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

from firefly_forge.source_catalog import SourceCatalogBudget
from firefly_forge.verification_dependencies import prepare_dependency_capture
from firefly_forge.verification_scope import build_profile
from firefly_forge.verification_scope_attestation import _runtime_boundary_guard


@pytest.fixture
def npm_case(tmp_path):
    prefix = tmp_path / "runtime"
    package = prefix / "lib" / "node_modules" / "npm"
    (package / "bin").mkdir(parents=True)
    (prefix / "bin").mkdir()
    node = prefix / "bin" / "node"
    node.write_text("fictional runtime; never executed\n")
    node.chmod(0o700)
    cli = package / "bin" / "npm-cli.js"
    cli.write_text("fictional CLI; never executed\n")
    manifest = package / "package.json"
    manifest.write_text(json.dumps({"name": "npm", "bin": {"npm": "bin/npm-cli.js"}}))
    executable = prefix / "bin" / "npm"
    executable.symlink_to(cli.relative_to(prefix / "bin", walk_up=True))
    builtin = package / ".npmrc"
    builtin.write_bytes(b"")
    home, workspace, state = (tmp_path / name for name in ("home", "workspace", "state"))
    for path in (home, workspace, state):
        path.mkdir()
    return SimpleNamespace(
        prefix=prefix,
        package=package,
        cli=cli,
        node=node,
        manifest=manifest,
        executable=executable,
        builtin=builtin,
        workspace=workspace,
        home=home,
        state=state,
        roots=[prefix, executable, cli],
    )


def boundary(c):
    return _runtime_boundary_guard(c.workspace, {"HOME": str(c.home)}, c.state, c.roots)


async def capture(c, tmp_path):
    async def current():
        pass

    return await prepare_dependency_capture(
        c.roots,
        tmp_path / "capture",
        SourceCatalogBudget(min_free_bytes=0),
        current,
        validate_path=boundary(c),
    )


async def test_exact_admitted_empty_npm_builtin_has_a_stable_capture_and_metadata_fence(npm_case, tmp_path):
    prepared = await capture(npm_case, tmp_path)
    assert len(prepared.digest) == 64
    await prepared.assert_current()
    npm_case.builtin.write_text("private configuration\n")
    with pytest.raises(ValueError):
        await prepared.assert_current()


@pytest.mark.parametrize("location", ["owner", "workspace", "unadmitted", "dependency"])
def test_empty_npm_configs_outside_the_admitted_builtin_remain_private(npm_case, tmp_path, location):
    c = npm_case
    folder = {
        "owner": c.home,
        "workspace": c.workspace,
        "unadmitted": tmp_path / "other",
        "dependency": c.package / "node_modules" / "other",
    }[location]
    folder.mkdir(parents=True, exist_ok=True)
    config = folder / ".npmrc"
    config.write_bytes(b"")
    with pytest.raises(ValueError, match="private"):
        boundary(c)(config, False)


@pytest.mark.parametrize("change", ["nonempty", "symlink", "hardlink", "case_alias", "package", "entrypoint", "node"])
def test_empty_builtin_exception_refuses_changed_or_aliased_identity(npm_case, change):
    c = npm_case
    guard = boundary(c)
    if change == "nonempty":
        c.builtin.write_bytes(b"secret")
    elif change == "symlink":
        c.builtin.unlink()
        c.builtin.symlink_to(c.home / ".npmrc")
        (c.home / ".npmrc").write_bytes(b"")
    elif change == "hardlink":
        os.link(c.builtin, c.home / ".npmrc")
    elif change == "case_alias":
        alias = c.builtin.with_name(".NPMRC")
        if not alias.exists():
            pytest.skip("Native case identity requires a case-insensitive volume")
        with pytest.raises(ValueError):
            guard(alias, False)
        return
    else:
        target = {"package": c.manifest, "entrypoint": c.cli, "node": c.node}[change]
        target.write_text("changed runtime identity\n")
    with pytest.raises(ValueError):
        guard(c.builtin, False)


@pytest.mark.parametrize("change", ["package_name", "package_bin", "missing_admission", "foreign_cli"])
def test_empty_file_does_not_self_declare_an_npm_runtime(npm_case, change):
    c = npm_case
    if change.startswith("package_"):
        c.manifest.write_text(
            json.dumps(
                {
                    "name": "other" if change == "package_name" else "npm",
                    "bin": {"npm": "other.js" if change == "package_bin" else "bin/npm-cli.js"},
                }
            )
        )
    elif change == "missing_admission":
        c.roots = [c.prefix]
    else:
        c.executable.unlink()
        c.executable.symlink_to(c.node)
    with pytest.raises(ValueError):
        boundary(c)(c.builtin, False)


@pytest.mark.skipif(sys.platform != "darwin", reason="Native scoped file access requires Darwin")
async def test_measured_empty_npm_builtin_remains_unreadable_to_the_build_process(npm_case, tmp_path):
    c = npm_case
    await capture(c, tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/sandbox-exec",
        "-p",
        build_profile(c.workspace, c.roots, scratch),
        "/bin/cat",
        str(c.builtin),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode != 0
    assert stdout == b""
    assert b"Operation not permitted" in stderr
