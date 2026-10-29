# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Declared local build checks use an input-scoped, network-denied transport."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any, Literal

DENY_NETWORK_PROFILE = "(version 1) (allow default) (deny network*)"
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
PLATFORM_READ_ROOTS = (
    "/System/Library",
    "/System/Cryptexes",
    "/System/Volumes/Preboot/Cryptexes",
    "/usr/lib",
    "/usr/share",
    "/usr/bin",
    "/bin",
    "/dev",
)


def build_profile(workspace: Path, dependencies: list[Path], scratch: Path) -> str:
    roots = sorted({str(workspace), str(scratch), *PLATFORM_READ_ROOTS})
    root_values = " ".join(json.dumps(p) for p in roots)
    dependency_values = " ".join(json.dumps(str(p)) for p in dependencies)
    writes = " ".join("(subpath " + json.dumps(str(p)) + ")" for p in (workspace, scratch))
    private_names = (
        ".git",
        ".codex",
        ".claude",
        ".forge",
        ".venv",
        "vendor",
        "node_modules",
        ".npmrc",
        ".pypirc",
        ".ssh",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "__pycache__",
        ".cache",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "build",
        "dist",
    )

    def folded(value: str) -> str:
        return "".join(
            "[" + c.lower() + c.upper() + "]" if c.isalpha() else "[.]" if c == "." else re.escape(c) for c in value
        )

    names = "|".join(folded(name) for name in private_names)
    workspace_prefix = "^" + re.escape(str(workspace)) + r"(/[^/]+)*"
    examples = "|".join(folded(name) for name in ("example", "sample", "template"))
    private = workspace_prefix + "/(" + names + r"|[^/]*[.](" + folded("pem") + "|" + folded("key") + r"))(/|$)"
    excluded = (
        " (deny file-read* (require-all (subpath "
        + json.dumps(str(workspace))
        + ") "
        + "(require-not (apply subpath dependency-roots)) (regex "
        + json.dumps(private)
        + ")))"
    )
    sensitive_names = tuple(
        name
        for name in private_names
        if name
        not in {
            "vendor",
            "node_modules",
            "__pycache__",
            ".cache",
            ".mypy_cache",
            ".ruff_cache",
            ".pytest_cache",
            "build",
            "dist",
        }
    )
    sensitive = (
        workspace_prefix
        + "/("
        + "|".join(folded(name) for name in sensitive_names)
        + r"|[^/]*[.]("
        + folded("pem")
        + "|"
        + folded("key")
        + r"))(/|$)"
    )
    sensitive_writes = (
        " (deny file-write* (require-all (subpath "
        + json.dumps(str(workspace))
        + ") (regex "
        + json.dumps(sensitive)
        + ")))"
    )
    bytecode_reads = ' (deny file-read* (regex #"/__pycache__(/|$)"))'
    config_names = "|".join(folded(name) for name in sensitive_names if name != ".venv")
    config_parts = ["^("]
    for index, path in enumerate(sorted({workspace, *dependencies})):
        config_parts.append(("|" if index else "") + re.escape(str(path)))
    config_parts.append(")")
    root_pattern = " (define build-root-pattern (string-append " + " ".join(map(json.dumps, config_parts)) + "))"
    config_suffix = r"(/[^/]+)*/(" + config_names + r")(/|$)"
    runtime_config_reads = (
        " (deny file-read* (regex (string-append build-root-pattern " + json.dumps(config_suffix) + ")))"
    )
    env_name = r"(/[^/]+)*/[.]" + folded("env") + r"([.][^/]*)?"
    public_example = r"/[.]" + folded("env") + "[.](" + examples + ")$"
    environment_files = (
        " (deny file-read* file-write* (require-any"
        + " (regex (string-append build-root-pattern "
        + json.dumps(env_name + "/")
        + "))"
        + " (require-all (regex (string-append build-root-pattern "
        + json.dumps(env_name + "$")
        + "))"
        + " (require-not (regex "
        + json.dumps(public_example)
        + ")))))"
    )
    return (
        DENY_NETWORK_PROFILE
        + " (define dependency-roots (list "
        + dependency_values
        + "))"
        + root_pattern
        + " (define input-roots (append (list "
        + root_values
        + ") dependency-roots))"
        + ' (deny file-read* (require-not (require-any (literal "/") '
        + "(apply subpath input-roots) (apply path-ancestors input-roots))))"
        + ' (deny file-read-data file-read-xattr (require-not (require-any (literal "/") '
        + "(apply subpath input-roots))))"
        + " (deny file-write* (require-not (require-any "
        + writes
        + ' (literal "/dev/null") (subpath "/dev/fd"))))'
        + excluded
        + sensitive_writes
        + bytecode_reads
        + runtime_config_reads
        + environment_files
    )


def wrap_build_argv(argv: list[str], profile: str = DENY_NETWORK_PROFILE) -> list[str]:
    from firefly_forge.sandbox_models import bounded_argv

    return bounded_argv([str(SANDBOX_EXEC), "-p", profile, *argv])


def build_check_kind(argv: list[str], workspace: Path) -> Literal["python_static", "node_build"] | None:
    tail = argv[1:]
    if tail[:1] == ["-B"]:
        tail = tail[1:]
    if tail[:1] == ["-m"] and len(tail) >= 2 and tail[1] in {"ruff", "mypy"}:
        return "python_static"
    if len(argv) != 5 or Path(argv[0]).name not in {"npm", "npm.cmd"} or argv[1] != "--prefix" or argv[3] != "run":
        return None
    return _node_kind(argv, workspace)


def _node_kind(argv: list[str], workspace: Path) -> Literal["python_static", "node_build"] | None:
    if argv[4] not in {"test", "lint", "typecheck", "build"}:
        return None
    directory = (workspace / argv[2]).resolve()
    if not directory.is_relative_to(workspace.resolve()):
        return None
    try:
        scripts = json.loads((directory / "package.json").read_text())["scripts"]
        name = argv[4]
        if "pre" + name in scripts or "post" + name in scripts:
            return None
        command = shlex.split(scripts[name])
        expected = {
            "test": "vitest/vitest.mjs",
            "lint": "eslint/bin/eslint.js",
            "typecheck": "typescript/bin/tsc",
            "build": "vite/bin/vite.js",
        }[name]
        if len(command) < 2 or command[:2] != ["node", "node_modules/" + expected]:
            return None
        if any(token in {";", "&&", "||", "|", "&"} or "$" in token or "`" in token for token in command):
            return None
        return "node_build"
    except (OSError, ValueError, KeyError, TypeError):
        return None


def eligible_build_checks(run: dict[str, Any], workspace: Path) -> list[dict[str, Any]]:
    import sys

    from firefly_forge.journal import digest
    from firefly_forge.sandbox_models import validate_sandbox_snapshot

    config = validate_sandbox_snapshot(run["sandbox_snapshot"])
    if sys.platform != "darwin" or config.mode != "host" or config.pool_binding is not None:
        return []
    return [
        dict(
            command_index=index + 1,
            command_digest=digest(command),
            argv=command["argv"],
            purpose="source_build",
            requires_declaration=True,
        )
        for index, command in enumerate(run.get("commands", []))
        if build_check_kind(command["argv"], workspace) is not None
    ]
