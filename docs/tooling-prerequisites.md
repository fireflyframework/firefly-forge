---
description: Check which tools a factory phase can use, prepare each provider's prerequisites and distinguish working adapters from blocked templates.
---

# Prepare the tools your project needs

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

Start with the task, then enable the tools it needs. A product conversation, an implementation worker and an application health probe have different permissions. Adding a provider to the catalog does not authorize an account, grant a project or prove that a tool call worked.

The native agent registry has fifteen handlers. The factory's own MCP server has 138 operator tools for the authenticated API. Those operator tools manage factory resources; they are not automatically available inside an implementation worker.

## Follow the phase boundary

Use the [lifecycle explorer](agent-lifecycle.md) to inspect the selected repository, phase, participant and actual execution binding.

| Phase | What executes | Tool setup |
| --- | --- | --- |
| Product, design and architecture | A tool-free dialogue produces proposals for owner review. | Select the task's model profile and attach reviewed documents or images. External design tools are not dispatched by this dialogue. |
| Prepare | Factory services retain approved contracts, source and execution settings. | Connect the native SCM application and keep required flycanon retrieval ready. |
| Implement | One coding provider edits the component; optional documentation guidance uses the same worker. | Grant confined source tools and any configured command, browser, search or external-MCP capability. |
| Verify | The factory executes repository checks independently of the worker. | Configure real test, lint, type, build and selected policy-gate commands in the execution profile. |
| Assurance and checkpoint | Optional independent review uses a fresh source copy; the factory retains the verified tree. | Select the reviewer separately. Evidence must refer to the same verified source. |
| Publication | The native SCM integration publishes the branch and review request after an explicit action. | Use a current release permission and the native repository connection. GitHub MCP read access cannot publish. |
| Deploy and observe | Explicit target commands, health probes and metric observations retain their own receipts. | Configure the target and application independently. A Grafana or Sentry template does not enable monitoring. |
| Maintain | An explicitly requested maintenance run follows implementation and verification again. | Keep the incident, source, checks and tool settings associated with that run. |

