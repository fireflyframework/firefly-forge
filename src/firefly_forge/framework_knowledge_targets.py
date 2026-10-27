# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact framework targets selected from immutable Git dependency manifests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import Version

from firefly_forge.framework_knowledge_models import FrameworkName, framework_version
from firefly_forge.git_workspace import git_environment, trusted_git

GENERATION_RELEASE_CANDIDATES = {"pyfly": "26.10.11", "larafly": "26.10.4"}
MANIFEST_PATHS = {
    "pyfly": ("pyproject.toml", "uv.lock", "requirements.txt"),
    "larafly": ("composer.json", "composer.lock"),
}


def manifest_target(framework: FrameworkName, files: dict[str, bytes], *, allow_missing: bool = False) -> str:
    declared_framework = False
    locked: set[str] = set()
    exact: set[str] = set()
    try:
        if framework == "pyfly":
            if "uv.lock" in files:
                lock = tomllib.loads(files["uv.lock"].decode())
                locked = {framework_version(p["version"]) for p in lock.get("package", []) if p.get("name") == "pyfly"}
            dependencies = []
            if "pyproject.toml" in files:
                project = tomllib.loads(files["pyproject.toml"].decode())
                dependencies.extend(project.get("project", {}).get("dependencies", []))
            if "requirements.txt" in files:
                dependencies.extend(files["requirements.txt"].decode().splitlines())
            for requirement in dependencies:
                if not isinstance(requirement, str):
                    raise ValueError("Dependency declarations must be strings")
                if not re.match(r"^pyfly(?:\[|\s|[=<>!~@]|$)", requirement, re.I):
                    continue
                declared_framework = True
                try:
                    declared = Requirement(requirement)
                except InvalidRequirement as exc:
                    raise ValueError("Framework dependency declaration is invalid") from exc
                if locked and not declared.url and any(Version(v) not in declared.specifier for v in locked):
                    raise ValueError(
                        "Frozen framework lock version conflicts with its declared dependency constraint; "
                        "it does not satisfy it"
                    )
                match = re.fullmatch(r"pyfly(?:\[[^\]]+\])?\s*==\s*(v?\d+\.\d+\.\d+)\s*", requirement, re.I)
                if match:
                    exact.add(framework_version(match[1]))
                    continue
                match = re.fullmatch(
                    r"pyfly(?:\[[^\]]+\])?\s*@\s*https://github\.com/fireflyframework/fireflyframework-pyfly/releases/download/(v?\d+\.\d+\.\d+)/pyfly-(\d+\.\d+\.\d+)-py3-none-any\.whl",
                    requirement,
                    re.I,
                )
                if match:
                    if framework_version(match[1]) != framework_version(match[2]):
                        raise ValueError("Official dependency URL has conflicting release versions")
                    exact.add(framework_version(match[1]))
        else:
            if "composer.lock" in files:
                lock = json.loads(files["composer.lock"])
                locked = {
                    framework_version(p["version"])
                    for p in lock.get("packages", []) + lock.get("packages-dev", [])
                    if p.get("name") == "fireflyframework/larafly"
                }
            if "composer.json" in files:
                project = json.loads(files["composer.json"])
                for group in ("require", "require-dev"):
                    value = project.get(group, {}).get("fireflyframework/larafly")
                    declared_framework = declared_framework or value is not None
                    if isinstance(value, str) and locked:
                        from firefly_forge.framework_knowledge_requirements import composer_satisfies

                        if any(not composer_satisfies(v, value) for v in locked):
                            raise ValueError("Frozen framework lock conflicts with its declared Composer dependency")
                    if isinstance(value, str) and re.fullmatch(r"v?\d+\.\d+\.\d+", value):
                        exact.add(framework_version(value))
    except (UnicodeError, KeyError, TypeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Retained framework manifests are invalid or ambiguous") from exc
    versions = locked | exact
    if not versions and not declared_framework and allow_missing:
        return ""
    if len(versions) != 1:
        raise ValueError(
            "Select one exact framework dependency target; ambiguous or conflicting manifests cannot use latest"
        )
    return next(iter(versions))


async def _git(workspace: Path, argv: list[str], budget: int) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *trusted_git(["git", *argv]),
        cwd=workspace,
        env=git_environment({"PATH": os.defpath, "LANG": "C.UTF-8"}),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    chunks = bytearray()
    try:
        async with asyncio.timeout(60):
            assert process.stdout is not None
            while block := await process.stdout.read(65536):
                if len(chunks) + len(block) > budget:
                    raise ValueError("Frozen manifest reading exhausted its configured byte budget")
                chunks.extend(block)
            await process.wait()
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise
    return process.returncode or 0, bytes(chunks)


async def git_manifests(workspace: Path, commit: str, framework: FrameworkName, component: str) -> dict[str, bytes]:
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Framework target inspection requires an immutable Git commit")
    prefix = PurePosixPath(component)
    if prefix.is_absolute() or ".." in prefix.parts or "\\" in component:
        raise ValueError("Framework target component must remain inside the frozen source")
    budget = int(os.environ.get("FORGE_FRAMEWORK_MANIFEST_BYTE_BUDGET", str(32 * 1024**2)))
    if budget <= 0:
        raise ValueError("Configure a positive frozen manifest work budget")
    values = {}
    for name in MANIFEST_PATHS[framework]:
        path = (prefix / name).as_posix()
        code, description = await _git(workspace, ["ls-tree", "-z", commit, "--", path], budget)
        if code or not description:
            continue
        descriptor = description.split(b"\t", 1)[0].split()
        if len(descriptor) != 3 or descriptor[0] not in {b"100644", b"100755"} or descriptor[1] != b"blob":
            raise ValueError("Frozen dependency manifests must be regular Git blobs")
        code, body = await _git(workspace, ["cat-file", "blob", commit + ":" + path], budget)
        expected = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest().encode()
        if code or expected != descriptor[2]:
            raise ValueError("Frozen dependency manifest content does not match its Git object")
        budget -= len(body)
        values[name] = body
    return values


def select_target(
    framework: FrameworkName,
    operation: str,
    files: dict[str, bytes],
    explicit: dict[str, Any] | None,
    *,
    supported_default: str | None = None,
) -> tuple[str, str | None]:
    supported_default = supported_default or GENERATION_RELEASE_CANDIDATES[framework]
    if operation == "build":
        if explicit and explicit.get("upgrade_from"):
            raise ValueError("Framework upgrades require an existing-project maintenance operation")
        target = framework_version(explicit["framework_version"]) if explicit else supported_default
        original = manifest_target(framework, files, allow_missing=True)
        if original and original != target:
            raise ValueError("Use explicit maintenance upgrade consent to change an existing framework dependency")
        if target != supported_default:
            raise ValueError("New projects require the validated supported generation release")
        return target, None
    original = manifest_target(framework, files)
    if explicit:
        target = framework_version(explicit["framework_version"])
        previous = explicit.get("upgrade_from")
        if previous:
            if framework_version(previous) != original or target == original:
                raise ValueError("Explicit framework upgrade must bind the exact frozen original dependency")
            return target, original
        if target != original:
            raise ValueError("Changing an existing framework target requires explicit upgrade_from consent")
    return original, None


CURRENT_FRAMEWORK_TARGET: ContextVar[dict[str, Any] | None] = ContextVar("forge_framework_target", default=None)


@contextmanager
def framework_target_scope(pin: dict[str, Any] | None) -> Iterator[None]:
    token = CURRENT_FRAMEWORK_TARGET.set(pin)
    try:
        yield
    finally:
        CURRENT_FRAMEWORK_TARGET.reset(token)
