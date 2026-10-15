---
description: Review firefly forge release changes, runtime requirements and the verification boundaries of each published version.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# firefly forge release notes

## 0.2.14

SDK, CLI and MCP clients can inspect the original commands available for a continuation review. Use `ForgeClient.runner_continuation_candidates(run_id)`, `forge runner-continuation-candidates RUN_ID` or `get_runner_continuation_candidates(identifier)`. Each performs one authenticated read and preserves the server's unavailable or empty result. Inspection does not allocate a request, claim a command or dispatch work; the original leader still authorizes a separate explicit continuation. See [runner continuation](sandbox.md#request-continuation-in-studio).

Health and authenticated metrics now share the native supervision service's readiness calculation. `forge_admission_ready` describes local admission, while `forge_execution_ready` describes local execution. A healthy API-only process can accept authorized requests without running local jobs, so Studio no longer tells its operator to restart execution. Degraded health and explicit diagnostics remain visible. Remote worker availability remains unknown. See [runtime roles](runtime-roles.md).

The generic runner queue leaves explicitly continued commands reserved. An expired lease, a refused decision or a change of Factory ownership cannot silently restart or cancel that original command. A durable continuation event also preserves the reservation when its mutable marker is missing. Other queued commands retain their normal admission and capacity checks. This correction does not authorize another continuation or recover an interrupted worker. See [continuation boundaries](sandbox.md#continue-one-queued-command-after-a-controller-restart).

## 0.2.13

Studio can request continuation of an original queued runner command. Open the run's Plan tab, choose the exact command, review its identity and acknowledge the leader's final admission check. The browser saves the original request before submitting it. After a lost response or reload, **Check original request** reads that same receipt. A missing receipt keeps the request unresolved. The local candidate preview does not claim or dispatch work. See the [illustrated continuation guide](sandbox.md#continue-one-queued-command-after-a-controller-restart).

Product projects now include a campaign report with component states, task and check receipts, recorded source, checkpoints and prior attempts. **Load campaigns** and **View report** perform explicit reads. The backend uses one bounded, read-only PostgreSQL snapshot and checks current access to the product and its repositories. It examines the current task attempt's durable intent and receipt even when mutable task state says queued or failed. An unresolved or failed remote operation cannot establish verified delivery. When the snapshot cannot establish bounded evidence, counts remain unavailable. See [campaign reports](project-campaigns.md#read-a-report-in-studio).

The SDK, CLI and factory MCP expose the same campaign report. Its digest identifies the returned snapshot; it does not create persisted report history. SCM freshness is not observed, and milestones and waves remain unconfigured. The existing continuation boundary still covers one controller replacement while the original Factory worker waits. Multi-host placement, repeated controller losses and interrupted-worker recovery require separate work.

Container acceptance also tolerates a process disappearing while its procfs record is read. Permission and other I/O errors still fail the check, and a persistent zombie still fails the bounded reaping test.

## 0.2.12

An authorized operator can explicitly continue one original queued pool command after replacing its controller, while the Factory worker still owns and waits for that command. The persisted request retains the original execution identity, actor, stage, source, admission and WAL fence. API replicas record the request; only the current Factory leader can deliver it. The replacement controller checks current authority and quotas before claiming that exact command. Reads, repeated request identities and startup do not start other queued work.

The SDK, CLI and factory MCP expose admission and exact readback. Save the request identity before submitting it; after a lost response, inspect that same request. An accepted continuation is not evidence of command success or publication. The shared pending-request limit uses the same database admission lock as other worker requests, including concurrent admissions from different runs. See [queued command continuation](sandbox.md#continue-one-queued-command-after-a-controller-restart).

This flow covers one controller replacement before any uncertain create/start effect. It does not recover a second controller loss, revive an interrupted Factory worker, add a command budget, automatically replay unknown work or provide Kubernetes/multi-host execution. Studio continuation controls remain a separate follow-up.

## 0.2.11

Existing-source verification now retains a durable attempt before checking local admission. When an exact server receipt proves that no flycanon request was dispatched, Studio offers **Review and start a new verification**. It rechecks current access and the saved target, preserves the refusal history, and requires confirmation of a fresh request. Unconfirmed source text is replaced by a fingerprint in pending browser storage and the local attempt record.

An unknown, preparing or dispatched attempt blocks a fresh identity for the same learning record. Repeated calls with the original identity only read its outcome. A verified attempt must match its immutable observation event and original scope. Legacy pending requests keep their original recovery flow; a missing receipt or an error message cannot authorize correction. The SDK, CLI and factory MCP expose the same strict attempt and readback contracts. See [source verification](learning.md#verify-an-existing-indexed-source) and [operator integrations](agent-integrations.md#exact-observation-attempts).

Verification jobs run concurrently while the required aggregate still blocks packaging unless Python, Studio, documentation and container acceptance all succeed. Tag publication reuses those checks, then binds its downloaded distributions to the publishing source and rebuilt Studio before release.

## 0.2.10

Studio can verify an existing indexed flycanon source through a source-and-review wizard. The factory checks the source and its citations against the saved projection, then retains the original actor, request and immutable observation event. It does not create a source or trigger ingestion. A lost response keeps the original request; reload and explicit recovery read that exact receipt without repeating the observation. Current project authority and credential checks also apply to receipt readback. See [existing-source verification](learning.md#verify-an-existing-indexed-source).

The SDK, CLI and factory MCP expose the same exact observation receipt. Source identifiers are checked against the current credential registry before any flycanon request. Verification checks the returned source, content and admitted projection again before recording the event. Remote indexing and the local transaction remain separate operations.

The optional `trace_python` tool reports bounded line-hit counts for one admitted Python file in a network-isolated Docker runner. It requires explicit tracing and command grants, verifies the selected source bytes and preserves execution and cleanup receipts. Imported and dynamically compiled code are outside its observation scope. Project code can disable or spoof tracing; these reports are diagnostic evidence, not a coverage or acceptance gate. See [agent tools](agent-tools.md) and [tool prerequisites](tooling-prerequisites.md).

## 0.2.9

Knowledge invalidation retains an exact receipt with its original actor, reason, request and journal event. SDK, CLI and the factory MCP can read that receipt under current project review authority. A missing receipt means the operation has not been observed; it does not permit replay. Legacy records without actor evidence remain explicitly unknown. See [learning invalidation](learning.md).

Studio adds a reason-and-review flow for invalidating saved evidence, with its original request retained in the current tab before submission. A response, record refresh or missing receipt cannot clear an uncertain decision or authorize replay. The exact receipt must match the original context and actor. Confirmation stays available when the active filter excludes the updated record, with an explicit close action. Follow the [illustrated learning journey](learning.md#invalidate-saved-evidence).

The optional `profile_python` agent tool runs bounded stdlib cProfile in an admitted, network-isolated Docker workspace. It requires both command and profiling grants, verifies the selected Python source bytes inside the container and returns a bounded report with execution and cleanup receipts. Profiling does not approve the code, retry an uncertain execution or enable host/pool fallback. See [agent tools](agent-tools.md) and [tool prerequisites](tooling-prerequisites.md).

## 0.2.8

Local command-pool supervisors reap adopted children while a command runs. After the command exits, they terminate and reap remaining descendants before returning a completion receipt, including processes that started a separate session. The original deadline and command exit status remain in force. This prevents a successful result from racing a still-running project writer; it does not undo external effects. See [local command pools](sandbox.md#administrator-opt-in-local-command-pools).

Retained pool artifacts and command output are checked against the current credential registry before being returned. A value newly recognized as a credential blocks the read and requires reconciliation without altering the original artifact or receipt or resubmitting the command. Unregistered secrets still require review.

Runner admission rechecks current execution authority and cancellation after capacity waits and at the host/Docker dispatch boundaries. Revocation or cancellation observed at these checks refuses dispatch. Retained reads use the same admission, and owned-resource cleanup remains available under its existing ownership checks. See [dispatch admission](sandbox.md#recheck-authority-after-waiting-for-a-runner).

The existing-product, feature-change and proposed analytics diagrams read from left to right. Long sequences use phases; narrow screens retain readable labels in keyboard-accessible scroll regions. Existing-product review explicitly precedes frozen assistant context, and admission to flycanon remains optional. Compact navigation also keeps the forge name lowercase and preserves canonical Firefly artwork for the bridge.

These changes retain the trusted local command-pool boundary. Kubernetes, multi-host placement, browser pools and complete coding-provider isolation require separate qualification.

## 0.2.7

**Knowledge > Learning records** lists saved evidence for the selected authorized repository. Operators can inspect its source, verification receipts, dates and saved flycanon projection without submitting or replaying a write. The desktop selector stays reachable while long provenance scrolls; narrow windows use the normal vertical flow. A saved verified state does not establish current reuse eligibility. See [record inspection](learning.md#review-saved-records-in-studio).

After actual failed QA, the captured learning policy can hold the next same-run repair proposal when identical native evidence has reached its limit. The journal retains the decision and original checks. Current authority and unresolved effects are checked before dispatch; retrying the same proposal does not count the verification twice. The limit never relaxes the existing two-round no-progress stop. Verification, intentional RED tests and test-only runs remain available. Cross-run failure classification and sharing between projects remain separate.

Project commands receive a fresh Python cache namespace so normal imports check current source, including same-size edits that could previously reuse stale bytecode and produce incorrect QA results. Host commands clean up their own temporary namespace; container runners use their existing ephemeral filesystem. Project caches remain intact. See [Python verification and its limits](sandbox.md#keep-python-verification-tied-to-current-source).

The runtime uses public pyfly 26.10.6, fireflyframework-agentic 26.10.1 and flycanon SDK 26.10.4. Managed knowledge installs the checksummed flycanon 26.10.4 service in its separate framework environment. Existing admitted runs retain their captured settings. Studio record inspection is read-only; lifecycle writes still require their reviewed operator and reconciliation contracts.

SDK, CLI and factory MCP clients add six reads for global and repository role lifecycle, registered users, eligible project members, identity administration and retained identity operations. Each request uses the existing server permission and redaction boundary. Five existing identity writes also gain client bindings for enable/suspend, provisioning, invitation, reconciliation and owner-stopped recovery. They preserve native administrator checks and durable receipts, without new grants or automatic retries. Four further bindings read public authentication configuration, current authenticated identity and project members, and explicitly set or revoke a registered user’s membership. OAuth configuration adds optional revision comparison in CLI/MCP while preserving no-revision calls. Current owner/admin checks, revocation and the last-owner safeguard remain server-owned. The MCP catalog has 123 tools: 118 earlier contracts are unchanged, one adds only optional `expected_revision`, and four are new. See [operator integrations](agent-integrations.md).

## 0.2.6

Administrators can configure verified learning through **Administration > Learning**. The wizard separates knowledge selection, retrieval limits and review. It preserves saved preferences when learning is disabled and requires a read of saved settings after an uncertain write. New runs capture the revised policy; existing runs retain theirs. See [learning and knowledge-first execution](learning.md).

Verified learning projections can be read from flycanon when a new run explicitly permits it. The record must match the current project, repository, framework and dependencies. Source identity and citation checks precede admission; current local authority and freshness are checked again before new or cached results are returned. An unavailable optional projection leaves ordinary source retrieval and local evidence checks intact. Cross-project promotion and automatic repair circuits remain separate work.

The locked runtime adopts public pyfly 26.10.6 and flycanon SDK 26.10.3. Managed knowledge installs the separately checksummed flycanon 26.10.3 service in its own framework environment. Existing private configuration, corpora and queued revisions are preserved. Canon and Query keep their separately qualified framework event contracts.

## 0.2.5

Native model execution now uses the public fireflyframework-agentic 26.10.1 release. The dependency lock also consumes pyfly 26.10.4 and flycanon SDK 26.10.2. Managed knowledge installs the checksummed flycanon 26.10.2 server, configuration and migration archive in its separate runtime. Existing knowledge configuration and queued revisions retain their original state. The factory keeps its journal-controlled retry and handoff boundaries; upgrading the model framework does not authorize replay of an uncertain external effect. See [coding providers](agents.md) and [agent tools](agent-tools.md).

The product design assistant uses a compact drawer in windows below 480 pixels high. Its recorded response remains readable while the message field, file picker and send action stay reachable. The short-window checks cover light and dark themes, conversation scrolling, keyboard navigation and focus restoration.

Agentic 26.10.1 includes dependency compatibility corrections and namespace-safe PostgreSQL vector updates. Optional Ragas and Chroma dependencies retain known upstream advisories; review the [Agentic security guidance](https://github.com/fireflyframework/fireflyframework-agentic/blob/main/docs/security.md) before enabling those extras. This release does not establish hosted vendor access or production cluster acceptance.

## 0.2.4

Connected accounts now provides an administrator flow for the owned local repository service: admit its fixed policy, register a client and project, complete browser consent, inspect state, request refresh and disconnect locally. The native foundation uses public pyfly 26.10.3 OAuth client and S256 PKCE APIs. Different literal factory/provider hosts protect browser cookies; a same-host policy is rejected before provider contact. See [connected-account setup](connected-accounts.md).

PostgreSQL retains encrypted registration/grant context, stable request identities and token-effect receipts. Unknown exchange or rotating-refresh responses block replay. Historical credential evidence reserves its bounded custody capacity before dispatch. Scoped resource use and cached tool receipts recheck current policy, grant family, expiry and project authority. Schema discovery, exact review, repository/tool grants and the worker capability remain separate admissions. The supported local adapter has four fixed repository-read tools; GitHub, Figma, Sentry, Grafana and external Playwright OAuth remain at their vendor prerequisites.

Native runtime composition uses `FactoryConfiguration`, typed controllers/repositories and injected service collaborators. The [architecture guide](architecture.md) documents knowledge, OAuth resource custody, the ledger and write-ahead boundaries without claiming every domain construction path is container-owned. The Studio walkthrough retains 228 fixture captures tied to its exact frontend and package version. Native OAuth captures keep their own source identities and local qualification scope.

The authoritative [flycanon](https://github.com/fireflyframework/flycanon) repository and the optional [flyquery](https://github.com/fireflyframework/flyquery) repository are now under `fireflyframework`. Forge's SDK and managed server installation use the canonical flycanon release URLs. Required flycanon retrieval and canonical verified memory remain implemented; [structured analytics](structured-knowledge.md) documents a proposed snapshot-based flyquery integration, its assessed safety gaps and admission checks. Forge has no flyquery adapter or managed service.

The native local qualification exercised actual Studio forms, a stock public pyfly authorization/MCP service, token refresh, disconnect, scoped resource calls and retained WAL. MCP linking, discovery, review and project grants used native API requests; resource dispatch used the native in-process broker. This evidence does not establish hosted vendor accounts, production identity federation, paid models, cluster deployment or complete factory acceptance. Release packaging and exact-source CI have their own checks.

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

A local deployment used a real command and an independent verification command. Flycanon acceptance includes real 768-dimensional Ollama embeddings, source and memory retrieval, separate-project isolation and a verified execution journal. The [server scanner correction](https://github.com/fireflyframework/flycanon/pull/76) has been merged and released; strict PII rejection is retained. This does not establish access to every hosted Flycanon workspace.

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
- Token protection across the forge API, with native management/admin/actuator
  surfaces disabled in the packaged pyfly configuration.
- Firefly branding, self-hosted typography, accessible SVG diagrams and
  task-based installation, product, integration and operations guides.

### Verification recorded during preparation

The configured factory gate covers Python tests, lint, formatting and types;
Studio types, lint, formatting, unit tests, browser journeys and production
build; and strict MkDocs compilation.

Studio was also exercised against the real authenticated forge API: connection,
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