Use checks and dependencies that match the selected [pyfly](https://fireflyframework.org/frameworks/python/) or [larafly](https://fireflyframework.org/frameworks/php/) profile and the actual product frontend. A tool grant cannot install those dependencies.

The seven editable instruction roles are product manager, technical lead, architect, implementer, QA, security and documentation. Most bind to deterministic factory services. Only implementer and optional documentation guidance bind to the run's coding worker at `implement`; they do not start separate model sessions. [Roles and skills](agent-roles.md) explains the fixed bindings and project overrides.

## Configure the native capabilities

The implementer starts with `list_files`, `read_file`, `write_file`, `search_text`, `edit_lines` and `git_diff`. Commands, browser checks, internet search and external MCP require additional role grants and their actual executors. Saving a role request does not install a missing executor.

| Capability | Prerequisite | Check before starting work |
| --- | --- | --- |
| Files, line edits and diffs | A confined component workspace and the frozen role grant. | Paths, current file hashes and the component diff are readable; credential paths remain excluded. |
| Commands and tests | A frozen [runner profile](sandbox.md), explicit tool grant and the required dependencies. | The exact image or trusted host can execute the project's declared checks. Docker selection never falls back to host execution. |
| Python CPU profiling | Both `profile_python` and `execute_command` grants; a pinned Docker image with Python 3, archive transfer and network `none`, without a pool binding. | Run the [versioned native recipe](agent-tools.md#observe-a-python-workload) and inspect its source-bound command receipts. Timings and text are observations, not QA approval. |
| Python line tracing | Both `trace_python` and `execute_command` grants; the same isolated Python runner as profiling. | Inspect the [best-effort line observation](agent-tools.md#inspect-which-python-lines-ran) and exact command receipt. Counts do not establish complete coverage or QA approval. |
| PHP and larafly profiling | Both `profile_php` and `execute_command` grants; a pinned PHP image, archive transfer and network `none`, without a pool binding. PHP and the application's Composer/vendor dependencies must already be available. | Select the [runtime or Xdebug recipe](agent-tools.md#observe-a-php-or-larafly-workload). Xdebug also requires its preinstalled extension and POSIX file limits. Runner readiness proves none of these interpreter or application prerequisites. |
| Visual QA | A native browser image, matching Playwright binaries, a declared container-loopback application and browser role grants. | Test the actual [journey](browser-qa.md), viewport, theme and approved reference. Screenshot attachment also requires verified model image input. |
| Internet research | An authorized [Brave or Tavily key](search-providers.md), domain restrictions, repository search capability and role grant. | Review the explicit connection-test result and retained citations. Testing consumes that provider's allowance. |
| Knowledge and memory | The managed flycanon services, authenticated tenant/workspaces, embeddings and admitted sources. | Inspect [cited retrieval and memory](knowledge.md). Missing required knowledge blocks generation. |
| External MCP | An executable approved adapter, server credential, schema review and active project/repository/role grant. | Inspect the provider-specific prerequisites below and the actual [invocation receipt](tool-broker.md). |

Flycanon retrieval and verified per-repository memory are factory services. Native `knowledge_lookup` also checks repository-scoped references and eligible local outcomes when its role grant and frozen learning policy permit it. Semantic similarity supplies candidates; source, version, freshness and verified receipts determine reuse. The [learning guide](learning.md) separates this local path from projected-namespace retrieval and future sharing between projects.

The optional native Python CPU, line-tracing and PHP profiling recipes run inside the isolated project runner. PHP runtime metrics need no Xdebug extension; the separately selected Xdebug recipe refuses missing prerequisites before application execution. Neither recipe installs PHP, Composer dependencies or Xdebug. Other debugging and profiling still use configured command arguments and the systematic-debugging instructions. A product image may include its chosen debugger, profiler or scanner, but forge does not supply a dedicated interactive debugger, host profiler report parser or universal security-scanner adapter. Keep diagnostic dependencies, time limits and outputs explicit. Opening a host debugger port is not part of this setup.

## Sign-in and provider authorization are separate

Signing in to forge identifies the operator. Authorizing a provider grants access to that provider's resources. A factory session, SCM application token and external-tool token have separate audiences and permissions; none substitutes for another.

| Connection | Current authorization path |
| --- | --- |
| Factory sign-in | Native pyfly Keycloak OIDC authorization code with S256 PKCE, encrypted PostgreSQL attempts/sessions and single-use browser state. |
| GitLab SCM | Application authorization code with S256 PKCE and refresh. Current grant/state storage is owner-private configuration, separate from the login session tables. |
| Bitbucket SCM | The configured OAuth consumer uses authorization code with refresh or its configured client-credentials flow. It is separate from GitLab's PKCE implementation. |
| GitHub external MCP | The current fixed adapter accepts a server-held token. It does not implement a browser OAuth connection flow. |
| Figma external MCP | Vendor client approval and OAuth are required; neither is implemented by the blocked template. |
| Sentry external MCP | Upstream supports MCP OAuth or its explicit Sentry token mode. The factory has no admitted OAuth/scoped tool adapter. |
| Grafana external MCP | The future managed-server contract must declare its caller authentication and target account separately. No generic OAuth connection or executable adapter is admitted. |
| Native Playwright, local flycanon and search | Browser execution has no vendor login. Flycanon uses its scoped service authentication; Brave/Tavily use their selected API keys. These paths do not acquire an OAuth account by installing a template. |

See [factory identity](identity.md) and [SCM application setup](scm.md) for the implemented flows. Direct coding-provider and Bridge configuration is separate from external MCP authorization.

### Implemented connection OAuth foundation

**Connected accounts** provides the native administrator flow for service policy, client registration, browser consent, current state, refresh and local disconnect. The supported `local-repository-tools` policy requires explicit development settings and fixes the service paths and `repository:read` scope. Five vendor OAuth entries remain at their prerequisite state. Saving an account registration cannot enable an agent tool.

The foundation uses public pyfly OAuth acquisition, refresh, resource/client-authentication and S256 PKCE APIs. Single-use PostgreSQL attempts bind the actor, browser and registration revision. Encrypted server-side grants retain their resource, scope, expiry and family. Exchange and refresh use retained effects and exact request receipts; unknown responses block replay. Current policy and project authority are checked again for resource calls and cached broker receipts.

Follow the [connected-account guide](connected-accounts.md) for the actual wizard, development prerequisites and API/SDK/CLI/MCP contracts. An authorized account still needs the separate MCP link, explicit discovery, exact schema review, repository/tool grant and worker capability. The local adapter admits the four fixed repository-read tools. Vendor approval and scoped adapters remain separate requirements.

## GitHub

**Current boundary:** four repository-read tools are admitted at the fixed `https://api.githubcopilot.com/mcp/readonly` endpoint: `get_file_contents`, `get_commit`, `list_commits` and `list_branches`. Native GitHub App installation is a separate SCM connection.

1. Obtain an authorized credential that can read the selected repository. Check the organization's token policies and any required approval.
2. Save it through the connection wizard or [private key-file CLI flow](external-tools.md#connect-review-and-grant). Credentials stay in the control plane.
3. Discover the server's actual schemas and review their exact digest. Discovery performs a remote request; it does not execute or grant a tool.
4. Grant the registered repository, exact tools and implementation role, then enable the worker's `external_mcp` capability. Inspect a real run receipt before claiming live acceptance.

The adapter checks `owner` and `repo` against the registered source and rejects private file paths. It accepts bounded text results and rejects binary blocks or resource fetching. Upstream's [remote-server documentation](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md) describes the read-only endpoint. A factory-level catalog state still needs the selected project's grant; there is no global permission to every repository.

## Figma

**Current boundary:** a blocked template. forge has no admitted Figma OAuth connection or project-scoped design adapter.

1. Check whether Figma has approved the factory as an allowed client. Support for an existing editor does not approve a new forge client.
2. Obtain the authorized user's consent through a future reviewed OAuth integration. Copying an editor's token into another client is not that integration.
3. Define the product, journey, file, node and reviewed design revision that the adapter may read.
4. Admit the actual schemas and retained design artifacts through a scoped adapter before enabling agent calls. Until then, attach reviewed design files through the product's existing file/reference workflow.

[Figma's installation guide](https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/) requires catalog admission or its new-client waitlist and OAuth. Its [tool reference](https://developers.figma.com/docs/figma-mcp-server/tools-and-prompts/) separates metadata and variables from canvas writes, generative tools and asset downloads. Binary screenshots and temporary asset URLs need a separate bounded artifact adapter; forge's text-only MCP adapter cannot silently fetch them.

## Sentry

**Current boundary:** a blocked template. Saving an account token does not provide a scoped, non-enriched issue adapter.

1. Choose the approved Sentry organization/project and the account permissions required for that scope.
2. Review the authentication mode. Upstream distinguishes an explicit Sentry token using `Sentry-Bearer` from an MCP OAuth access token using `Bearer`.
3. Review the server version and the exact permitted tools. Skill filtering alone does not prove that a selected tool avoids enrichment or embedded model calls.
4. Qualify a metadata-only adapter with current project checks, bounded results and retained receipts before enabling it. Continue using native application health/incident records while this prerequisite remains unmet.

The [official toolkit](https://github.com/getsentry/toolkit) documents the token modes and skill filters. In the reviewed source, `get_issue_details` includes Seer/enrichment paths even when exposed through the inspection skill. forge does not admit that call. A narrow release-metadata adapter is a candidate for further review; it is not implemented by the current template.

## Grafana

**Current boundary:** a blocked template. forge accepts neither an arbitrary managed-server URL nor an executable dashboard adapter.

1. Select an approved authenticated HTTPS MCP deployment and keep its caller credential separate from the Grafana target service account.
2. Restrict the target account to the intended organization, folders and dashboards. Define those targets in a future reviewed grant contract.
3. Restrict the managed server to reviewed metadata tools. Upstream provides `--disable-write` and `--disable-query`; those flags still require tool-by-tool review.
4. Qualify the exact server identity, schemas, current target access and bounded metadata results before admitting calls.

[Grafana's MCP documentation](https://github.com/grafana/mcp-grafana) explains that disabling queries does not block every route that can reach a datasource, including rendering. A first metadata adapter could read one granted dashboard summary. It must reject general API requests, rendering and datasource query tools until those have separate reviewed contracts. Native metrics/health observation does not depend on this MCP template.

## Playwright

**Current boundary:** use the native isolated BrowserRunner. An external stdio server or developer browser profile is not admitted.

1. Build the declared browser image with matching Playwright packages and binaries. See the [official Docker guide](https://playwright.dev/docs/docker).
2. Configure the project's local startup command and literal container-loopback origin in its browser profile.
3. Grant `browser_check` or `browser_journey` to the native implementation role. Keep the journey, expected states, viewport/theme and references explicit.
4. Execute the declared journey, inspect DOM diagnostics and the actual screenshots, and retain the source-bound evidence. Multimodal comparison requires verified model image input.

The supported profile uses Docker network `none`, a fresh context and restricted project-origin requests. Fonts, APIs or databases outside that container will not load. [Upstream Playwright MCP](https://github.com/microsoft/playwright-mcp) is not a security boundary; installing it does not replace the factory's runner controls. Full application-network orchestration needs separate acceptance.

## Understand what the evidence proves

Native search and external MCP use the PostgreSQL tool broker, which records intent and budget before the effect. An unknown remote result requires explicit reconciliation and cannot be blindly replayed. Commands and browser execution use their own effect journals and evidence records. Confined source reads, writes and diffs are guarded, but are not each recorded as individual ToolBroker invocations.

Operator search requests and schema discovery are explicit administrative actions; they are distinct from a run's frozen tool permissions and budget. A returned schema or an enabled connection is not a successful project task. Use the [tool evidence guide](tool-broker.md) and [run activity](operations.md) to inspect actual execution.

## Apply skills without installing hidden tools

The seven community adaptations are instruction text with pinned source revisions, content hashes and license receipts. They cover thinking, TDD, systematic debugging, evidence-first review, frontend design, browser QA and Humanizer. Superpowers and Humanizer adaptations use MIT licenses; the selected Anthropic skills have their own Apache-2.0 receipts. See the [provenance table](agent-roles.md#pinned-community-instructions).

Role and dialogue snapshots retain the selected instructions. Deterministic roles display instruction metadata; implementation and dialogue bindings apply their selected prompt text. Enabling frontend or browser guidance does not install Figma, Playwright binaries or a model account. Executable quality gates are a separate [policy kind](policies.md#define-a-quality-gate) with reviewed command arrays and real passing results.
