# firefly forge

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

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

Start with [concepts and factory journeys](docs/factory-journeys.md) to distinguish the product contract, repository, component, project, campaign and run. The guide follows monorepo and multiple-repository setup, source-control conversations, execution evidence and product support.

1. **Connect.** Sign in to the factory, verify the selected coding provider, and grant repository access through a native SCM application or register a local Generic Git source. GitHub, GitLab, Bitbucket Cloud, Azure DevOps and Gitea have their own grants and callback settings. See [SCM connections](docs/scm.md) and [provider configuration](docs/configuration.md).
2. **Define.** Map users and journeys to features, observable behavior and failure paths. Choose [pyfly](https://fireflyframework.org/frameworks/python/) or [larafly](https://fireflyframework.org/frameworks/php/) as the project's backend. Review architecture, brand and journey references before saving the product revision. An API-only product still needs a process and acceptance criteria. Start with [your first product](docs/quickstart.md) and [product definition](docs/product-definition.md).
3. **Review and run.** Inspect the brief, saved revision, foundation, executable checks and review policy. Follow the run's diff, command output, artifacts and journal. The [guided Studio tour](docs/console-tour.md) illustrates this path with labeled fixtures, including a failed check and an unresolved intent.

A passing run records its verified source and local checkpoint. Publication rechecks that retained source before creating a remote branch or pull request. Deployment and rollback have separate targets and receipts. An intent without an outcome requires [reconciliation](docs/recovery.md), even when its journal hash chain is valid. Keep failed checks and uncertain effects visible when deciding whether to continue.

Review an [incremental product change](docs/product-lifecycle.md) against its saved revision before adding a feature or integration. Studio retains the original save request and any later draft separately when the outcome is uncertain. Journey references show linked behavior and acceptance criteria; review them against the product's intended experience before implementation.

Read the run's [retained evidence](docs/run-evidence.md) to distinguish coding-provider activity from final factory verification. Studio distinguishes Codex command results and reported MCP activity from Claude tool requests, reported results and provider messages. Known configured-check calls use a human title with their exact tool identity available separately. Complete sanitized event content is available on demand within archive limits; provider reports remain separate from factory verification. SDK, CLI and MCP readers use the same current permissions and exact content descriptor.

## Choose how the factory executes work

The default combined service admits and executes work. A split installation separates API replicas from headless workers: one execution leader runs the jobs while other workers wait as standbys. Processes share the PostgreSQL ledger and retained private storage. Standbys provide takeover; they do not add a distributed pool of active workers. See [runtime roles](docs/runtime-roles.md) and [factory infrastructure](docs/factory-infrastructure.md).

Choose and verify the execution environment before admitting a run. Git workspaces protect the consumer checkout; host commands still run with the worker's operating-system identity. Project users require isolated execution, while host coding and host verification require current administrator authority. [Runner configuration](docs/sandbox.md) and [security](docs/security.md) describe the enforced boundaries.

With the stdlib asyncio event loop on POSIX, trusted Codex and Claude launches preserve an inherited ignored `SIGXFSZ` when the parent has a finite file-size limit. `forge serve` selects this loop explicitly. External ASGI hosts must also use asyncio; a known uvloop host with a finite file-size limit is refused before the provider dispatch intent or child launch. The kernel limit and spent recovery history remain unchanged. See [file-size failure recovery](docs/recovery.md#recover-a-known-failed-official-cli-attempt) for the launch conditions and qualification limits.

After a known, drained fourth official-CLI failure, a native factory administrator can review [one final provider allowance](docs/recovery.md#review-the-final-allowance-in-studio) in the run's Plan tab. The wizard reviews the selected current CLI and source, records the administrator's mitigation attestation and reserves one fifth dispatch. The allowance is spent at reservation, even if its response is lost or dispatch never starts. Legacy recovery keeps its four-attempt limit; the final allowance cannot authorize a sixth dispatch or establish product QA.

A recovery can also use [explicitly granted configured checks](docs/recovery.md#grant-configured-checks-for-the-final-recovery). Review the retained commands, runner and budget, grant access for one identified recovery, then reserve that final attempt separately. The scoped tool cannot submit arbitrary commands; successful interactive checks remain separate from independent final verification. SDK and CLI operators use the same grant and receipt contracts.

In Studio, a run's Plan tab can review an original queued command for explicit continuation after one controller replacement. The original Factory worker must still be waiting. The browser retains the request before submission and reads its exact receipt after a lost response. Follow the [continuation guide](docs/sandbox.md#continue-one-queued-command-after-a-controller-restart) for the admission and recovery limits.

Open a paused run’s **Agent questions** tab to review the agent’s question and context. Choose a suggested answer or use the Markdown editor, review it, then send it under a retained response identity. Readback confirms the recorded answer after reload or a lost response; answering leaves the run paused until you resume it. Current project permissions and the authenticated replying user are checked separately from the recorded actor label. See [operator decisions](docs/agent-lifecycle.md#answer-an-agents-question).

Typed verification requests can let forge run the admitted checks through its authorized runner and record factory-attributed results. New runs default to one bounded verification continuation, separately from their repair budget; product decisions and credentials remain manual. Unknown effects stay held without replay. See [factory verification recovery](docs/recovery.md#inspect-a-factory-verification-handoff) for limits and explicit adoption of older paused requests. Authorized source-control conversations also receive milestone feedback from recorded run facts, with checks and publication reported separately.

A factory administrator can review source-control delegation under **Project access**: choose an account asserted by a signed delivery, an existing Forge user, repository components and allowed requests. The wizard retains one request before saving and confirms its exact receipt. Agent questions display the historical delegation behind a mapped answer, while each new action still needs current permissions and application access. This grants no project membership or personal OAuth login. Follow the [delegation walkthrough](docs/console-tour.md#10-delegate-specific-source-control-requests).

SDK, CLI and MCP clients can inspect those original-command candidates through the same authenticated read. Inspection never creates a continuation request or dispatches work. API-only installations report local admission and execution separately in health and metrics; accepting a request does not establish that a remote worker is ready.

For a local factory without an incoming webhook route, an administrator can enable [GitHub App issue polling](docs/scm.md#use-github-issue-comments-from-a-local-factory). Complete the baseline and separate account consent before posting a new status or answer comment. The mapped user's current permissions still apply, recorded answers retain their provenance, and answering leaves the existing run paused. For an answered clarification, a separate polling continuation consent lets the mapped person request continuation of that same run. Review its account, components and acknowledgment under **Project access**, then use the exact continuation cut in a new GitHub comment. See the [continuation walkthrough](docs/console-tour.md#review-github-continuation-consent). Polling does not create projects or provide full GitFlow.

For a new product, a separately consented signed GitHub conversation can use `/forge intake` to register its first project and functional definition. This requires current native administrator authority and the repository's explicit intake consent. Registration does not start implementation. Follow [repository intake](docs/repository-workflow.md#register-a-first-product-from-github) for the contract; local issue polling supports status, answers and separately consented clarification continuation on existing runs.

Use the [run evidence reader](docs/run-evidence.md) to inspect or export a consistent cut of activity, tasks, WAL and tool results. A separately granted implementation agent can read bounded evidence from its own run. Reading evidence never replays a command or clears an unresolved outcome. Optional [Bridge original-request recovery](docs/bridge.md) also retains the first submission and its pending or terminal observations; it keeps the graph-continuation hold until a supported checkpoint exists.

For a product spanning components, open **Product projects > Campaign reports** to read saved task, check and source evidence. Evidence state is separate from recorded workflow status; unresolved operations prevent verified delivery. The [campaign guide](docs/project-campaigns.md#read-a-report-in-studio) explains unavailable counts, component references and the SDK, CLI and MCP entry points. Studio's [report-and-review wizard](docs/project-campaigns.md#save-and-recover-in-studio) can save the selected report under a retained request identity and read that same historical result later. Saved history retains its original evidence boundary; it does not refresh source control or approve delivery.

After admitting a campaign, use Studio’s waves, milestones and review wizard to save [reporting criteria](docs/project-campaigns.md#define-reporting-milestones-and-waves). Select an exact saved report to inspect component and task-receipt progress against that plan. The SDK, CLI and factory MCP expose the same contracts. These criteria support post-admission reporting; execution-wave orchestration remains separate work.

Administrators can explicitly configure a mutually authenticated local command controller with frozen execution profiles and project quotas. Its isolated containers receive sanitized component source and return bounded command receipts and artifacts. This adapter has local qualification on a trusted Factory host; browser pools, full coding-provider isolation, Kubernetes and multi-host placement remain separate work. Follow the [local pool setup](docs/sandbox.md#administrator-opt-in-local-command-pools) before selecting it.

## Give agents scoped context and tools

flycanon supplies cited project and framework context. Fresh managed installations require retrieval and enable automatic memory after final verification. Knowledge and memory use a separate `forge-<repository_id>` workspace for each repository; a product spanning several components does not automatically create one combined memory scope. Read [knowledge and memory](docs/knowledge.md) before indexing source or changing scope grants.

Optional [structured analytics](docs/structured-knowledge.md) connects a project to an independently provisioned flyquery service. Administrators configure its scope through a three-step wizard; authorized readers explicitly fetch its catalog, table metadata or a known schema object. Install the analytics SDK extra first. This integration does not run SQL, provision the service or replace required flycanon knowledge and memory.

**Knowledge > Learning records** shows saved outcomes, failed-check candidates and projection provenance for an authorized repository. The [learning walkthrough](docs/learning.md#review-saved-records-in-studio) explains record inspection, separate lookup eligibility checks, reviewed invalidation and verification of an existing indexed source, with exact-request recovery. Receipts retain the original actor, reason, request and journal event; Studio, SDK, CLI and MCP readback does not authorize replay when a write response is missing. A frozen same-run proposal cap can hold repeated coding repairs after actual QA; it preserves verification and the existing no-progress guard.

Select [agent roles](docs/agent-roles.md), project policies and the configured runner capabilities. Firefly Agentic and bridge execution use bounded native file, search, edit, diff, command and browser tools. Official Codex and Claude Code follow their own CLI permissions; a role tool list is advisory for those providers. Browser checks use a fresh restricted container context, and internet search requires explicit Brave or Tavily provider configuration and an authorized credential. The optional `profile_python`, `trace_python` and `profile_php` tools require their explicit diagnostic and command grants and an isolated Docker runner. They report bounded cProfile output, line-hit counts or PHP runtime observations with execution receipts. Diagnostic results do not approve the work or authorize replay. See [native tools](docs/agent-tools.md) and [browser journeys](docs/browser-qa.md).

Project policies can use the public [central policy catalog](https://github.com/fireflyframework/firefly-forge-policies). Teams can connect a separately installed [Firefly Agent Bridge](docs/bridge.md). Studio, the CLI, SDK and the factory's own MCP tools operate the authenticated application contracts; [agent integrations](docs/agent-integrations.md) explains those entry points.

Repository-scoped [learning](docs/learning.md) binds exact questions to verified local outcomes and current applicability. Invalidated or expired evidence cannot supply a cached answer. Explicit flycanon projection retains one submission identity and preserves uncertain outcomes. Opt-in retrieval can read the verified projection of an exact eligible local record. Automatic discovery of other projected lessons, cross-run failure classification and sharing lessons between projects remain open.

## Development

Follow the [source-development setup](docs/installation.md#source-development), then install Chromium with `npx --prefix studio playwright install chromium` before the first browser gate. Set `FORGE_TEST_DATABASE_URL` to an isolated PostgreSQL test database and prepare an existing PHP 8.3+ CLI for the recipe fixtures with `make php-fixture-check`. `make check` runs Python tests, lint and types, Studio checks/tests/browser journeys/build, and the strict documentation build. `make docs-pages-check` also checks every built local link and exercises navigation, search and screenshots under the published repository path.

Built-in model instructions and MCP descriptions live in versioned YAML resources and use Firefly Agentic's native template loader and registry. Follow [maintain model instructions](docs/prompt-templates.md) for variable contracts, source digests and golden tests.

`make package` creates installable distributions with Studio included. Agent bridge source is maintained separately and is never included in forge distributions. See the [release notes](docs/release-notes.md) for recorded validation and remaining live-acceptance boundaries. A fixture walkthrough demonstrates presentation; it does not establish provider authentication, generated-product correctness or a successful external deployment.

Apache 2.0 for code. Firefly names and generated brand assets retain their separate trademark and asset rights notices.
