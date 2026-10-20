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

`create_runtime()` constructs the native `PyFlyApplication`, registers `FactoryConfiguration` and the typed controllers/repositories, then resolves their registered dependencies through the container. Domain services also retain explicit collaborators and test seams; this does not establish that every construction path is container-owned. `create_application()` adds the Starlette adapter, request limits, trusted-host middleware and Studio's static build. The worker uses the runtime without an HTTP listener.

| Layer | Concrete implementation | Contract |
| --- | --- | --- |
| Authentication | `OperatorAuth`, `IdentityService`, `AccessRoutes` | Validate identity, protect cookie writes and authorize the requested resource. |
| HTTP | Native `@rest_controller` and typed mappings | Bind actual request models and expose authenticated OpenAPI. |
| Admission | `RunService`, `Store.create_run`, `ProjectCampaigns.launch` | Freeze selected inputs and retain accepted work. |
| Execution | `Engine`, `ExecutionRequests`, `DeploymentManager` | Claim work, check authority and retain observed effects. |
| Context | `RoleRegistry`, `TaskRouting`, `KnowledgeManager`, `SearchTools` | Select instructions, model configuration and scoped cited evidence. |
| Persistence | Native typed `@repository` beans and `SessionProvider.unit()` | Commit records, enforce keys and serialize updates. |

Lifecycle ordering initializes `Store` before topology and execution consumers. The engine acquires ownership before recovering retained work. Worker supervision watches coding, delivery, deployment, campaign, channel and application-observation consumers. A stopped required consumer degrades execution readiness; shutdown closes dispatch and drains effects before releasing ownership. See [runtime roles](runtime-roles.md).

### Native knowledge services

`FactoryConfiguration.knowledge_connection()` injects the private `ConfigurationStore` into `KnowledgeConnection`. `knowledge_manager()` injects `Store`, `Engine`, `Journal`, that connection and `KnowledgeFirst`; it connects the same manager to execution and native knowledge-first tools. `KnowledgeController` uses typed pyfly mappings for configuration, authentication tests, scoped search, source indexing and verified memory. `KnowledgeUploads` has its own injected repository for retained source-upload evidence.

The connection owns authenticated SDK transport and TLS trust. The manager chooses the frozen connection revision, confines repository scope and handles source/canonical receipts. `KnowledgeFirst` combines those cited references with eligible PostgreSQL procedure outcomes before considering configured web search. It cannot treat a search hit as an approved procedure.

Flycanon's server runs in a separate environment, with its own migration and ingestion worker. Installing the Python SDK into forge supplies the client; the managed server image installs the released server independently. The optional flyquery catalog integration has separate connection and project-read services; it does not replace required knowledge or start an analytics worker.

Source: [`api.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/api.py) (`FactoryConfiguration`, `create_runtime`, `create_application`), [`worker_runtime.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/worker_runtime.py) (`WorkerSupervision`).

### Observed provider activity

`activity_models.py` projects known Codex JSON into small typed summaries; `activity_claude.py` recognizes Claude stream-json content blocks and result messages. Both describe observed output. A requested tool, reported tool result and final provider report remain distinct from the factory's WAL receipts and verification. Claude format recognition supplies no authenticated provider attribution, cross-event tool correlation or inferred exit code.

