<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Firefly Forge 0.1.0

Firefly Forge introduces Firefly's software factory: a product brief becomes an
isolated Git implementation with real verification, retained source evidence
and explicit pull-request publication. PyFly and LaraFly are the primary backend
profiles. Angular, React, Next.js and Vue are selectable frontend contracts.

## Included in this version

- A PyFly API, local durable run store, CLI, Python client and MCP tools.
- Studio for repository registration, product briefs, provider connections,
  progress, diffs, check results, artifact downloads and explicit publication.
- Official signed-in Codex and Claude Code execution, native Firefly Agentic,
  and a client for an authorized internal Firefly Agent Bridge.
- Generic Git sources and GitHub, GitLab, Bitbucket Cloud, Azure DevOps and
  Gitea pull-request adapters.
- A bounded automatic repair loop that returns all failed-check evidence to
  the provider and preserves each attempt's results.
- Final passing verification tied to a source-tree snapshot, checked again
  before publication, plus exclusive service ownership of each data directory.
- Token protection across the Forge API, with native management/admin/actuator
  surfaces disabled in the packaged PyFly configuration.
- Firefly branding, self-hosted typography, accessible SVG diagrams and
  task-based installation, product, integration and operations guides.

## Verification recorded during preparation

The configured factory gate covers Python tests, lint, formatting and types;
Studio types, lint, formatting, unit tests, browser journeys and production
build; and strict MkDocs compilation.

Studio was also exercised against the real authenticated Forge API: connection,
run diff and verification evidence, an actual ZIP download and the product
form. Direct browser review covered 1440, 640, 390 and 360 pixel widths without
browser console errors.

Specific live product checks recorded so far:

| Journey | Evidence | Remaining boundary |
| --- | --- | --- |
| Native Firefly Agentic through an authorized bridge, PyFly service | Nine behavior tests, lint, types and package build | Bounded service fixture, not every PyFly capability |
| Native Firefly Agentic through an authorized bridge, LaraFly service | Fourteen actual PHP checks and a fresh Composer install using official LaraFly 26.10.1 | Bounded service fixture, not the complete LaraFly plus frontend journey |
| Codex full factory run, PyFly plus React | 39 backend tests, 12 frontend tests, lint, types and production builds; automatic repair reached a passing second implementation attempt | Generated-product browser acceptance is separate |

The full LaraFly plus React factory journey remains pending. Java, Rust and .NET
profiles have upstream source contracts inspected; they have no claimed live
full-product acceptance here. Frontend profile availability does not establish
acceptance of every generated framework combination. Pull-request adapter tests
do not establish successful publication on every provider account.

## Operating boundaries

This version serves trusted development operators on a dedicated macOS or Linux
host. Git workspace isolation is not host sandboxing or tenant isolation.
Provider configuration readiness is separate from real model execution.
Publication is explicit and does not merge, release or deploy a generated
product. Read [capabilities](capabilities.md) and [security](security.md) before
operating the service remotely.

Firefly Agent Bridge remains a separate private internal developer service.
This public release contains its client integration; authorized installation
instructions are available only through the [private repository guide](bridge.md).
