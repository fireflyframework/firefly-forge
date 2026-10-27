# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Upstream runtime requirements from hash-verified immutable package manifests."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from firefly_forge.framework_knowledge_models import OFFICIAL_REPOSITORIES, FrameworkName, framework_version
from firefly_forge.product_provenance import content_digest as digest


def manifest_requirements(
    framework: FrameworkName, version: str, commit: str, source: dict[str, Any], body: bytes
) -> dict[str, Any]:
    path = "pyproject.toml" if framework == "pyfly" else "composer.json"
    if (
        source["path"] != path
        or source["bytes"] != len(body)
        or hashlib.sha256(body).hexdigest() != source["sha256"]
        or hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest() != source["blob_oid"]
        or source["source_uri"]
        != "https://github.com/" + OFFICIAL_REPOSITORIES[framework] + "/blob/" + commit + "/" + path
    ):
        raise ValueError("Official runtime manifest provenance or digest does not match its immutable release")
    try:
        if framework == "pyfly":
            project = tomllib.loads(body.decode())["project"]
            if project["name"] != "pyfly" or framework_version(project["version"]) != version:
                raise ValueError("Official runtime manifest names a different framework release")
            requirement = project["requires-python"]
            dependencies = {}
            SpecifierSet(requirement)
            runtime = "python"
        else:
            project = json.loads(body)
            if project["name"] != "fireflyframework/larafly":
                raise ValueError("Official runtime manifest names a different framework")
            dependencies = project["require"]
            if not isinstance(dependencies, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in dependencies.items()
            ):
                raise ValueError("Official runtime manifest dependencies are invalid")
            requirement = dependencies["php"]
            runtime = "php"
            composer_satisfies("0.0.0", requirement)  # Validate the declared constraint grammar, not a runtime claim.
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError, InvalidSpecifier) as exc:
        raise ValueError("Official runtime manifest requirements are incomplete or invalid") from exc
    value = {
        "version": 1,
        "framework": framework,
        "framework_version": version,
        "source_commit": commit,
        "path": path,
        "source_uri": source["source_uri"],
        "source_sha256": source["sha256"],
        "blob_oid": source["blob_oid"],
        "bytes": len(body),
        "runtime": runtime,
        "runtime_requirement": requirement,
        "dependencies": dependencies,
    }
    value["digest"] = digest(value)
    return value


def composer_satisfies(version: str, constraint: str) -> bool:
    def normalized(value: str) -> tuple[int, int, int]:
        if not re.fullmatch(r"v?\d+(?:\.\d+){0,2}", value):
            raise ValueError("Runtime version must be a stable semantic version")
        parts = [int(v) for v in value.removeprefix("v").split(".")]
        parts += [0, 0]
        return parts[0], parts[1], parts[2]

    actual = normalized(version)
    alternatives = []
    for alternative in re.split(r"\s*\|\|?\s*", constraint):
        accepted = True
        for token in re.split(r"[ ,]+", alternative.strip()):
            match = re.fullmatch(r"(\^|~|>=|<=|!=|>|<|==|=)?(v?\d+(?:\.\d+){0,2})", token)
            if not match:
                raise ValueError("This Composer runtime constraint requires an explicitly supported native validator")
            operator, value = match.groups()
            lower = normalized(value)
            if operator == "^":
                upper = (lower[0] + 1, 0, 0) if lower[0] else (0, lower[1] + 1, 0) if lower[1] else (0, 0, lower[2] + 1)
                result = lower <= actual < upper
            elif operator == "~":
                upper = (lower[0], lower[1] + 1, 0) if value.count(".") == 2 else (lower[0] + 1, 0, 0)
                result = lower <= actual < upper
            else:
                result = {
                    None: actual == lower,
                    "=": actual == lower,
                    "==": actual == lower,
                    "!=": actual != lower,
                    ">": actual > lower,
                    ">=": actual >= lower,
                    "<": actual < lower,
                    "<=": actual <= lower,
                }[operator]
            accepted = accepted and result
        alternatives.append(accepted)
    return any(alternatives)


def runtime_satisfies(requirements: dict[str, Any], version: str) -> bool:
    if digest({k: v for k, v in requirements.items() if k != "digest"}) != requirements["digest"]:
        raise ValueError("Retained runtime requirements digest changed")
    if requirements["runtime"] == "php":
        return composer_satisfies(version, requirements["runtime_requirement"])
    try:
        return Version(version) in SpecifierSet(requirements["runtime_requirement"])
    except (InvalidVersion, InvalidSpecifier) as exc:
        raise ValueError("Selected Python runtime requirements are invalid") from exc
