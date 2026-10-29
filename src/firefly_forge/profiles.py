# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Framework contracts used by every coding provider and validation stage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from firefly_forge.prompts import prompt_text

_BACKENDS = [
    {
        "id": "none",
        "name": "Frontend only",
        "version": "No backend",
        "detail": "A standalone browser application connected to documented service contracts.",
        "source": "",
        "instruction": prompt_text("implementation/backend-none"),
    },
    {
        "id": "pyfly",
        "name": "pyfly · Python",
        "version": "26.10.11 candidate · Python 3.13+",
        "detail": "Native PyFly DI, controllers, typed contracts and async services; Starlette ASGI.",
        "source": "https://github.com/fireflyframework/fireflyframework-pyfly/releases/tag/v26.10.11",
        "instruction": prompt_text("implementation/backend-pyfly", version="1.0.1"),
    },
    {
        "id": "java",
        "name": "Firefly · Java",
        "version": "Parent 26.07.06 · Java 25 · Spring Boot 3.5.10",
        "detail": "Reactive Firefly Spring Boot services with Maven, WebFlux and framework starters.",
        "source": "https://github.com/fireflyframework/fireflyframework-parent/blob/main/pom.xml",
        "instruction": prompt_text("implementation/backend-java"),
    },
    {
        "id": "larafly",
        "name": "larafly · PHP",
        "version": "26.10.4 candidate · PHP 8.5.12+ · Laravel/Illuminate 13.38+",
        "detail": "Laravel-native Firefly attributes, services, repositories, CQRS and typed contracts.",
        "source": "https://github.com/fireflyframework/fireflyframework-php/blob/8379ef8ed1d8a15ea13adba762815b900a937bc4/composer.json",
        "instruction": prompt_text("implementation/backend-larafly"),
    },
    {
        "id": "rust",
        "name": "Firefly · Rust",
        "version": "26.6.36 · Rust 1.88+",
        "detail": "Typed Firefly Rust services; upstream source inspected, live product verification pending.",
        "source": "https://github.com/fireflyframework/fireflyframework-rust/blob/main/Cargo.toml",
        "instruction": prompt_text("implementation/backend-rust"),
    },
    {
        "id": "dotnet",
        "name": "Firefly · .NET",
        "version": ".NET 10 · Framework 26.04.01",
        "detail": "Firefly .NET enterprise services; upstream source inspected, live product verification pending.",
        "source": "https://github.com/fireflyframework/fireflyframework-dotnet/blob/main/Directory.Build.props",
        "instruction": prompt_text("implementation/backend-dotnet"),
    },
]

_FRONTENDS = [
    {
        "id": "angular",
        "name": "Angular · Firefly Hub",
        "version": "Angular 22.0.4 · TypeScript 6.0.3 · Hub 22.x",
        "detail": "Current Firefly product-template with ng-hub-ui components and Playbook conventions.",
        "source": "https://github.com/fireflyframework/firefly-product-template/blob/develop/package.json",
        "instruction": prompt_text("implementation/frontend-angular"),
    },
    {
        "id": "react",
        "name": "React",
        "version": "React 19.3 · Vite 8.3",
        "detail": "Typed React application with accessible components and a production build.",
        "source": "https://react.dev/learn",
        "instruction": prompt_text("implementation/frontend-react"),
    },
    {
        "id": "nextjs",
        "name": "Next.js",
        "version": "Next.js 16.4 · React 19.3",
        "detail": "Next.js App Router with explicit server/client boundaries.",
        "source": "https://nextjs.org/docs/app",
        "instruction": prompt_text("implementation/frontend-nextjs"),
    },
    {
        "id": "vue",
        "name": "Vue",
        "version": "Vue 3.5.43 · Vite 8.3",
        "detail": "Vue Composition API, typed components and an accessible application shell.",
        "source": "https://vuejs.org/guide/introduction.html",
        "instruction": prompt_text("implementation/frontend-vue"),
    },
    {
        "id": "none",
        "name": "Backend only",
        "version": "",
        "detail": "No browser frontend.",
        "source": "",
        "instruction": prompt_text("implementation/frontend-none"),
    },
]


