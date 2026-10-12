<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# firefly forge 0.2.0

This version connects the software factory's interface to durable development plans and real execution records. The production floor uses the Firefly website's 3D machine geometry; machines run only when recorded tasks are active. Paused work stops the scene, and the stage controls remain usable without WebGL or animation.

## What changed

- Native PyFly dependency injection, REST controllers, repositories and transaction management with PostgreSQL persistence.
- Immutable plan revisions, append-only intents and receipts, source checkpoints, bounded rate-limit retries and explicit provider handoffs.
- Separate project workspaces and fair dispatch, with one active run per project and a configurable global concurrency limit.
- Wizard flows for product work, coding providers, repository applications, knowledge and deployment targets.
- GitHub Apps, GitLab OAuth with PKCE and Bitbucket OAuth consumers, with repository grants checked before transport.
- A versioned central policy catalog, project defaults and frozen run selections. Recommendations guide coding; quality gates run commands.
- Real flycanon SDK indexing, cited retrieval and verified project memory in separate project scopes.
- Explicit deployment and rollback commands, mandatory health checks, frozen target settings and durable external-effect records.
- Shared brand-kit typography, grid and light/dark/system themes, with a distinct lowercase forge mark.
- CLI, Python client and MCP access to plans, journals, handoffs, knowledge and delivery.

## Storage upgrade

PostgreSQL is required. Configure `FORGE_DATABASE_URL` and retain the private data directory for credentials, policy revisions and run workspaces. This version does not silently import a previous SQLite database. Existing 0.1.0 deployments should back up their state and follow [installation](installation.md) before starting the new runtime.

## Acceptance boundaries

The local runtime has exercised both released PyFly and LaraFly example products with their actual test commands. A local deployment used a real command and an independent verification command. Flycanon acceptance includes real 768-dimensional Ollama embeddings, source and memory retrieval, separate-project isolation and a verified execution journal. This local proof uses the [documented server scanner correction](knowledge.md#flycanon-server-compatibility), with strict PII rejection retained.

Application adapter tests do not prove that an external organization has registered or granted a GitHub, GitLab or Bitbucket application. Live model execution still depends on the server account's subscription, login, credits and selected model. Java, Rust and .NET remain additional profiles without claimed end-to-end product acceptance.

## 0.1.0 — Earlier release

Firefly Forge introduces Firefly's software factory: a product brief becomes an
isolated Git implementation with real verification, retained source evidence
and explicit pull-request publication. PyFly and LaraFly are the primary backend
profiles. Angular, React, Next.js and Vue are selectable frontend contracts.

### Included in this version

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

### Verification recorded during preparation

The configured factory gate covers Python tests, lint, formatting and types;
Studio types, lint, formatting, unit tests, browser journeys and production
build; and strict MkDocs compilation.

Studio was also exercised against the real authenticated Forge API: connection,
repository and run creation, cancellation, run diff and verification evidence,
an actual ZIP download and the product form. Direct browser review covered 1440,
640, 390 and 360 pixel widths without
browser console errors.

Specific live product checks recorded so far:

| Journey | Evidence | Remaining boundary |
| --- | --- | --- |
| Native Firefly Agentic through an authorized bridge, PyFly service | Nine behavior tests, lint, types and package build | Bounded service fixture, not every PyFly capability |
| Native Firefly Agentic through an authorized bridge, LaraFly service | Fourteen actual PHP checks and a fresh Composer install using official LaraFly 26.10.1 | Bounded service fixture, not the complete LaraFly plus frontend journey |
| Codex full factory run, PyFly plus React | 40 backend tests, 17 frontend tests, lint, types and production builds; automatic repair corrected startup and browser CSRF integration | Actual browser create/reserve/conflict flows, mobile layout and keyboard focus verified |
| Codex full factory run, LaraFly plus React | 32 backend tests with 94 assertions, 23 frontend tests, PHPStan, Pint, lint, types and production builds | Actual browser create/reserve/conflict flows, mobile layout and keyboard focus verified |

Both primary browser products created eight units of stock, reserved three,
showed five remaining, and rejected an over-reservation without changing stock.
Their 390 pixel layouts had no horizontal overflow and their browsers reported
no application exceptions.

The GitHub publication connector was exercised against the real repository:
isolated clone, source snapshot, passing check, branch push and draft pull-request
creation. The temporary pull request was closed without merging. The MCP stdio
transport completed a real handshake and an authenticated repository tool call.
Release archives passed the private-file boundary check, and a fresh wheel
installation served Studio and the authenticated API with its packaged runtime
configuration.

Java, Rust and .NET
profiles have upstream source contracts inspected; they have no claimed live
full-product acceptance here. Frontend profile availability does not establish
acceptance of every generated framework combination. Pull-request adapter tests
do not establish successful publication on every provider account.

### Operating boundaries

This version serves trusted development operators on a dedicated macOS or Linux
host. Git workspace isolation is not host sandboxing or tenant isolation.
Provider configuration readiness is separate from real model execution.
Publication is explicit and does not merge, release or deploy a generated
product. Read [capabilities](capabilities.md) and [security](security.md) before
operating the service remotely.

Firefly Agent Bridge remains a separate private internal developer service.
This public release contains its client integration; authorized installation
instructions are available only through the [private repository guide](bridge.md).
