# firefly forge

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

![firefly forge: from brief to verified software](assets/banner.svg)

firefly forge is the Firefly software factory. Define a product through its users, journeys and acceptance criteria, connect its repositories, and follow the work from implementation to review, verification and delivery. GitHub, GitLab and Bitbucket conversations are part of that workflow; Studio provides the product workspace and execution history.

The application uses native [pyfly](https://fireflyframework.org/frameworks/python/) services, controllers and PostgreSQL persistence. Coding providers are configurable through Firefly Agentic or the [agent bridge](https://github.com/fireflyframework/firefly-agent-bridge). A run retains its source baseline, plan, command results and recovery history in the execution journal.

[Documentation](https://fireflyframework.github.io/firefly-forge/) · [Installation](docs/installation.md) · [Studio walkthrough](docs/console-tour.md) · [Architecture](docs/architecture.md) · [Releases](https://github.com/fireflyframework/firefly-forge/releases)

## Knowledge and current development

[Koinon](https://github.com/fireflyframework/firefly-koinon) is the unified knowledge platform selected for forge: framework references, project documents, structured knowledge and reviewed memory belong behind its headless interfaces. The [native platform integration](https://github.com/fireflyframework/firefly-forge/pull/73) is still in development. The published managed installer retains its earlier knowledge-service bindings; it does not yet install the completed Koinon composition. Follow the [current knowledge guide](docs/knowledge.md) for the behavior of the version you install. Preserve existing knowledge, credentials and captured run configuration during migration.

Studio includes [factory, project and run observations](docs/factory-dashboard.md), an account panel and [personal project read tokens](docs/personal-api-tokens.md). A [branded local Keycloak sandbox](deploy/keycloak/README.md) prepares organization sign-in without creating default application users or project grants. Immutable source browsing, native object storage and distributed runner execution remain separate integration work. Check the [capability boundaries](docs/capabilities.md) and [release notes](docs/release-notes.md) before selecting an installation for a project. These components have separate acceptance gates. The complete reference-product journey through source control, factory execution, QA and a usable delivered application remains under validation.

## Follow a product journey

A product can contain several journeys, features and integrations, with one repository or several component repositories. API-only products describe their consumers and observable behavior without a frontend. Start with [factory concepts and journeys](docs/factory-journeys.md).

1. Connect the repositories and coding provider. Grant access through the source-control application's permissions and map the relevant accounts to current project access. [SCM setup](docs/scm.md) covers provider grants, signed deliveries and local polling.
2. Define the users, problem and journeys. Map each journey to features, expected behavior, failure paths and acceptance checks. Choose [pyfly](https://fireflyframework.org/frameworks/python/) or [larafly](https://fireflyframework.org/frameworks/php/) for supported backend generation. Review the architecture and, where applicable, the product's brand, design tokens, wireframes and mockups. See [product definition](docs/product-definition.md) and [your first product](docs/quickstart.md).
3. Review the saved product revision and executable checks, then start the run. Follow its plan, source changes, command output and artifacts in Studio. The [walkthrough](docs/console-tour.md) includes successful checks, failures and unresolved operations.
4. Review the implementation against the journeys and recorded QA. Publication rechecks the retained source before creating a remote branch or pull request. [Development checkpoints](docs/development-workflow.md) retain intermediate progress separately from final approval.
5. Continue through the configured deployment and support processes. Deployment, rollback and health checks have their own targets and receipts. [Factory infrastructure](docs/factory-infrastructure.md) explains the available setup and remaining qualification requirements.

[Incremental product changes](docs/product-lifecycle.md) update a saved revision. [Campaigns](docs/project-campaigns.md) group component evidence, reporting waves and milestones. A saved report preserves the observations it contains; it does not refresh source control or approve delivery.

## Work with forge from source control

Ask naturally in a connected issue, for example: `@forge, how is this going?` or `@forge, what should happen if the account owner changes?` The configured communication model uses the ticket's recorded context. When forge asks a confirmed question, answer in the same thread. A pending clarification, its answer and the permission to execute remain separate records.

Source-control requests need current project permissions and the application's repository grant. Read requests, product proposals, approved implementation and publication have different effects. Ambiguous requests require clarification. See [natural ticket conversations](docs/scm.md#ask-about-the-current-ticket), [repository intake](docs/repository-workflow.md) and [agent questions](docs/agent-lifecycle.md#answer-an-agents-question) for the supported paths and provider-specific restrictions.

Studio, the API, SDK, CLI and [factory MCP](docs/agent-integrations.md) use the application's authenticated contracts. The [evidence reader](docs/run-evidence.md) exposes tasks, activity, tool results and journal records. Reading an earlier result does not repeat a command or resolve an unknown outcome.

## Agents, tools and execution

Select the [agent roles](docs/agent-roles.md), project policies, model routing and runner capabilities required by the task. Native tools cover file inspection, search, editing, diffs, commands and browser checks. Internet search requires a configured Brave or Tavily connection. Python and PHP profiling require their diagnostic grants and an isolated Docker runner. See [agent tools](docs/agent-tools.md), [browser QA](docs/browser-qa.md) and the [policy catalog](https://github.com/fireflyframework/firefly-forge-policies).

Official Codex and Claude Code launches follow their own CLI permissions. The separately installed [agent bridge](docs/bridge.md) connects local subscriptions for development and testing. Framework role tool lists do not override an official CLI's permission model.

New work has no fixed deadline by default; a project can choose one. Request waits, ownership heartbeats and execution deadlines are configured separately. The combined service admits and executes work. Split installations provide API replicas and a single active execution leader with standby takeover. Standbys do not provide a distributed pool of active workers. Docker command pools require explicit configuration; Kubernetes and multi-host execution have separate acceptance requirements. Follow [runtime roles](docs/runtime-roles.md), [runners](docs/sandbox.md) and [security](docs/security.md).

Choose an isolated runner for project execution. A Git worktree protects the source checkout, but host commands still use the worker's operating-system identity. Host coding and verification require current administrator authority.

## Verification and recovery

Provider-reported activity and factory verification are recorded separately. Inspect the actual checks, source revision, code diff and artifacts before approving delivery. Documentation screenshots use labeled controlled fixtures; they do not prove that a generated application passed its journeys.

The journal retains intents and observed outcomes. Missing outcomes remain unresolved, including after restart. [Recovery](docs/recovery.md) and [checkpoint continuations](docs/checkpoint-continuations.md) explain supported readback and continuation paths, retained usage and explicit approvals. An intact journal hash chain alone cannot establish that an external operation succeeded.

[Project learning](docs/learning.md) records verified outcomes and their applicability. Framework references retain versions and provenance; [framework refresh](docs/framework-knowledge-refresh.md) provides discovery, immutable corpora and version-specific lookup. Reusing knowledge requires current access and eligible evidence. Changing a model, restarting a worker or reading a receipt does not reset spent usage or authorize an uncertain operation again.

## Run locally

Use a dedicated macOS or Linux development environment. Source development requires Python 3.13+, Git and Node.js 22.12+. Complete the [installation prerequisites](docs/installation.md#run-the-complete-local-stack), including private environment settings and the identity client, before starting the managed stack:

```sh
git clone https://github.com/fireflyframework/firefly-forge.git
cd firefly-forge
docker compose up --build -d
docker compose ps --all
```

Open http://127.0.0.1:8787, sign in and verify the configured knowledge service before admitting work. The Compose stack follows the published installation's compatibility contract described above; review it before expecting the development Koinon integration. Keep database credentials, tokens and private configuration outside Git. Runtime acceptance is performed locally; cluster preparation is documented separately in [factory infrastructure](docs/factory-infrastructure.md).

## Develop and verify

Follow the [source setup](docs/installation.md#source-development). Install Chromium with `npx --prefix studio playwright install chromium`. Set `FORGE_TEST_DATABASE_URL` to an isolated PostgreSQL test database and prepare PHP 8.3+ with `make php-fixture-check`.

- `make check` runs Python tests, lint and types, Studio checks and browser journeys, the frontend build and strict documentation checks.
- `make docs-pages-check` validates the published repository path, local links, navigation, search and documentation captures.
- `make package` builds installable distributions with Studio included.

Model instructions and MCP descriptions live in versioned YAML resources and use the framework template loader and registry. See [prompt maintenance](docs/prompt-templates.md) for variables, digests and regression tests. The agent bridge is maintained in its own repository and is not bundled in forge distributions.

Copyright 2026 Firefly Software Foundation. Code is licensed under [Apache 2.0](LICENSE). Firefly names and generated brand assets retain their separate trademark and asset-rights notices.