def profile_catalog() -> dict[str, list[dict[str, Any]]]:
    ordered = sorted(_BACKENDS, key=lambda p: ["pyfly", "larafly", "java", "rust", "dotnet", "none"].index(p["id"]))

    def public(profile: dict[str, str]) -> dict[str, Any]:
        live = profile["id"] in {"pyfly", "larafly", "react"}
        validation = (
            "Independent live PyFly/LaraFly service, API and React inventory fixture plus browser flow verified; "
            "every generated product still requires its own checks"
            if live
            else "Upstream contract inspected; live product verification pending"
        )
        candidate = profile["id"] in {"pyfly", "larafly"}
        if candidate:
            validation = (
                "Official stable release metadata verified; generation requires a ready exact corpus, "
                "native runtime prerequisites and installed-framework/product checks. "
                "Historical live fixture results do not qualify this new release."
            )
        return {
            **{k: v for k, v in profile.items() if k != "instruction"},
            "validation_state": "requires_run_validation"
            if candidate
            else "live_fixture_verified"
            if live
            else "contract_inspected",
            "validation": validation if profile["id"] != "none" else "Not applicable",
        }

    return {"backends": [public(p) for p in ordered], "frontends": [public(p) for p in _FRONTENDS]}


def build_instruction(brief: str, backend: str, frontend: str, *, target: dict[str, Any] | None = None) -> str:
    from firefly_forge.framework_knowledge_targets import CURRENT_FRAMEWORK_TARGET

    target = target or CURRENT_FRAMEWORK_TARGET.get()
    if frontend == "next":
        frontend = "nextjs"
    selected = []
    for kind, value, catalog in [("backend", backend, _BACKENDS), ("frontend", frontend, _FRONTENDS)]:
        match = next((p for p in catalog if p["id"] == value), None)
        if match is None:
            raise ValueError(f"Unknown {kind} profile: {value}")
        if kind == "backend" and target and target.get("framework") == backend:
            selected.append(
                prompt_text(
                    "implementation/backend-" + backend,
                    version="1.0.2" if backend == "pyfly" else "1.0.1",
                    framework_version=target["framework_version"],
                    **(
                        {
                            "runtime_requirement": "PHP "
                            + target["runtime_requirements"]["runtime_requirement"]
                            + "; Laravel/Illuminate "
                            + json.dumps(
                                {
                                    k: v
                                    for k, v in target["runtime_requirements"]["dependencies"].items()
                                    if k.startswith("illuminate/")
                                },
                                sort_keys=True,
                            )
                        }
                        if backend == "larafly"
                        else {}
                    ),
                )
            )
        else:
            selected.append(match["instruction"])
    if backend == frontend == "none":
        raise ValueError("Select at least one backend or frontend profile")
    return "\n\n".join(
        [
            prompt_text("implementation/base"),
            *selected,
            prompt_text("implementation/request", brief=brief),
        ]
    )


def verification_commands(workspace: Path, backend: str, frontend: str) -> list[list[str]]:
    """Select real project checks, refusing to manufacture a passing empty gate."""
    commands: list[list[str]] = []
    if (workspace / "pyproject.toml").is_file():
        commands.extend(
            [
                ["uv", "run", "python", "-m", "compileall", "-q", "src"],
                ["uv", "run", "pytest"],
                ["uv", "run", "ruff", "check", "."],
                ["uv", "run", "pyright"],
            ]
        )
    if (workspace / "pom.xml").is_file():
        commands.append(["./mvnw" if (workspace / "mvnw").is_file() else "mvn", "verify"])
    if (workspace / "Cargo.toml").is_file():
        commands.extend(
            [
                ["cargo", "fmt", "--check"],
                ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"],
                ["cargo", "test"],
            ]
        )
    if list(workspace.glob("*.sln")) or list(workspace.glob("*.slnx")) or list(workspace.glob("*.csproj")):
        commands.extend([["dotnet", "format", "--verify-no-changes"], ["dotnet", "build"], ["dotnet", "test"]])
    if (workspace / "composer.json").is_file():
        data = json.loads((workspace / "composer.json").read_text())
        scripts = data.get("scripts", {})
        for name in ["test", "lint", "typecheck", "analyse"]:
            if name in scripts:
                commands.append(["composer", "run", name])
        if "test" not in scripts:
            commands.append(["vendor/bin/pest" if (workspace / "vendor/bin/pest").is_file() else "vendor/bin/phpunit"])
    for directory in [workspace, workspace / "frontend", workspace / "web", workspace / "client"]:
        manifest = directory / "package.json"
        if not manifest.is_file():
            continue
        scripts = json.loads(manifest.read_text()).get("scripts", {})
        manager = (
            "pnpm"
            if (directory / "pnpm-lock.yaml").is_file()
            else "yarn"
            if (directory / "yarn.lock").is_file()
            else "npm"
        )
        prefix = [manager] if directory == workspace else [manager, "--prefix", str(directory)]
        for name in ["test", "lint", "typecheck", "build"]:
            if name in scripts:
                commands.append([*prefix, "run", name])
    if not commands:
        raise ValueError("No configured project verification commands were found")
    return commands
