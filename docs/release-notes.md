---
description: Review firefly forge release changes, runtime requirements and the verification boundaries of each published version.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# firefly forge release notes

## 0.2.3

Repository-scoped learning records retain their question, source tree, checks and review evidence in PostgreSQL. Native knowledge-first tools can reuse an exact eligible local outcome. Cached reuse holds a shared lock on the selected record and rechecks its policy, expiry and current execution authority before returning it. Explicit invalidation preserves the historical receipt. Submission to flycanon is a separate, single-dispatch action; uncertain submission requires observation rather than another write. Automatic retrieval from projected learning namespaces, classified repair controls and sharing between projects remain open. See [learning](learning.md).

Administrators can opt into an authenticated local command controller with immutable profiles, project quotas and bounded Docker execution. Component bundles transfer sanitized source and a minimum Git snapshot. The controller retains command results and returned artifacts; concurrent source edits reject publication without losing either version. Cached results still require current task ownership and authority. This release's qualification covers a trusted Factory host and local command containers. Browser pools, complete coding-provider isolation, Kubernetes and multi-host placement require further qualification. See [runner setup](sandbox.md#administrator-opt-in-local-command-pools).

The external-tool wizard separates saving a connection, requesting discovery, reviewing exact schemas and granting project access. GitHub repository-read tools remain the first executable external adapter. Figma, Sentry and Grafana guides show prerequisites and their blocked state. The [walkthrough](external-tools-tour.md) joins the Studio and search guides in a gallery of 228 source-bound light/dark desktop/phone captures. Their fixtures are identified in the guides.

SCM credential custody refuses silent reuse when a confidential client or provider host changes. A retained refresh-dispatch marker blocks reuse after an uncertain rotating-token exchange. Approved MCP calls recheck current authority before returning persisted or cached results. The package uses public pyfly 26.10.2 with explicit web, data, PostgreSQL, security and OAuth client dependencies. The separate general-provider OAuth foundation is outside this release.

Documentation now renders Mermaid through Material's native runtime. The product lifecycle and AS-IS handoff diagrams use readable vertical layouts on desktop and phones. Local browser checks use a loopback documentation server that admits gallery asset bursts and retain the exact SVG snapshot inspected during theme changes.

## 0.2.2

Administrators can configure internet search through a three-step Studio wizard. Existing provider settings remain selected; changing providers requires a new write-only key. Saving and making a potentially billable connection test are separate actions. Desktop and mobile screenshots are tied to the current frontend source and package version.

Agent search and approved external MCP calls now retain a PostgreSQL intent before dispatch and a bounded receipt afterwards. Project, repository, role, source and configuration snapshots define the invocation. Unknown outcomes block source consolidation and require explicit reconciliation. The first external adapter supports reviewed GitHub repository-read tools; other connector templates state their unmet prerequisites.

Campaigns freeze their tool catalog after capturing the repository's MCP grants and reject settings changed during admission. Native FireflyAgent tool errors retain quota, authentication and rate-limit pauses or the need for reconciliation. Internal whole-run retries are disabled so the journaled supervisor controls continuation.

## 0.2.1

Standalone packages now retain the Flycanon SDK release URL in their dependency metadata. The package check rejects wheels that would require an unavailable registry package. The v0.2.0 tag records the initial qualification; its publication was stopped when a clean installation exposed this issue.

