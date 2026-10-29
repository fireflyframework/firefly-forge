# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Observed project declarations and dependency hashes for exact learning reuse."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from firefly_forge.journal import digest
from firefly_forge.learning_models import LearningContext, SourceAssociation
from firefly_forge.process import checked, execute
from firefly_forge.workspace_tools import WorkspaceTools

MANIFESTS = (
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "composer.json",
    "composer.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
)


def query_digest(query: str) -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > 4000:
        raise ValueError("Learning query requires nonempty text within 4000 characters")
    return hashlib.sha256(" ".join(query.split()).encode()).hexdigest()


async def _retained_manifests(workspace: Path, component: str, tree: str) -> dict[str, str]:
    paths = {(Path(component) / name).as_posix(): name for name in MANIFESTS}
    entries = await checked(["git", "ls-tree", "-z", tree, "--", *paths], workspace)
    contents: dict[str, str] = {}
    for entry in filter(None, entries.split("\0")):
        metadata, path = entry.split("\t", 1)
        mode, kind, blob = metadata.split()
        if path not in paths or kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("Verified learning declarations must be regular retained source files")
        size = int(await checked(["git", "cat-file", "-s", blob], workspace))
        if size > 1_000_000:
            raise ValueError("Verified learning declaration exceeds its 1 MB bound")
        result = await execute(["git", "cat-file", "blob", blob], workspace)
        content = result.output
        if result.exit_code or "\ufffd" in content or len(content.encode()) != size:
            raise ValueError("Verified learning declaration requires intact UTF-8 source content")
        contents[paths[path]] = content
    return contents


async def observe_context(
    run: dict[str, Any], source: dict[str, Any], workspace: Path, query: str, *, immutable_source: bool = False
) -> LearningContext:
    association = SourceAssociation.model_validate(
        {key: source[key] for key in ("base_revision", "head", "source_tree")}
    )
    component = (run.get("component_snapshot") or {}).get("path", ".")
    scope = workspace / component
    tools = WorkspaceTools(scope, repository_root=workspace, max_file_bytes=1_000_000)
    contents = {}
    if immutable_source:
        contents = await _retained_manifests(workspace, component, association.source_tree)
    else:
        available = set(await tools.list_files())
        for name in MANIFESTS:
            if name not in available:
                continue
            try:
                contents[name] = await tools.read_file(name)
            except FileNotFoundError:
                continue
    hashes = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in contents.items()}
    backend = run["backend"]
    framework = backend if backend in {"pyfly", "larafly"} else "none"
    version = "none" if framework == "none" else "unknown"
    try:
        if framework == "pyfly":
            versions = []
            for name in ("uv.lock", "poetry.lock"):
                packages = tomllib.loads(contents.get(name, "")).get("package", [])
                versions += [
                    item["version"] for item in packages if item.get("name") == "pyfly" and item.get("version")
                ]
            project = tomllib.loads(contents.get("pyproject.toml", "")).get("project", {})
            for requirement in project.get("dependencies", []):
                pin = re.fullmatch(r"pyfly(?:\[[^\]]{1,100}\])?==([0-9]+(?:\.[0-9]+){1,3})", requirement)
                wheel = re.fullmatch(
                    r"pyfly(?:\[[^\]]{1,100}\])? @ https://[^\s?#]+/pyfly-([0-9]+(?:\.[0-9]+){1,3})-py3-none-any\.whl",
                    requirement,
                )
                selected = pin or wheel
                if selected is not None:
                    versions.append(selected.group(1))
            if versions and len(set(versions)) == 1:
                version = str(versions[0])
        elif framework == "larafly":
            lock = json.loads(contents.get("composer.lock", "{}"))
            versions = [
                item["version"]
                for item in lock.get("packages", [])
                if item.get("name") == "fireflyframework/larafly" and item.get("version")
            ]
            if versions and len(set(versions)) == 1 and re.fullmatch(r"v?[0-9]+(?:\.[0-9]+){1,3}", versions[0]):
                version = versions[0].removeprefix("v")
    except (ValueError, TypeError, KeyError):
        version = "unknown"
    if run.get("framework_knowledge_pin"):
        pin = run["framework_knowledge_pin"]
        if pin["framework"] != framework:
            raise ValueError("Learning context differs from the retained framework target")
        version = pin["framework_version"]
    return LearningContext.model_validate(
        {
            "repository_id": run["repository_id"],
            "project_id": run.get("project_id"),
            "component_id": run.get("component_id"),
            "framework": framework,
            "version": version,
            "dependency_digest": digest(
                {
                    "manifests": hashes,
                    "backend": backend,
                    "frontend": run["frontend"],
                    "sandbox": run.get("sandbox_snapshot"),
                    **(
                        {"framework_target_digest": run["framework_knowledge_pin"]["digest"]}
                        if run.get("framework_knowledge_pin")
                        else {}
                    ),
                }
            ),
            "source_tree": association.source_tree,
            "query_digest": query_digest(query),
        }
    )
