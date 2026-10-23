# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Canonical restricted native filesystem policy matching the governed readers."""

from __future__ import annotations

import copy
import json
import os
import stat
from pathlib import Path
from typing import Any

from firefly_forge.workspace_tools import PRIVATE_BASENAMES, PRIVATE_KEY_SUFFIXES, PUBLIC_ENV_SUFFIXES, _private_name

PROFILE_NAME = "forge_source_edit"
FILESYSTEM_POLICY_VERSION = 2
MAX_SCAN_ENTRIES = 20000
MAX_SCAN_DEPTH = 64
MAX_PRIVATE_PATHS = 256
UNUSED_NETWORK_FIELDS = (
    "proxy_url",
    "enable_socks5",
    "socks_url",
    "enable_socks5_udp",
    "allow_upstream_proxy",
    "dangerously_allow_non_loopback_proxy",
    "dangerously_allow_all_unix_sockets",
    "mode",
    "domains",
    "unix_sockets",
    "allow_local_binding",
    "mitm",
)
EMPTY_NATIVE_HOOKS: dict[str, list[Any]] = {
    name: []
    for name in (
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "PreCompact",
        "PostCompact",
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "SubagentStart",
        "SubagentStop",
        "Stop",
        "Interrupt",
    )
}


def source_private_roots(configuration_directory: Path | None = None) -> tuple[Path, ...]:
    """Resolve trusted service/auth locations; callers cannot supply them through tool arguments."""
    home = Path.home()
    roots = [home / ".codex", home / ".ssh", home / ".aws", home / "Library/Keychains", Path("/Library/Keychains")]
    if value := os.environ.get("CODEX_HOME"):
        roots.append(Path(value))
    if configuration_directory is not None:
        roots.append(configuration_directory)
    return tuple(sorted({Path(_literal_path(root)) for root in roots}, key=str))


def native_api_permissions(permissions: dict[str, Any]) -> dict[str, Any]:
    """Match pinned ApiConfig serialization, retaining every unused typed option as null."""
    expected = copy.deepcopy(permissions)
    for profile in expected.values():
        profile.update(description=None, extends=None)
        profile["filesystem"]["glob_scan_max_depth"] = None
        profile["network"].update(dict.fromkeys(UNUSED_NETWORK_FIELDS))
    return expected


