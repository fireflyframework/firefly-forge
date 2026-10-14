# firefly forge

![firefly forge: from brief to verified software](assets/banner.svg)

firefly forge is the Firefly software factory. Describe a product's users, journeys and acceptance checks, connect its repositories, then choose a stack and coding provider. Review the implementation, actual checks and retained evidence before publishing a pull request.

The factory combines native [pyfly](https://fireflyframework.org/frameworks/python/) services and controllers, a PostgreSQL execution ledger, Firefly Agentic, Studio and official local coding agents. Build new software, maintain an existing source or run model-free tests. Every run retains its source baseline, configuration, plan, command results and recovery history.

[Documentation](https://fireflyframework.github.io/firefly-forge/) · [Installation](docs/installation.md) · [Studio walkthrough](docs/console-tour.md) · [Architecture](docs/architecture.md) · [Capabilities](docs/capabilities.md)

## Start locally

The managed local stack includes forge, its PostgreSQL ledger, Flycanon, a separate pgvector database, durable knowledge objects and local embeddings. Fresh setup requires knowledge retrieval and enables verified project memory. Use Docker Compose and a dedicated macOS or Linux development environment. Source development also needs Python 3.13+, Git and Node.js 22.12+.

Complete the [installation prerequisites](docs/installation.md#run-the-complete-local-stack), including the separately configured Keycloak client and private environment settings, before starting:

```sh
git clone https://github.com/fireflyframework/firefly-forge.git
cd firefly-forge
docker compose up --build -d
docker compose ps --all
```

Open http://127.0.0.1:8787, sign in and verify the included [knowledge service](docs/knowledge.md). Connect a repository and an authorized coding provider before creating a run. Existing knowledge connections and queued configuration snapshots are preserved. [Factory infrastructure](docs/factory-infrastructure.md) covers server and cluster preparation, storage, backup and recovery.

## Follow a product change

1. **Connect.** Sign in to the factory, verify the selected coding provider, and grant repository access through a native SCM application or register a local Generic Git source. GitHub, GitLab, Bitbucket Cloud, Azure DevOps and Gitea have their own grants and callback settings. See [SCM connections](docs/scm.md) and [provider configuration](docs/configuration.md).
2. **Define.** Map users and journeys to features, observable behavior and failure paths. Choose [pyfly](https://fireflyframework.org/frameworks/python/) or [larafly](https://fireflyframework.org/frameworks/php/) as the project's backend. Review architecture, brand and journey references before saving the product revision. An API-only product still needs a process and acceptance criteria. Start with [your first product](docs/quickstart.md) and [product definition](docs/product-definition.md).
3. **Review and run.** Inspect the brief, saved revision, foundation, executable checks and review policy. Follow the run's diff, command output, artifacts and journal. The [guided Studio tour](docs/console-tour.md) illustrates this path with labeled fixtures, including a failed check and an unresolved intent.

A passing run records its verified source and local checkpoint. Publication rechecks that retained source before creating a remote branch or pull request. Deployment and rollback have separate targets and receipts. An intent without an outcome requires [reconciliation](docs/recovery.md), even when its journal hash chain is valid. Keep failed checks and uncertain effects visible when deciding whether to continue.

## Choose how the factory executes work

The default combined service admits and executes work. A split installation separates API replicas from headless workers: one execution leader runs the jobs while other workers wait as standbys. Processes share the PostgreSQL ledger and retained private storage. Standbys provide takeover; they do not add a distributed pool of active workers. See [runtime roles](docs/runtime-roles.md) and [factory infrastructure](docs/factory-infrastructure.md).

Choose and verify the execution environment before admitting a run. Git workspaces protect the consumer checkout; host commands still run with the worker's operating-system identity. Project users require isolated execution, while host coding and host verification require current administrator authority. [Runner configuration](docs/sandbox.md) and [security](docs/security.md) describe the enforced boundaries.

Administrators can explicitly configure a mutually authenticated local command controller with frozen execution profiles and project quotas. Its isolated containers receive sanitized component source and return bounded command receipts and artifacts. This adapter has local qualification on a trusted Factory host; browser pools, full coding-provider isolation, Kubernetes and multi-host placement remain separate work. Follow the [local pool setup](docs/sandbox.md#administrator-opt-in-local-command-pools) before selecting it.

## Give agents scoped context and tools

Flycanon supplies cited project and framework context. Fresh managed installations require retrieval and enable automatic memory after final verification. Knowledge and memory use a separate `forge-<repository_id>` workspace for each repository; a product spanning several components does not automatically create one combined memory scope. Read [knowledge and memory](docs/knowledge.md) before indexing source or changing scope grants.

Select [agent roles](docs/agent-roles.md), project policies and the configured runner capabilities. Firefly Agentic and bridge execution use bounded native file, search, edit, diff, command and browser tools. Official Codex and Claude Code follow their own CLI permissions; a role tool list is advisory for those providers. Browser checks use a fresh restricted container context, and internet search requires explicit Brave or Tavily provider configuration and an authorized credential. See [native tools](docs/agent-tools.md) and [browser journeys](docs/browser-qa.md).

Project policies can use the public [central policy catalog](https://github.com/fireflyframework/firefly-forge-policies). Teams can connect a separately installed [Firefly Agent Bridge](docs/bridge.md). Studio, the CLI, SDK and the factory's own MCP tools operate the authenticated application contracts; [agent integrations](docs/agent-integrations.md) explains those entry points.

Repository-scoped [learning](docs/learning.md) binds exact questions to verified local outcomes and current applicability. Invalidated or expired evidence cannot supply a cached answer. Explicit flycanon projection retains one submission identity and preserves uncertain outcomes. Automatic projected-namespace retrieval, classified repair circuits and sharing lessons between projects remain open.

## Development

Follow the [source-development setup](docs/installation.md#source-development), then install Chromium with `npx --prefix studio playwright install chromium` before the first browser gate. Set `FORGE_TEST_DATABASE_URL` to an isolated PostgreSQL test database. `make check` runs Python tests, lint and types, Studio checks/tests/browser journeys/build, and the strict documentation build. `make docs-pages-check` also checks every built local link and exercises navigation, search and screenshots under the published repository path.

`make package` creates installable distributions with Studio included. Agent bridge source is maintained separately and is never included in forge distributions. See the [release notes](docs/release-notes.md) for recorded validation and remaining live-acceptance boundaries. A fixture walkthrough demonstrates presentation; it does not establish provider authentication, generated-product correctness or a successful external deployment.

Apache 2.0 for code. Firefly names and generated brand assets retain their separate trademark and asset rights notices.
