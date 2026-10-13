# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Framework contracts used by every coding provider and validation stage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_BACKENDS = [
    {
        "id": "none",
        "name": "Frontend only",
        "version": "No backend",
        "detail": "A standalone browser application connected to documented service contracts.",
        "source": "",
        "instruction": "Do not create a backend. Implement the selected frontend against documented API contracts. "
        "Keep credentials server-side in the authorized service; do not embed service secrets in the browser.",
    },
    {
        "id": "pyfly",
        "name": "PyFly · Python",
        "version": "26.10.1 · Python 3.13+",
        "detail": "Native PyFly DI, controllers, typed contracts and async services; Starlette ASGI.",
        "source": "https://github.com/fireflyframework/fireflyframework-pyfly/releases/tag/v26.10.01",
        "instruction": (
            "Use pyfly 26.10.1 with Python >=3.13. Use pyfly.core.pyfly_application, PyFlyApp"
            "lication, pyfly.container.rest_controller/service, pyfly.web mappings, and pyfly"
            ".web.adapters.starlette.create_app. Reuse PyFly CLI templates (web-api or hexago"
            "nal) where suitable. Obtain PyFly from its official GitHub Release wheel, not an"
            " assumed PyPI distribution. Write meaningful pytest tests; configure ruff and a "
            "type checker. Keep business logic in services and typed domain contracts."
            " Depend directly on pyfly[web] @ "
            "https://github.com/fireflyframework/fireflyframework-pyfly/releases/download/"
            "v26.10.01/pyfly-26.10.1-py3-none-any.whl in the manifest; this release URL is verified."
            " Honor its dependency floors: Pydantic>=2.13.3, Starlette>=1.0.0, Uvicorn>=0.46.0. "
            "Do not pin incompatible older dependencies."
            " PyFly lifecycle methods are await runtime.startup() and await runtime.shutdown(), "
            "not start()/close(). Pass runtime.context to create_app and wire lifecycle through ASGI lifespan."
            " For browser mutations, forward the XSRF-TOKEN cookie as X-XSRF-TOKEN in the same-origin API client. "
            "Keep CSRF protection enabled and verify the real cookie/header flow, not just mocked fetch responses."
        ),
    },
    {
        "id": "java",
        "name": "Firefly · Java",
        "version": "Parent 26.07.06 · Java 25 · Spring Boot 3.5.10",
        "detail": "Reactive Firefly Spring Boot services with Maven, WebFlux and framework starters.",
        "source": "https://github.com/fireflyframework/fireflyframework-parent/blob/main/pom.xml",
        "instruction": (
            "Use Java 25 and Firefly Framework parent 26.07.06 (org.fireflyframework:fireflyf"
            "ramework-parent), Spring Boot 3.5.10, reactive WebFlux and appropriate Firefly s"
            "tarters. Reuse flywork core/domain/application/library archetypes after inspecti"
            "ng their templates; disable git initialization. Write JUnit behavioral tests and"
            " use Maven verify. Do not invent Firefly artifact coordinates; inspect upstream "
            "starter POMs."
        ),
    },
    {
        "id": "larafly",
        "name": "LaraFly · PHP",
        "version": "26.10.1 · PHP 8.3+ · Laravel 13",
        "detail": "Laravel-native Firefly attributes, services, repositories, CQRS and typed contracts.",
        "source": "https://github.com/fireflyframework/fireflyframework-php/blob/main/composer.json",
        "instruction": (
            "Use PHP >=8.3, Laravel/Illuminate 13 and fireflyframework/larafly. Inspect the o"
            "fficial bundled skeleton and CLI make stubs for exact namespaces. Reuse firefly "
            "new --api or --web --no-git --no-interaction; never --force. Write Pest/PHPUnit "
            "behavioral tests; configure Laravel Pint and PHPStan/Larastan. Use native Firefl"
            "y attributes and service composition."
        ),
    },
    {
        "id": "rust",
        "name": "Firefly · Rust",
        "version": "26.6.36 · Rust 1.88+",
        "detail": "Typed Firefly Rust services; upstream source inspected, live product verification pending.",
        "source": "https://github.com/fireflyframework/fireflyframework-rust/blob/main/Cargo.toml",
        "instruction": (
            "Use Rust >=1.88 and the Firefly Rust 26.6.36 workspace contracts. Inspect upstre"
            "am crates/firefly and starter-core/application/domain before selecting crate coo"
            "rdinates; do not invent APIs. Pin exact upstream revisions when using git depend"
            "encies. Write behavioral cargo tests, run cargo fmt --check, cargo clippy --all-"
            "targets -- -D warnings and cargo test."
        ),
    },
    {
        "id": "dotnet",
        "name": "Firefly · .NET",
        "version": ".NET 10 · Framework 26.04.01",
        "detail": "Firefly .NET enterprise services; upstream source inspected, live product verification pending.",
        "source": "https://github.com/fireflyframework/fireflyframework-dotnet/blob/main/Directory.Build.props",
        "instruction": (
            "Use .NET 10 (net10.0), nullable references and the Firefly .NET 26.04.01 source "
            "contracts. Inspect upstream project references and Directory.Packages.props for "
            "real package names; do not invent NuGet APIs. Use framework DI/domain/CQRS compo"
            "nents where appropriate. Write xUnit behavioral tests; configure dotnet format -"
            "-verify-no-changes, dotnet build and dotnet test."
        ),
    },
]

