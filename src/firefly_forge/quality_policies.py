# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project-selected quality recipes extend the incumbent policy command pipeline."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from firefly_forge.models import Command, NewWorkCommand
from firefly_forge.product_provenance import content_digest as digest
from firefly_forge.prompts import prompt_text
from firefly_forge.quality_gate_models import QualityGateContractV1, QualityStepDescriptorV1, quality_version

Data = dict[str, Any]


class ProjectQualityPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    baseline: Literal["builtin-v1"] | None = "builtin-v1"
    skills: list[str] = Field(default_factory=list)
    tool_versions: dict[str, str] = Field(default_factory=dict)
    python: str = "python"
    php: str = "php"
    node: str = "node"
    command_timeout_seconds: int | None = Field(default=None, gt=0)

    _version = field_validator("version", mode="before")(quality_version)

    @field_validator("python", "php", "node")
    @classmethod
    def executable(cls, value: str) -> str:
        Command(argv=[value])
        return value

    @field_validator("tool_versions")
    @classmethod
    def exact_versions(cls, values: dict[str, str]) -> dict[str, str]:
        if any(
            not name.strip() or not version.strip() or any(ord(c) < 32 for c in version)
            for name, version in values.items()
        ):
            raise ValueError("Configure exact approved quality-tool versions")
        return values


class QualityCommand(NewWorkCommand):
    model_config = ConfigDict(extra="forbid", frozen=True)
    gate_id: str = Field(exclude=True)
    phase: Literal["prerequisite", "check"] = Field(exclude=True)
    quality_contract: QualityGateContractV1 = Field(exclude=True)


def quality_descriptors(commands: list[Command | NewWorkCommand]) -> list[QualityStepDescriptorV1]:
    result = []
    seen = set()
    for ordinal, command in enumerate(commands, 1):
        if not isinstance(command, QualityCommand):
            continue
        key = command.gate_id, command.phase
        if key in seen:
            raise ValueError("Quality gate phases must have one unambiguous command")
        seen.add(key)
        result.append(
            QualityStepDescriptorV1(
                ordinal=ordinal,
                gate_id=command.gate_id,
                phase=command.phase,
                contract=command.quality_contract,
                command=NewWorkCommand.model_validate(command.model_dump(mode="json")),
            )
        )
    return result


_MYPY = "[mypy]\nstrict = True\nfollow_imports = normal\nwarn_unused_ignores = True\n"
_PHPSTAN = "parameters:\n    level: max\n    paths:\n        - .\n    tmpDir: %env.TMPDIR%/forge-phpstan\n"
_TSCONFIG = (
    json.dumps(
        {
            "compilerOptions": {
                "strict": True,
                "noEmit": True,
                "allowJs": True,
                "checkJs": True,
                "target": "ES2022",
                "module": "ESNext",
                "moduleResolution": "Bundler",
                "jsx": "react-jsx",
                "skipLibCheck": False,
            },
            "include": ["."],
            "exclude": ["node_modules", "dist", "build"],
        },
        indent=2,
    )
    + "\n"
)
_ESLINT = (
    'export default [{ ignores: ["node_modules/**", "dist/**", "build/**"] }, '
    '{ files: ["**/*.{js,mjs,cjs,jsx}"], languageOptions: { ecmaVersion: "latest", sourceType: "module", '
    "parserOptions: { ecmaFeatures: { jsx: true } } }, "
    'rules: { "no-debugger": "error", "no-constant-condition": "error", "no-unreachable": "error" } }];\n'
)