This release brings product definition, coding, verification and delivery into one [pyfly](https://fireflyframework.org/frameworks/python/) application with a PostgreSQL execution ledger. Teams can start with a new product or analyze an existing project, work across one or several repositories, and follow the same retained task history through interruptions and provider handoffs.

Studio presents that work through guided forms, a project flow board and an operational production floor. Its machines animate recorded active tasks. Paused work stops the scene; stage controls remain available without WebGL or animation.

## What changed

- Native pyfly dependency injection, REST controllers, repositories and transaction management with PostgreSQL persistence.
- Immutable plan revisions, append-only intents and receipts, source checkpoints, bounded rate-limit retries and explicit provider handoffs.
- Separate project workspaces and fair dispatch, with one active run per project and a configurable global concurrency limit.
- Wizard flows for product work, coding providers, repository applications, knowledge and deployment targets.
- GitHub Apps, GitLab OAuth with PKCE and Bitbucket OAuth consumers, with repository grants checked before transport.
- A versioned central policy catalog, project defaults and frozen run selections. Recommendations guide coding; quality gates run commands.
- Real flycanon SDK indexing, cited retrieval and verified project memory in separate project scopes.
- Explicit deployment and rollback commands, mandatory health checks, frozen target settings and durable external-effect records.
- Shared brand-kit typography, grid and light/dark/system themes, with a distinct lowercase forge mark.
- CLI, Python client and MCP access to plans, journals, handoffs, knowledge and delivery.
- Journey-based product definitions, feature changes, architecture choices, project design tokens, brand imports and reviewable design evidence. API-only products can omit frontend work.
- An AI design assistant with retained conversations, Markdown, document and image attachments, source receipts and proposals that remain drafts until the product owner reviews them.
- Responsive editor and assistant panels with a shared header, reachable actions and a drawer on narrow or short windows. Application frames use the available width while prose retains readable line lengths.
- Keycloak sign-in, durable encrypted server sessions, project memberships and guided user administration. API replicas and a single headless execution worker have separate runtime roles.
- Isolated command and browser runners with bounded resources, retained outputs, DOM checks and screenshot evidence. Community skills and agent responsibilities are configurable through the factory's policy and agent catalogs.
- Reconciliation of uncertain publication and delivery against immutable server receipts. A matching task, attempt and source is required before recording an outcome; reconciliation does not repeat an external write.

## Storage upgrade

PostgreSQL is required. Configure `FORGE_DATABASE_URL` and retain the private data directory for credentials, policy revisions and run workspaces. This version does not silently import a previous SQLite database. Existing 0.1.0 deployments should back up their state and follow [installation](installation.md) before starting the new runtime.

## Acceptance boundaries

The local runtime has exercised both released pyfly and [larafly](https://fireflyframework.org/frameworks/php/) example products with their actual test commands. Current source-only factory runs passed 40 pyfly tests and 32 larafly tests with 94 assertions. Those runs retained valid write-ahead journals and left the saved product definition unchanged. They verify existing source without calling a coding model.

Separate live Codex acceptance covers a retained design conversation with a document and an image. Its response identified facts from both original files; completed replies survive service restart and cannot be recovered as unfinished work. Native Docker and browser acceptance exercised resource limits, cancellation, command output, JavaScript/DOM interactions and screenshots against pinned local images.

A local deployment used a real command and an independent verification command. Flycanon acceptance includes real 768-dimensional Ollama embeddings, source and memory retrieval, separate-project isolation and a verified execution journal. The [server scanner correction](https://github.com/firefly-operationOS/flycanon/pull/76) has been merged and released; strict PII rejection is retained. This does not establish access to every hosted Flycanon workspace.

Application adapter tests do not prove that an external organization has registered or granted a GitHub, GitLab or Bitbucket application. Cloud recipes cover AWS, Azure, Google Compute Engine, DigitalOcean, VPS and on-premises hosts; recipe validation is separate from provisioning each provider. Live model execution depends on the server account's subscription, login, credits and selected model. Java, Rust and .NET remain additional profiles without claimed end-to-end product acceptance.

The private Firefly Agent Bridge is distributed separately. Local subscription execution requires the official CLI's account login and an authorized connection; a hosted factory does not automatically tunnel to a developer's machine. Distributed execution worker pools and automatic external identity-provider federation are outside this release.

## 0.1.0: Earlier release

firefly forge introduces Firefly's software factory: a product brief becomes an
isolated Git implementation with real verification, retained source evidence
and explicit pull-request publication. pyfly and larafly are the primary backend
profiles. Angular, React, Next.js and Vue are selectable frontend contracts.

### Included in this version

- A pyfly API, local durable run store, CLI, Python client and MCP tools.
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
  surfaces disabled in the packaged pyfly configuration.
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
| Native Firefly Agentic through an authorized bridge, pyfly service | Nine behavior tests, lint, types and package build | Bounded service fixture, not every pyfly capability |
| Native Firefly Agentic through an authorized bridge, larafly service | Fourteen actual PHP checks and a fresh Composer install using official larafly 26.10.1 | Bounded service fixture, not the complete larafly plus frontend journey |
| Codex full factory run, pyfly plus React | 40 backend tests, 17 frontend tests, lint, types and production builds; automatic repair corrected startup and browser CSRF integration | Actual browser create/reserve/conflict flows, mobile layout and keyboard focus verified |
| Codex full factory run, larafly plus React | 32 backend tests with 94 assertions, 23 frontend tests, PHPStan, Pint, lint, types and production builds | Actual browser create/reserve/conflict flows, mobile layout and keyboard focus verified |

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

Firefly Agent Bridge is now a separate public Apache-2.0 project for local development and testing.
The factory's [bridge guide](bridge.md) explains its client integration and the production-use limitations.