_FRONTENDS = [
    {
        "id": "angular",
        "name": "Angular · Firefly Hub",
        "version": "Angular 22.0.4 · TypeScript 6.0.3 · Hub 22.x",
        "detail": "Current Firefly product-template with ng-hub-ui components and Playbook conventions.",
        "source": "https://github.com/fireflyframework/firefly-product-template/blob/develop/package.json",
        "instruction": (
            "Follow the current firefly-frontend-playbook main doctrine and firefly-product-t"
            "emplate develop v0.3.0. Use Angular ^22.0.4, TypeScript ~6.0.3, and ng-hub-ui hu"
            "b-* components; ff-* components are deprecated. Inspect the Hub component catalo"
            "g and Angular checklist before coding. Use standalone components, lazy feature r"
            "outes, typed services, accessible semantics, tokens, page/section composition an"
            "d the matching PageBase. Preserve the core/layout, shared/ui, features/<name>/ui"
            " convention. Pin and record the template revision. Do not run init.sh unchanged:"
            " it creates an initial commit. Explicitly materialize self-contained or detached"
            " topology as requested. Embedded mode is not implemented; auth provider flags ar"
            "e metadata only and require real implementation. Add behavioral Vitest tests, li"
            "nt/type checks, production build, and Playwright user-flow checks."
        ),
    },
    {
        "id": "react",
        "name": "React",
        "version": "React 19.3 · Vite 8.3",
        "detail": "Typed React application with accessible components and a production build.",
        "source": "https://react.dev/learn",
        "instruction": (
            "Use React 19.3 with TypeScript and Vite 8.3. Build accessible, responsive compon"
            "ents, typed API clients and deliberate loading/error/empty states. Keep applicat"
            "ion logic in testable hooks/services. Configure behavioral Vitest and Testing Li"
            "brary tests, ESLint, tsc --noEmit and a production build."
        ),
    },
    {
        "id": "nextjs",
        "name": "Next.js",
        "version": "Next.js 16.4 · React 19.3",
        "detail": "Next.js App Router with explicit server/client boundaries.",
        "source": "https://nextjs.org/docs/app",
        "instruction": (
            "Use Next.js 16.4, React 19.3 and TypeScript. Use the App Router; make server/cli"
            "ent boundaries explicit and keep secrets on the server. Build accessible, respon"
            "sive components with loading/error states. Configure behavioral unit tests, ESLi"
            "nt, tsc --noEmit, production next build and Playwright critical-path tests."
        ),
    },
    {
        "id": "vue",
        "name": "Vue",
        "version": "Vue 3.5.43 · Vite 8.3",
        "detail": "Vue Composition API, typed components and an accessible application shell.",
        "source": "https://vuejs.org/guide/introduction.html",
        "instruction": (
            "Use Vue 3.5.43 with TypeScript, Composition API, script setup, Vue Router and Vi"
            "te 8.3. Build accessible, responsive components and typed API clients. Configure"
            " behavioral Vitest/Vue Test Utils tests, ESLint, vue-tsc --noEmit and a producti"
            "on build."
        ),
    },
    {
        "id": "none",
        "name": "Backend only",
        "version": "",
        "detail": "No browser frontend.",
        "source": "",
        "instruction": (
            "Do not generate a browser frontend. Deliver the requested service or library wit"
            "h runnable entry points and documented API contracts."
        ),
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
        return {
            **{k: v for k, v in profile.items() if k != "instruction"},
            "validation_state": "live_fixture_verified" if live else "contract_inspected",
            "validation": validation if profile["id"] != "none" else "Not applicable",
        }

    return {"backends": [public(p) for p in ordered], "frontends": [public(p) for p in _FRONTENDS]}


def build_instruction(brief: str, backend: str, frontend: str) -> str:
    if frontend == "next":
        frontend = "nextjs"
    selected = []
    for kind, value, catalog in [("backend", backend, _BACKENDS), ("frontend", frontend, _FRONTENDS)]:
        match = next((p for p in catalog if p["id"] == value), None)
        if match is None:
            raise ValueError(f"Unknown {kind} profile: {value}")
        selected.append(match["instruction"])
    if backend == frontend == "none":
        raise ValueError("Select at least one backend or frontend profile")
    return "\n\n".join(
        [
            "You are implementing a real software product inside an isolated factory workspace. "
            "Read existing files and repository AGENTS.md before editing; reuse existing patterns. "
            "Implement the arbitrary product brief fully as real runnable source code. "
            "A plan, architecture narrative, placeholder, canned recipe or assumed test success is insufficient. "
            "Do not commit, push, publish, deploy, alter git metadata, read credentials "
            "or edit outside this workspace. "
            "Do not delete unrelated work. Write meaningful behavioral tests, dependency manifests, "
            "a runnable entry point, a README with exact launch/verification instructions "
            "and .env.example where needed. "
            "Use environment configuration for secrets; never hardcode credentials. "
            "Install only declared project dependencies if the environment permits, then run "
            "actual build/tests/lint/type checks. "
            "Report failed checks and environmental limitations honestly. "
            "Do not turn live integrations into mock claims.",
            *selected,
            "PRODUCT BRIEF:\n" + brief,
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