def baseline_quality_policies(policy: ProjectQualityPolicyV1, backend: str, frontend: str) -> list[Data]:
    if policy.baseline is None:
        return []
    recipes: list[tuple[str, str, str, str, list[str], list[str], dict[str, str], str]] = []
    if backend == "pyfly":
        prefix = [policy.python, "-B", "-I", "-m"]
        recipes.extend(
            [
                (
                    "python-lint",
                    "ruff",
                    "ruff",
                    "lint",
                    [*prefix, "ruff", "--version"],
                    [
                        *prefix,
                        "ruff",
                        "check",
                        "--no-cache",
                        "--isolated",
                        "--select",
                        "E,F,I,UP,B,SIM",
                        "--ignore-noqa",
                        "--no-respect-gitignore",
                        "--output-format",
                        "sarif",
                        ".",
                    ],
                    {},
                    "ruff ",
                ),
                (
                    "python-format",
                    "ruff",
                    "ruff",
                    "format",
                    [*prefix, "ruff", "--version"],
                    [*prefix, "ruff", "format", "--no-cache", "--isolated", "--check", "--line-length", "120", "."],
                    {},
                    "ruff ",
                ),
                (
                    "python-types",
                    "mypy",
                    "mypy",
                    "types",
                    [*prefix, "mypy", "--version"],
                    [*prefix, "mypy", "--config-file", "forge-quality-mypy.ini", "--cache-dir", "/dev/null", "."],
                    {"forge-quality-mypy.ini": _MYPY},
                    "mypy ",
                ),
            ]
        )
    if backend == "larafly":
        recipes.extend(
            [
                (
                    "php-format",
                    "laravel/pint",
                    "Pint",
                    "format",
                    [policy.php, "vendor/bin/pint", "--version"],
                    [
                        policy.php,
                        "vendor/bin/pint",
                        "--test",
                        "--no-config",
                        "--preset",
                        "laravel",
                        "--cache-file",
                        "/dev/null",
                    ],
                    {},
                    "Pint ",
                ),
                (
                    "php-types",
                    "phpstan/phpstan",
                    "PHPStan",
                    "types",
                    [policy.php, "vendor/bin/phpstan", "--version"],
                    [
                        policy.php,
                        "vendor/bin/phpstan",
                        "analyse",
                        "--configuration",
                        "forge-quality-phpstan.neon",
                        "--no-progress",
                    ],
                    {"forge-quality-phpstan.neon": _PHPSTAN},
                    "PHPStan - PHP Static Analysis Tool ",
                ),
            ]
        )
    if frontend != "none":
        recipes.extend(
            [
                (
                    "web-types",
                    "typescript",
                    "TypeScript",
                    "types",
                    [policy.node, "node_modules/typescript/bin/tsc", "--version"],
                    [policy.node, "node_modules/typescript/bin/tsc", "--project", "forge-quality-tsconfig.json"],
                    {"forge-quality-tsconfig.json": _TSCONFIG},
                    "Version ",
                ),
                (
                    "web-lint",
                    "eslint",
                    "ESLint",
                    "lint",
                    [policy.node, "node_modules/eslint/bin/eslint.js", "--version"],
                    [
                        policy.node,
                        "node_modules/eslint/bin/eslint.js",
                        "--no-config-lookup",
                        "--config",
                        "forge-quality-eslint.config.mjs",
                        ".",
                    ],
                    {"forge-quality-eslint.config.mjs": _ESLINT},
                    "v",
                ),
            ]
        )
    skills = []
    for ident, package, name, category, probe, argv, configs, version_prefix in recipes:
        ecosystem = "python" if ident.startswith("python-") else "composer" if ident.startswith("php-") else "node"
        contract = QualityGateContractV1.model_validate(
            {
                "version": 1,
                "tool": {
                    "package": package,
                    "name": name,
                    "ecosystem": ecosystem,
                    "lock_file": {"python": "uv.lock", "composer": "composer.lock", "node": "package-lock.json"}[
                        ecosystem
                    ],
                    "probe": {"argv": probe, "timeout": policy.command_timeout_seconds},
                    "version_prefix": version_prefix,
                    "approved_version": policy.tool_versions.get(package),
                },
                "category": category,
                "format": "sarif-2.1.0" if ident == "python-lint" else "exit-status",
                "configuration_paths": list(configs),
                "approved_configuration": {
                    path: hashlib.sha256(value.encode()).hexdigest() for path, value in configs.items()
                },
            }
        )
        skills.append(
            {
                "id": "quality-" + ident + "-v1",
                "name": name + " " + category,
                "description": "Required " + category + " checks for this component",
                "category": "quality",
                "kind": "quality_gate",
                "default": False,
                "instructions": prompt_text(
                    "skills/quality-gate",
                    category=category,
                    tool=name,
                    configuration_files=json.dumps(configs, sort_keys=True),
                ),
                "commands": [
                    {
                        "argv": argv,
                        "timeout": policy.command_timeout_seconds,
                        "backends": [backend],
                        "quality": contract.model_dump(mode="json"),
                    }
                ],
            }
        )
    return skills


def apply_project_quality(
    snapshot: Data, policy: ProjectQualityPolicyV1 | None, backend: str, frontend: str, catalog: Any
) -> Data:
    if policy is None:
        return snapshot
    required = baseline_quality_policies(policy, backend, frontend)
    if policy.skills:
        selected = catalog.selection(policy.skills)["skills"]
        if any(skill["kind"] != "quality_gate" for skill in selected):
            raise ValueError("Project-required quality policies must contain executable gates")
        required += selected
    value = copy.deepcopy(snapshot)
    seen = {skill["id"] for skill in required}
    value["skills"] = [skill for skill in value["skills"] if skill["id"] not in seen] + required
    value["project_quality_policy"] = policy.model_dump(mode="json")
    value.pop("digest", None)
    return {**value, "digest": digest(value)}
