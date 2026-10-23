# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Private-name parity and canonical native policy boundaries."""

import fnmatch
import random
import string
import tomllib

import pytest

from firefly_forge.workspace_tools import _private_name


def test_env_deny_patterns_preserve_all_public_suffix_exceptions():
    from firefly_forge.source_edit_filesystem_policy import env_private_patterns

    patterns = env_private_patterns()
    randomizer = random.Random(712)
    names = {".env", ".enve", ".envle", ".envple", ".envexample", ".env.sample", ".env.template"}
    for _ in range(5000):
        tail = "".join(randomizer.choices(string.ascii_letters + string.digits + "._-", k=randomizer.randrange(30)))
        names.add(".env" + tail)
        for suffix in (".example", ".sample", ".template"):
            names.add(".env" + tail + suffix)
            for length in range(len(suffix)):
                names.add(".env" + tail + suffix[:length])
    for name in names:
        for prefix in (".env", ".ENV", ".eNv"):
            candidate = prefix + name[4:]
            actual = any(fnmatch.fnmatchcase(candidate, pattern) for pattern in patterns)
            assert actual is _private_name(candidate), candidate


def test_native_policy_binds_one_workspace_private_roots_and_no_network(tmp_path):
    from firefly_forge.source_edit_filesystem_policy import build_named_permissions, native_policy_overrides

    workspace = tmp_path / 'quoted " workspace'
    workspace.mkdir()
    private = tmp_path / "private"
    result = build_named_permissions(workspace, (private,))
    assert set(result) == {"forge_source_edit"}
    profile = result["forge_source_edit"]
    assert set(profile) == {"workspace_roots", "filesystem", "network"}
    assert profile["workspace_roots"] == {str(workspace.resolve()): True}
    assert profile["network"] == {"enabled": False}
    assert profile["filesystem"][":minimal"] == "read"
    assert profile["filesystem"][str(private.resolve())] == "deny"
    assert profile["filesystem"][":workspace_roots"]["."] == "write"
    assert all(key not in profile["filesystem"] for key in (":root", ":tmpdir", ":slash_tmp"))
    overrides = native_policy_overrides(workspace, (private,))
    parsed = tomllib.loads("\n".join(overrides))
    assert parsed == {"default_permissions": "forge_source_edit", "permissions": result}


@pytest.mark.parametrize("name", [".git", ".ssh", ".claude", ".npmrc", "credentials.json", "id_ed25519"])
def test_private_files_and_directories_are_denied_at_every_depth(tmp_path, name):
    from firefly_forge.source_edit_filesystem_policy import build_named_permissions

    scoped = build_named_permissions(tmp_path, ())["forge_source_edit"]["filesystem"][":workspace_roots"]
    for candidate in (name, name.upper(), "nested/" + name.upper(), name.upper() + "/child/source.py"):
        assert any(value == "deny" and fnmatch.fnmatchcase(candidate, key) for key, value in scoped.items())


@pytest.mark.parametrize("name", ["KEY.PEM", "KEY.KEY", "nested/CREDENTIALS.JSON", ".ENV.EXAMPLE"])
def test_private_case_variants_are_in_native_deny_policy(tmp_path, name):
    from firefly_forge.source_edit_filesystem_policy import build_named_permissions

    scoped = build_named_permissions(tmp_path, ())["forge_source_edit"]["filesystem"][":workspace_roots"]
    assert any(value == "deny" and fnmatch.fnmatchcase(name, pattern) for pattern, value in scoped.items())


async def test_policy_rejects_ambiguous_roots_and_does_not_create_paths(tmp_path):
    from firefly_forge.source_edit_filesystem_policy import build_named_permissions

    with pytest.raises(ValueError):
        build_named_permissions(tmp_path, (tmp_path.parent,))
    with pytest.raises(ValueError):
        build_named_permissions(tmp_path / "missing", ())
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        build_named_permissions(alias, ())
    special = tmp_path / "glob[unsafe]"
    special.mkdir()
    with pytest.raises(ValueError):
        build_named_permissions(special, ())