`Store.event()` retains the sanitized message separately from that bounded projection. Studio renders supported summaries, leaves plain logs readable and offers an explicit verified reader for complete content. Unsupported structured previews receive a generic card. The optional Claude summary fields preserve the existing descriptor version and byte/hash binding; the archive writer, current-authority checks, credential fences and D04 export limits are shared. See [the activity contract](agent-integrations.md#read-complete-retained-provider-activity) and [the operator walkthrough](run-evidence.md#read-complete-provider-activity).

### Connection OAuth and resource custody

`FactoryConfiguration` registers `OAuthProviderPolicies`, `ConnectionOAuth`, `OAuthResources` and the external MCP registry as separate native beans. Policy admission fixes the issuer, endpoints, resource and scope. The connection service binds client registration and browser attempts to the current actor and immutable revision; it uses public pyfly OAuth client and PKCE APIs. PostgreSQL stores encrypted credentials, grant families, effects and append-only events.

`OAuthResources` signs a project/resource binding and rechecks current policy, registration, grant family/revision, expiry and project authority before dispatch. A schema review and exact repository/tool grant remain mandatory. Current authorization also protects cached broker receipts: a completed result cannot bypass a withdrawn policy or revoked grant.

<figure class="forge-shot forge-architecture-diagram" markdown="1">
<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/architecture-oauth-narrow.svg">
  <img src="../diagrams/architecture-oauth.svg" alt="Admit a fixed service, retain client registration, authorize in the browser, record token effects, bind the exact project resource, and review schemas and tool grants before broker dispatch.">
</picture>

[Open full diagram](diagrams/architecture-oauth.svg) · [Portrait layout](diagrams/architecture-oauth-narrow.svg)

<figcaption>Consent retains an account grant. Current resource custody, exact schemas and project tool admission remain separate checks.</figcaption>
</figure>

The implemented local service is administrator-only and requires explicit development flags, literal loopback addresses and different factory/provider hosts. The native browser qualification used IPv4 for forge and IPv6 for the owned stock pyfly service. GitHub, Figma, Sentry, Grafana and external Playwright OAuth retain their provider prerequisites. [Connected accounts](connected-accounts.md) explains the actual wizard, typed API and separate MCP admission steps.

Source: [`oauth_provider_policies.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/oauth_provider_policies.py), [`connection_oauth.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/connection_oauth.py), [`oauth_resources.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/oauth_resources.py), [`mcp_registry.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/mcp_registry.py).

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

## Factory-owned verification handoffs

A coding agent may need checks that its own sandbox cannot perform, such as connecting to the project's dedicated test database. It returns a typed `verification` request. forge executes the run's already approved commands in the configured runner and sends the recorded results back to the coding agent. A product decision, missing credential or unknown side effect is not permission to invent a command or expand access.

<div class="forge-flow forge-flow--dependencies" role="region" aria-label="Automatic verification handoff diagram" aria-describedby="automatic-verification-flow-guide" tabindex="0" markdown="1">

```mermaid
---
config:
  flowchart:
    wrappingWidth: 96
    rankSpacing: 32
---
flowchart LR
    Q["Agent<br/>check request"] --> I["Journal<br/>intent"]
    I --> R["Runner<br/>approved checks"]
    R --> K{"Known<br/>outcomes?"}
    K -->|Yes| A["Journal<br/>factory answer"]
    A --> C["Engine<br/>continue"]
    K -->|No or revoked| H["Hold for<br/>review"]
```

</div>

<p class="forge-flow-caption" id="automatic-verification-flow-guide">Read from left to right. The journal records intent before execution and results before continuation. On a narrow screen, scroll horizontally or focus the diagram and use the arrow keys.</p>

`VerificationHandoffs` is a native pyfly service registered through the runtime's configuration bean. It uses the same `Store`, `Journal`, `Engine` and `SandboxExecutor` as ordinary execution. The dispatcher applies the existing concurrency and repository ownership limits; a paused verification does not create a second coding run.

The immutable handoff binds the run, pending request, source manifest, approved commands, current policy, provider configuration, implementation attempt and command ordinals. Authority, cancellation, campaign controls and those bindings are checked again before each effect and before continuation. Command output is untrusted data. Its bounded answer excerpt does not replace the original runner receipts.

| Recorded state | Meaning | User action |
| --- | --- | --- |
| Pending or running | forge owns the approved verification step. | Follow progress; no manual answer is needed. |
| Completed | Every command has a known exit code, including any failures. | Inspect the results while the authorized continuation proceeds. |
| Held or refused | Admission failed, authority changed or an outcome cannot be established. | Review the run before authorizing another action. |

The tool policy enables one automatic continuation by default; administrators can disable it or set a finite limit from one to three. Each admitted run captures that policy. The continuation allowance is separate from the repair budget: it does not reset prior implementation rounds or create unlimited repair attempts. The coding attempt limit remains bounded. Failed commands remain failed evidence even when they provide enough information for coding to continue.

A completed handoff writes a version-4 factory-attributed `worker_response`, followed by its automatic resume receipt. Studio presents that response as work handled by forge, rather than attributing it to a person. The same run and workspace continue. A restart can reuse known command receipts, but an unfinished command or provider admission cannot be replayed blindly.

Older runs without an automatic-verification grant stay manual until an authenticated native operator explicitly adopts the exact pending request, retained source and current policy/provider cut. The typed adoption API provides candidate inspection, one retained request identity and exact readback; it does not execute commands inside the HTTP request. See [the API guide](api.md) for the contract.

Progress in the [repository workflow](repository-workflow.md) comes from durable run states. The bot reports starts, checks, questions and delivery outcomes through centralized Agentic templates. A confirmed GitHub comment and a successful product delivery remain separate records.

Source: [`verification_handoffs.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/verification_handoffs.py), [`verification_models.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/verification_models.py), [`verification_controllers.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/verification_controllers.py), [`journal.py`](https://github.com/fireflyframework/firefly-forge/blob/main/src/firefly_forge/journal.py).

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

Known official-CLI process failures have a separate same-run recovery admission. Its candidate binds current administrator authority, source, Git identity, provider settings and the original failed task. An optional executable adoption adds a server-derived file identity and SHA-256 to that cut. The admission, reservation and dispatch WAL retain the reviewed descriptor; each execution boundary rechecks it. The Engine builds a separate provider for that task with only the executable setting overlaid, so later normal repairs retain the original model configuration and CLI. See [updated CLI recovery](recovery.md#adopt-an-updated-cli-for-one-recovery) for the HTTP contract and installation limits.

Bridge completion requests follow the same uncertainty rule. A transport failure, unusable completion or cancellation during an unfinished submission retains the original intent and workspace for reconciliation. Local cancellation stops scheduling but cannot establish the remote result. The hold survives cancelled, interrupted or failed run status; resume, handoff and replacement retry check it before dispatch. Recovery of the original Bridge job is not automated. See [provider failure handling](agents.md).

Opt-in implementation checkpoints support explicit continuation at the original model boundary. The checkpoint and Bridge intent commit together before the completion POST. A bounded, read-only candidate projection verifies the retained journal, current authority and original ticket bindings before Studio offers a review. The projection makes no Bridge call and writes no ticket or consume receipt.

<div class="forge-flow forge-flow--original-result" role="region" aria-label="Original model result recovery diagram" aria-describedby="original-result-flow-guide" tabindex="0" markdown="1">

```mermaid
---
config:
  flowchart:
    wrappingWidth: 96
---
flowchart LR
    A["Save checkpoint<br/>and request intent"] --> B["Send original<br/>Bridge request"]
    B --> C["Hold uncertain<br/>original result"]
    C --> D["Read or check<br/>original result"]
    D --> E["Review<br/>continuation"]
    E --> F["Commit one-use receipt<br/>before native parsing"]
    F --> G["Continue implementation<br/>and verify"]
```

</div>

<p class="forge-flow-caption" id="original-result-flow-guide">Save the original checkpoint and intent before submitting to Bridge. If the result is uncertain, retain its identity, read or check the original result, then review one continuation. Continuing commits the one-use consume receipt before native parsing, followed by implementation and verification. On a narrow screen, scroll horizontally or focus the diagram and use the arrow keys.</p>

The restore ticket binds the original checkpoint, intent and selected result record. The execution owner repeats authority, source, configuration and deadline checks before restoring native Agentic history with `prompt=None`. The selected response is consumed once before the SDK parser receives it. A failure after that commit cannot authorize replay. A later repair keeps its own task identity; its mutable task key cannot replace the original implementation receipt. Owner recovery retains a committed consume boundary and holds unfinished tickets for reconciliation. See the [operator recovery journey](recovery.md#review-a-held-result-in-studio) for supported file tools and current limits.

`BridgeProvider.execute()` creates a separate pending-request set for each invocation. Agentic task-graph children inherit that shared object through a context variable. Each completion tracks its own attempt before transport and clears it after a validated response, an exhausted stream or a known quota/pause response. Completing one request cannot clear another pending request. If the task graph unwraps a cancellation exception, the outer provider checks its own set before returning cancellation to the Engine. Both context tokens are reset when their scopes end. The Engine then records an unresolved submission against the original journal intent before finishing local cancellation. Regression controls cover the real `FireflyAgent` path, concurrent invocations on a shared provider, successive completions and restart through the native Store.

Explicit manual continuation preserves the run's existing retry counter, including an absent field. Automatic retries increment it. Source-control consent can therefore compare the same frozen run fields before and after an authorized manual resume. The run identity, plan and execution actor remain subject to their existing checks.

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

A signed functional-product change uses a separately approved whole-product capability. It binds the existing project's full component graph, mapped native user, product repository and expected product revision/digest. Provider and credential admission finish before native transaction locks. The transaction repeats current authority checks and stores the immutable product revision and delegated action result together. Exact recovery reads retain the original source and result; they do not authorize another edit or start execution. See [functional product changes from SCM](scm.md#change-an-existing-functional-product).

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

Built-in model instructions are versioned YAML resources loaded and rendered by native Firefly Agentic templates. A bounded ID/version/SHA256 manifest validates packaged resources; existing call sites preserve message roles and ordering. Retained role and task instructions keep their original text and digests. The catalog identifies package content, not historical model invocations. See [maintain model instructions](prompt-templates.md) for the resource layout, variable contract and editing checks.

| Runtime | Tool authority | Evidence boundary |
| --- | --- | --- |
| Native Agentic / bridge inference | Requested tools intersected with actual configured capabilities | File scope, credentials and executor grants are enforced by Python. |
| Official Codex / Claude Code | The official CLI's native tools and permission controls | A Python role tool list is advisory; it is not the CLI sandbox. |
| Native browser QA | Fresh Docker context, declared local origin and bounded actions | Layout diagnostics and screenshots require actual visual/interaction review. |
| Native PHP diagnostics | Explicit `profile_php` plus command grant, frozen Docker/archive/network-`none` runner | Runtime metrics or preinstalled Xdebug observations retain command receipts; they do not authorize or approve work. |
| Internet search | Explicit Brave or Tavily selection, retained credential revision and domain restrictions | Bounded cited results are untrusted references; no key goes to the model. |
| Factory MCP | 162 authenticated operator tools in `mcp_server.py` | These API tools are distinct from a coding model's workspace tools. |