def typed_equal(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            typed_equal(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            typed_equal(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return bool(actual == expected)


def _private_case_pattern(value: str) -> str:
    return "".join(
        "[" + character + character.upper() + {"k": "K", "s": "ſ"}.get(character, "") + "]"
        if character.isascii() and character.islower()
        else character
        for character in value
    )


def env_private_patterns() -> tuple[str, ...]:
    """Complement the public suffixes without blocking .env.example files.

    A reversed suffix trie emits the first mismatching character class. Exact
    short names cover mismatches inside the fixed .env prefix itself.
    """
    trie: dict[str, Any] = {}
    for suffix in PUBLIC_ENV_SUFFIXES:
        node = trie
        for character in reversed(suffix):
            node = node.setdefault(character, {})
        node[""] = True
    patterns: set[str] = set()
    prefix = ".env"

    def visit(node: dict[str, Any], reverse_suffix: str) -> None:
        if "" in node:
            return
        suffix = reverse_suffix[::-1]
        children = "".join(sorted(node))
        patterns.add(prefix + "*[!" + children + "]" + suffix)
        for position, character in enumerate(prefix):
            short = prefix[: position + 1] + suffix
            if short.startswith(prefix) and character not in children:
                patterns.add(short)
        if suffix.startswith(prefix):
            patterns.add(suffix)
        for character in sorted(node):
            visit(node[character], reverse_suffix + character)

    visit(trie, "")
    return tuple(sorted(_private_case_pattern(prefix) + pattern[len(prefix) :] for pattern in patterns))


def _literal_path(path: Path) -> str:
    resolved = path.resolve()
    value = str(resolved)
    if not path.is_absolute() or any(character in value for character in "*?[]{}\\\x00\n\r"):
        raise ValueError("Native source policy roots must be absolute literal paths")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Native source policy roots must be valid UTF-8") from None
    return value


def _scan_private_paths(root: str) -> tuple[str, ...]:
    """Bind existing private object spellings without opening files or following links."""
    private: set[str] = set()
    count = 0

    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def visit(directory: int, parts: tuple[str, ...]) -> None:
        nonlocal count
        if len(parts) > MAX_SCAN_DEPTH:
            raise ValueError("Native source workspace scan exceeds its depth bound")
        before = os.fstat(directory)
        records = []
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_SCAN_ENTRIES:
                    raise ValueError("Native source workspace scan exceeds its entry bound")
                records.append((entry.name, entry.stat(follow_symlinks=False)))
        for name, info in sorted(records):
            relative = (*parts, name)
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)) or (
                stat.S_ISREG(info.st_mode) and info.st_nlink != 1
            ):
                raise ValueError("Native source workspace scan refuses hardlinks and special files")
            if _private_name(name):
                literal = str(Path(root).joinpath(*relative))
                if any(character in literal for character in "*?[]{}\\\x00\n\r"):
                    raise ValueError("Native source workspace scan cannot represent a private literal")
                literal.encode("utf-8")
                private.add(literal)
                if len(private) > MAX_PRIVATE_PATHS:
                    raise ValueError("Native source workspace scan exceeds its private-path bound")
            elif stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    if identity(os.fstat(child)) != identity(info):
                        raise ValueError("Native source workspace scan observed a replaced directory")
                    visit(child, relative)
                finally:
                    os.close(child)
            if identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != identity(info):
                raise ValueError("Native source workspace scan observed a changed entry")
        if identity(os.fstat(directory)) != identity(before):
            raise ValueError("Native source workspace scan observed a changed directory")

    try:
        named_root = os.stat(root, follow_symlinks=False)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if identity(os.fstat(descriptor)) != identity(named_root):
                raise ValueError("Native source workspace scan observed a replaced root")
            visit(descriptor, ())
            if identity(os.stat(root, follow_symlinks=False)) != identity(named_root):
                raise ValueError("Native source workspace scan observed a changed root")
        finally:
            os.close(descriptor)
    except (OSError, UnicodeError):
        raise ValueError("Native source workspace scan could not verify current names") from None
    return tuple(sorted(private))


def build_named_permissions(workspace: Path, private_roots: tuple[Path, ...]) -> dict[str, Any]:
    if not isinstance(workspace, Path) or workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("Native source execution requires a real workspace directory")
    root = _literal_path(workspace)
    if type(private_roots) is not tuple or len(private_roots) > 64:
        raise ValueError("Native source private roots must be a bounded trusted tuple")
    private: set[str] = set()
    for path in private_roots:
        if not isinstance(path, Path):
            raise ValueError("Native source private roots must be literal paths")
        value = _literal_path(path)
        if Path(root).is_relative_to(value):
            raise ValueError("A native private root cannot contain the execution workspace")
        private.add(value)
    private.update(_scan_private_paths(root))
    basenames = (
        *(_private_case_pattern(name) for name in sorted(PRIVATE_BASENAMES)),
        *("*" + _private_case_pattern(suffix) for suffix in PRIVATE_KEY_SUFFIXES),
        *env_private_patterns(),
    )
    scoped = {".": "write"}
    for name in basenames:
        for pattern in (name, name + "/**", "**/" + name, "**/" + name + "/**"):
            scoped[pattern] = "deny"
    return {
        PROFILE_NAME: {
            "workspace_roots": {root: True},
            "filesystem": {":minimal": "read", ":workspace_roots": scoped, **dict.fromkeys(sorted(private), "deny")},
            "network": {"enabled": False},
        }
    }


def _toml(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key) + "=" + _toml(item) for key, item in value.items()) + "}"
    if type(value) is bool:
        return "true" if value else "false"
    if not isinstance(value, str):
        raise ValueError("Native source policy contains an unsupported value")
    return json.dumps(value)


def native_policy_overrides(workspace: Path, private_roots: tuple[Path, ...]) -> tuple[str, str]:
    permissions = "permissions=" + _toml(build_named_permissions(workspace, private_roots))
    if len(permissions.encode()) > 65536:
        raise ValueError("Native source policy exceeds its supported size")
    return ('default_permissions="' + PROFILE_NAME + '"', permissions)
