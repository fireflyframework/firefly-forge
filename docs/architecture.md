---
description: Inspect native pyfly services, PostgreSQL records, WAL transitions, agent tools, project authority and factory recovery boundaries.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Understand the factory's internals

Follow a run from its authenticated request to its source checkpoint and delivery receipts. The diagrams describe the implemented factory: native pyfly services, a PostgreSQL ledger, retained private storage and one execution leader.

The factory itself uses [pyfly](https://fireflyframework.org/frameworks/python/). A generated project's selected backend can be pyfly, [larafly](https://fireflyframework.org/frameworks/php/) or another supported profile. Product architecture and cloud recipes describe that project's output; they do not establish the factory's own production availability.

## Components and dependency injection

<figure class="forge-shot forge-architecture-diagram" markdown="1">
<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/architecture-components-narrow.svg">
  <img src="../diagrams/architecture-components.svg" alt="The same services support a combined process and split admission/execution. Read the arrows as responsibility boundaries; coding providers, Git and knowledge services produce effects outside a database transaction.">
</picture>

[Open full diagram](diagrams/architecture-components.svg) · [Portrait layout](diagrams/architecture-components-narrow.svg)

<figcaption>The same services support a combined process and split admission/execution. Read the arrows as responsibility boundaries; coding providers, Git and knowledge services produce effects outside a database transaction.</figcaption>
</figure>

`create_runtime()` constructs the native `PyFlyApplication`, registers `ForgeBeans` and the typed controllers/repositories, then lets the container inject their dependencies. `create_application()` adds the Starlette adapter, request limits, trusted-host middleware and Studio's static build. The worker uses the runtime without an HTTP listener.

| Layer | Concrete implementation | Contract |
| --- | --- | --- |
| Authentication | `OperatorAuth`, `IdentityService`, `AccessRoutes` | Validate identity, protect cookie writes and authorize the requested resource. |
| HTTP | Native `@rest_controller` and typed mappings | Bind actual request models and expose authenticated OpenAPI. |
| Admission | `RunService`, `Store.create_run`, `ProjectCampaigns.launch` | Freeze selected inputs and retain accepted work. |
| Execution | `Engine`, `ExecutionRequests`, `DeploymentManager` | Claim work, check authority and retain observed effects. |
| Context | `RoleRegistry`, `TaskRouting`, `KnowledgeManager`, `SearchTools` | Select instructions, model configuration and scoped cited evidence. |
| Persistence | Native typed `@repository` beans and `SessionProvider.unit()` | Commit records, enforce keys and serialize updates. |

Lifecycle ordering initializes `Store` before topology and execution consumers. The engine acquires ownership before recovering retained work. Worker supervision watches coding, delivery, deployment, campaign, channel and application-observation consumers. A stopped required consumer degrades execution readiness; shutdown closes dispatch and drains effects before releasing ownership. See [runtime roles](runtime-roles.md).

Source: [`api.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/api.py) (`ForgeBeans`, `create_runtime`, `create_application`), [`worker_runtime.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/worker_runtime.py) (`WorkerSupervision`).

## PostgreSQL records and transaction boundaries

<figure class="forge-shot forge-architecture-diagram" markdown="1">
<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/architecture-ledger-narrow.svg">
  <img src="../diagrams/architecture-ledger.svg" alt="This is a logical map of retained entities, not an exhaustive schema. Each card names its actual reference or unique key; separate cards do not imply a foreign key between unrelated ledgers.">
</picture>

[Open full diagram](diagrams/architecture-ledger.svg) · [Portrait layout](diagrams/architecture-ledger-narrow.svg)

<figcaption>This is a logical map of retained entities, not an exhaustive schema. Each card names its actual reference or unique key; separate cards do not imply a foreign key between unrelated ledgers.</figcaption>
</figure>

`Store._unit()` uses the injected session provider, sets the selected schema and enables synchronous commit for that database unit. Repository methods use its active session. Row locks protect run/task transitions; request-specific advisory transaction locks serialize campaign or delivery admission. External Git, model, container and knowledge operations happen outside PostgreSQL's atomic commit boundary.

| Record group | Stored relationship or key | What remains separate |
| --- | --- | --- |
| Repository and run | `forge_runs.repository_id` → `forge_repositories.id` | Private credential revisions and the retained Git root are files. |
| Plan and task | Plan `(run_id, revision)`; task `(run_id, task_id)` and `plan_id` | The plan describes work; mutable task state records its attempt and evidence. |
| Journal | Unique `(run_id, sequence)` and `(run_id, idempotency_key, kind)` | A hash-chain record does not make an external effect atomic. |
| Campaign and child | Unique `(project_id, request_id)` and `(campaign_id, component_id, attempt)`; unique child `run_id` | Component source roots and dependency references remain distinct. |
| Delivery requests | Request state plus its own ordered event history | HTTP acceptance precedes the worker's publication/cancellation receipt. |
| Deployment | Run reference, target, status and receipts | Target commands and remote application health need their own evidence. |

PostgreSQL rejects updates to immutable plan/journal history and retained campaign admissions. A completion receipt, task evidence and checkpoint update share one unit; a failed receipt write rolls that unit back. The prior external effect may already have happened, so database rollback alone cannot authorize replay.

Back up the ledger, private revisions, source workspaces, artifacts and retained workspace backups together. See [operations](operations.md) and [factory infrastructure](factory-infrastructure.md).

Source: [`store.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/store.py) (`_unit`), [`persistence.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/persistence.py), [`project_campaign_persistence.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/project_campaign_persistence.py), [`migrations.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/migrations.py). Regression evidence: `test_receipt_failure_rolls_back_task_and_checkpoint` and `test_postgresql_refuses_updates_to_immutable_plan_and_journal` in `tests/test_journal.py`.

## Admission, execution and the verified tree

<details class="forge-gallery" markdown="1">
<summary>Inspect the admission and execution sequence</summary>

<figure class="forge-shot forge-architecture-diagram" markdown="1">
<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/architecture-admission-narrow.svg">
  <img src="../diagrams/architecture-admission.svg" alt="The queued response and final source evidence are different events. The leader's self-messages include provider execution and independent checks; a test-only run omits implementation and coding-model selection.">
</picture>

[Open full diagram](diagrams/architecture-admission.svg) · [Portrait layout](diagrams/architecture-admission-narrow.svg)

<figcaption>The queued response and final source evidence are different events. The leader's self-messages include provider execution and independent checks; a test-only run omits implementation and coding-model selection.</figcaption>
</figure>

</details>

`POST /api/v1/runs` captures the selected provider/model revisions, product and component inputs, policies, role instructions, runner capabilities and knowledge/search revisions. Client-supplied product approval must match an existing immutable product revision. A project user's admission requires current project authority and an isolated runner; direct host coding CLIs require administrator authority.

The engine dispatches queued work under a global concurrency budget and one active coding run per repository. It prepares an isolated Git root and records its authorized origin and baseline. The selected coding provider receives the frozen handoff. Factory verification executes commands independently; failed checks return to that provider within the frozen repair budget.

| Boundary | Accepted evidence | Failure behavior |
| --- | --- | --- |
| Admission | Retained queued run and selected snapshots | Invalid authority, conflicting inputs or unavailable capabilities refuse admission. |
| Implementation | Actual source writes and a returned provider report | A report without required source writes is failed generation. |
| Verification | Executable commands, real exit codes and the verified source tree | Failed checks remain recorded; a source change invalidates that attempt. |
| Requested review | Approved independent verdict matching the source tree | A missing or mismatched review cannot support delivery. |
| Checkpoint | Actual local commit and matching verified tree | Changed source requires fresh evidence before publication or deployment. |

Inspect `GET /api/v1/runs/{id}`, `/plan` and `/journal`; [the HTTP guide](api.md#follow-evidence-and-errors) explains the returned evidence. The [Studio tour](console-tour.md#4-inspect-the-run-before-continuing) shows readable code, commands and journal fields using explicitly labeled fixtures.

Source: [`store.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/store.py) (`create_run`, `claim`), [`engine.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/engine.py) (`dispatch_queued`, `execute_run`, `_checkpoint_commit`, `validated_workspace`).

## Write-ahead intents, receipts and uncertain effects

<details class="forge-gallery" markdown="1">
<summary>Inspect effect ordering and the reconciliation path</summary>

<figure class="forge-shot forge-architecture-diagram" markdown="1">
<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/architecture-wal-narrow.svg">
  <img src="../diagrams/architecture-wal.svg" alt="A missing receipt holds continuation until actual evidence settles the effect. Reconciliation records what was observed; it does not retroactively make a failed command successful.">
</picture>

[Open full diagram](diagrams/architecture-wal.svg) · [Portrait layout](diagrams/architecture-wal-narrow.svg)

<figcaption>A missing receipt holds continuation until actual evidence settles the effect. Reconciliation records what was observed; it does not retroactively make a failed command successful.</figcaption>
</figure>

</details>

`Journal.ensure_plan()` creates prepare, implement, verify and checkpoint tasks; test-only plans omit implementation, and requested independent review adds a review task. Later publication, deployment, rollback and memory effects receive their own planned tasks. The plan digest covers its definition and frozen references.

`Journal.intent()` derives a stable UUID5 key from the run, task and attempt. It locks the run before marking the task active and appending the intent. Reusing the key with an existing completion returns a completed disposition; reusing an unfinished intent requires reconciliation. `Journal.receipt()` accepts an active intent and retains the actual result alongside task evidence and the checkpoint.

Each record retains `sequence`, `task_id`, `idempotency_key`, `kind`, payload, timestamp, `previous_hash` and SHA-256 `digest`. `verify_chain()` checks contiguous sequence, the prior digest and canonical JSON content. The first previous hash is 64 zeroes. This detects inconsistent retained history; a database owner who can replace the entire chain can recompute its hashes. No independent notarization is claimed.

An unknown remote publication, deployment or memory write cannot be generically retried. Observe the actual service result and its identity first. Local repair requires recorded failed verification; late errors cannot overwrite a completed effect. Resume retains the run and available session, handoff freezes a new configured provider snapshot, and retry creates a separate attempt. Campaign children must also pass their current campaign control boundary. See [recovery](recovery.md).

Source: [`journal.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/journal.py) (`ensure_plan`, `intent`, `receipt`, `verify_chain`, `reconcile`, `resume`). Regression evidence: concurrent intent, restart reconciliation, late-error rejection and local-repair tests in `tests/test_journal.py`.

## Deterministic campaigns across components

<details class="forge-gallery" markdown="1">
<summary>Inspect atomic admission and dependency release</summary>

<figure class="forge-shot forge-architecture-diagram" markdown="1">
<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/architecture-campaign-narrow.svg">
  <img src="../diagrams/architecture-campaign.svg" alt="A campaign coordinates child runs with a frozen graph and retained evidence. It does not start a model-based manager or automatically assemble and test the product.">
</picture>

[Open full diagram](diagrams/architecture-campaign.svg) · [Portrait layout](diagrams/architecture-campaign-narrow.svg)

<figcaption>A campaign coordinates child runs with a frozen graph and retained evidence. It does not start a model-based manager or automatically assemble and test the product.</figcaption>
</figure>

</details>

`POST /api/v1/projects/{project_id}/campaigns` takes a fresh `request_id`, a brief, selected coding/review configuration and a dependency gate. Repeating an identical request returns its campaign; reusing the identity for different content is rejected. Selecting components includes their upstream dependencies.

Source commits and runner capabilities are resolved before admission locks. Inside one database unit, launch locks its request, project and repository rows, rechecks the project/configuration vector, and retains all selected child admissions and source bases. A settings change refuses admission instead of mixing revisions. Child runs start as `campaign_blocked`; the coordinator releases ready components into the normal queue.

| Gate or control | Required evidence | What it cannot establish |
| --- | --- | --- |
| `verified` | Nonempty passing executable checks; matching tree/checkpoint journal receipts; matching approved review when requested | A successful status alone is insufficient. |
| `published` | The verified gate plus an actual matching branch/PR publication receipt | Publication does not prove merge, deployment or product integration. |
| Upstream context | Run/source/tree/branch/artifact/publication references | References do not merge upstream code into the downstream repository. |
| Unknown effect | Reconciliation of the child intent or pending control | The coordinator stops release rather than replaying it. |
| Cancel, resume, retry | Explicit campaign request identity and eligible current attempts | Individual run controls cannot bypass the campaign's cancelled or frozen admission state. |

Retry preserves the graph, admitted source base and configuration, and retains earlier attempts. Each component allows three explicit retries. An active downstream component must stop before retrying its upstream dependency. Configure whole-product integration verification separately. [Campaign operations](project-campaigns.md) lists the exact action endpoints and control semantics.

Source: [`project_campaigns.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/project_campaigns.py) (`launch`, `_gate`, `_release`, `cancel`, `resume`, `retry`), [`store.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/store.py) (`assert_campaign_current`). Regression evidence: `tests/test_project_campaigns.py` and `tests/test_campaign_boundaries.py`.

## Identity, SCM grants and delivery authority

<details class="forge-gallery" markdown="1">
<summary>Inspect current authority and repository grants</summary>

<figure class="forge-shot forge-architecture-diagram" markdown="1">
<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/architecture-authority-narrow.svg">
  <img src="../diagrams/architecture-authority.svg" alt="Admission records who requested work. Execution checks whether that identity and repository grant still permit the effect. Secrets stay in owner-protected server configuration and ephemeral Git environments.">
</picture>

[Open full diagram](diagrams/architecture-authority.svg) · [Portrait layout](diagrams/architecture-authority-narrow.svg)

<figcaption>Admission records who requested work. Execution checks whether that identity and repository grant still permit the effect. Secrets stay in owner-protected server configuration and ephemeral Git environments.</figcaption>
</figure>

</details>

Hosted API clients use a validated OIDC token for the configured audience. Studio uses durable server sessions and protected cookie writes. Explicit local bootstrap is limited to its configured loopback mode. Typed inputs, an acknowledgement checkbox or a retained administrator flag cannot replace current server authorization.

`ProjectAccess.authorize_actor()` rechecks the retained actor against current identity and membership. OIDC host administrator authority needs a current, unexpired API-role grant matching the admitted audience. Revoked membership or expired administrator access is honored before effects; admission does not renew sign-in.

`SCMConnections` retains the application's identity separately from its live permissions. Clone, reference and publication operations check the selected repository against current grants. GitHub App tokens are narrowed to the repository; OAuth refresh stays server-side. Git receives short-lived credentials through a scoped environment. Publication also verifies the retained Git root, authorized origin, source tree and checkpoint before recording the actual PR URL.

API replicas retain publication/cancellation requests and deployment operations; the leader dispatches them. HTTP acceptance precedes the effect receipt. See [identity](identity.md), [SCM connections](scm.md) and [worker delivery requests](recovery.md#reconcile-worker-delivery-requests).

Source: [`api.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/api.py) (`OperatorAuth`), [`project_access.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/project_access.py), [`scm_connections.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/scm_connections.py), [`execution_requests.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/execution_requests.py).

## Agent roles, native tools and cited context

<details class="forge-gallery" markdown="1">
<summary>Inspect instructions, tool authority and knowledge scope</summary>

<figure class="forge-shot forge-architecture-diagram" markdown="1">
<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/architecture-agent-context-narrow.svg">
  <img src="../diagrams/architecture-agent-context.svg" alt="These are separate contracts, not six independent agents. The implementation provider receives instructions and untrusted cited context; configured executors enforce native tool access.">
</picture>

[Open full diagram](diagrams/architecture-agent-context.svg) · [Portrait layout](diagrams/architecture-agent-context-narrow.svg)

<figcaption>These are separate contracts, not six independent agents. The implementation provider receives instructions and untrusted cited context; configured executors enforce native tool access.</figcaption>
</figure>

</details>

The seven instruction roles bind to implemented stages. Product manager, architect and technical lead preparation are deterministic. The implementer uses the run's selected coding provider. QA runs actual checks, security enforces mandatory factory boundaries, and documentation guidance uses the implementation session. An optional assurance reviewer uses its separately selected provider. Product dialogue participants and task-specific model profiles have their own contracts; instruction roles do not create separate model sessions.

Pinned community skills are instruction adaptations with retained source, hash and license provenance. They do not download or run upstream helpers or authorize publication. Role and policy changes affect later admissions; existing runs retain their selected instruction revision and digest. [Roles and skills](agent-roles.md) and [lifecycle phases](agent-lifecycle.md) explain the projections and feedback handoffs.

| Runtime | Tool authority | Evidence boundary |
| --- | --- | --- |
| Native Agentic / bridge inference | Requested tools intersected with actual configured capabilities | File scope, credentials and executor grants are enforced by Python. |
| Official Codex / Claude Code | The official CLI's native tools and permission controls | A Python role tool list is advisory; it is not the CLI sandbox. |
| Native browser QA | Fresh Docker context, declared local origin and bounded actions | Layout diagnostics and screenshots require actual visual/interaction review. |
| Internet search | Explicit Brave or Tavily selection, retained credential revision and domain restrictions | Bounded cited results are untrusted references; no key goes to the model. |
| Factory MCP | 96 authenticated operator tools in `mcp_server.py` | These API tools are distinct from a coding model's workspace tools. |

Flycanon retrieves `forge-<repository_id>` project context and shared framework references. Required retrieval failure prevents coding continuation. The retained citations/context digest support recovery. A ProductProject does not automatically create an aggregated memory workspace across its repositories.

Automatic memory follows final verification and validated source, retaining source/version provenance in the repository scope. Explicit indexing admits selected verified files. An uncertain memory write must be reconciled in Flycanon before retry. The managed installation includes Flycanon's separate pgvector database, object storage and local embeddings; [knowledge](knowledge.md) and [search providers](search-providers.md) describe configuration and actual readiness checks.

Source: [`agent_roles.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/agent_roles.py), [`agentic_runtime.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/agentic_runtime.py), [`workspace_tools.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/workspace_tools.py), [`knowledge.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/knowledge.py), [`search_tools.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/search_tools.py), [`mcp_server.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/mcp_server.py).

## Learning and structured knowledge decisions

The [learning architecture](learning.md) defines exact reuse eligibility, evidence-backed local procedure outcomes, observed failed checks and typed question/tool/review associations. Its diagrams separate PostgreSQL decision authority from flycanon retrieval and show where native fireflyframework-agentic workers provide proposals. The initial implementation adds scoped PostgreSQL records, native knowledge-first tools and explicit one-shot flycanon projection. Automatic retrieval from projected learning namespaces, classified negative repair controls and sharing between projects remain open; their contracts do not establish runtime acceptance.

## Runner ownership, collection and takeover

<details class="forge-gallery" markdown="1">
<summary>Inspect fences, executor results and takeover</summary>

<figure class="forge-shot forge-architecture-diagram" markdown="1">
<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/architecture-runners-narrow.svg">
  <img src="../diagrams/architecture-runners.svg" alt="The API can retain work while a worker is unavailable. A standby begins recovery only after both fences are available, and it does not repeat an effect whose outcome remains unknown.">
</picture>

[Open full diagram](diagrams/architecture-runners.svg) · [Portrait layout](diagrams/architecture-runners-narrow.svg)

<figcaption>The API can retain work while a worker is unavailable. A standby begins recovery only after both fences are available, and it does not repeat an effect whose outcome remains unknown.</figcaption>
</figure>

</details>

The leader holds a private-storage lock and a PostgreSQL schema advisory lease. The topology also binds the resolved storage path, owner and protected storage-identity marker. Split replicas need the same durable storage at the retained paths. A copied marker or same-host Compose volume does not establish working multi-host storage.

The legacy `RunnerBackends` selects host or Docker and rejects a future `pool_binding` rather than falling back to either transport. Administrator opt-in command pools use the separate Factory worker and controller contract described in [execution runners](sandbox.md#administrator-opt-in-local-command-pools). Host commands run as the worker identity. Docker uses the explicit context, frozen image/resource/network policy and confined workspace; browser execution has its separate no-network local-origin profile.

Docker result collection prepares a complete replacement tree, retains the returned archive, and uses an atomic directory exchange. It checks both directory identities and preserves local Git/credential metadata. Failed or uncertain collection retains its archive, original tree or containers for reconciliation; repeating the invocation does not rerun the command. Source replacement is a filesystem effect with its own receipt, not a PostgreSQL transaction. See [runners and source collection](sandbox.md).

Lease loss closes dispatch and stops consumers. If old effects cannot drain, the storage fence remains held even when the database connection closes. A standby waits until the old process actually exits or all retained effects stop. Timeout alone does not prove safe takeover. Standbys add availability, not active capacity; API health reports remote worker availability as unknown.

| Surface | Current implementation | Separate boundary |
| --- | --- | --- |
| Split runtime | API replicas, one leader, waiting workers and administrator opt-in local command pools | Complete provider/browser placement, Kubernetes and shared multi-host storage require separate qualification. |
| Host, Docker and command-pool executors | Frozen supported transports and retained invocation evidence | A legacy `pool_binding` cannot activate a command pool or select another transport. |
| Native tool registry | Built-in workspace/browser/search tools and reviewed text-only GitHub MCP tools | External tools need exact schema review and current project grants; other catalog templates remain prerequisites. |
| Product cloud recipes | Generate repository-bound infrastructure/deployment inputs | They do not demonstrate the factory's production availability or a live target. |
| External design services | Reviewed retained design/reference inputs | No general Figma account access or connector entitlement is established. |

Source: [`runtime_roles.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/runtime_roles.py), [`engine.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/engine.py) (`start`, `prepare_shutdown`, `stop`), [`runner_backends.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/runner_backends.py), [`sandbox.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/sandbox.py), [`git_workspace.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/git_workspace.py). [Runtime roles](runtime-roles.md) describes readiness and shutdown contracts.

## Read the Studio floor as a projection

Stage labels and the floor scene follow returned plans and jobs. Failed or paused work stays at its recorded stage; hidden, stale, offscreen or reduced-motion scenes suspend motion. Motion and timestamps do not establish completed tasks or percentage progress. Inspect the actual [run evidence](console-tour.md#4-inspect-the-run-before-continuing) and [recovery controls](recovery.md) before accepting or continuing work.
