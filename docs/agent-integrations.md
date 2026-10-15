---
description: Operate forge through the Python SDK, CLI and MCP with authenticated contracts, project scope and retained execution evidence.
---

# Operate products from Python, the CLI or MCP

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

The Python client, CLI and MCP tools call the same authenticated factory API. Project operations, source inspections, product conversations and worker questions use the factory's permissions, revisions and durable receipts. The clients use the running factory and its execution lease.

Set `FORGE_URL` to the running factory and provide `FORGE_API_TOKEN` through your operator environment. Source-control credentials remain in the factory's application or OAuth configuration; do not put the operator token in an issue comment or product brief.

## Logical projects and existing source

Create a JSON `ProjectDefinition` with registered repository IDs, component roots, responsibilities, selected stacks, owners and verification commands. A monorepo uses nonoverlapping roots in one repository; a product split across repositories uses `multi_repo`. `product_repository_id` identifies the member that owns the shared customer definition, brand and layout contract.

```sh
forge project save project.json
forge project list
forge project get PROJECT_ID
forge project get PROJECT_ID --revision 1
forge project analyze PROJECT_ID --component stock
forge project analyses PROJECT_ID
forge project analysis PROJECT_ID ANALYSIS_ID
forge project review PROJECT_ID ANALYSIS_ID --digest EVIDENCE_DIGEST
```

AS-IS inspection reads authorized source without running that project's code. Review acknowledges the exact observed-source digest. It does not invent missing functional requirements or approve implementation, architecture, deployment or publication.

To queue a component change, omit stack and checks when inheriting the configured component:

```sh
forge run --repository REPOSITORY_ID --project PROJECT_ID --component stock \
  --brief "Apply the reviewed account visibility rule"
```

A standalone frontend can select `--backend none`. Explicit stack choices must match a configured component. `--roles-file role-selection.json` applies validated role overrides; a run retains the resulting snapshot.

## Delegate repository accounts

SCM delegation connects a recorded provider account to an existing Forge user for explicit project components and actions. It does not create a user, grant project membership or turn a webhook sender into a personal login. The mapped user must remain enabled and retain the required project permission.

Read the verified account references before preparing a delegation. Candidates come from recorded signed deliveries in the configured repository scope; listing them does not contact the provider or discover accounts remotely.

```sh
forge project scm-actors candidates PROJECT_ID REPOSITORY_ID
forge project scm-actors list PROJECT_ID REPOSITORY_ID
forge project scm-actors configure PROJECT_ID REPOSITORY_ID reviewed-delegation.json
forge project scm-actors get PROJECT_ID REPOSITORY_ID ORIGINAL_REQUEST_ID
```

The JSON document uses the native `SCMActorBindingInputV1` contract. Keep its stable `request_id`, exact verified receipt and reference digest, existing `native_user_id`, explicit `component_ids` and `allowed_actions`. Copy the candidate page's `project_binding_digest` into `expected_project_binding_digest` to bind approval to the reviewed project configuration. Supply the expected previous digest when revising a mapping. The administrator must explicitly acknowledge that the account identity is asserted through the configured webhook. Set `active` to `false` to request a revoked revision; old receipts remain readable under current access.

The initial delegated actions are `respond`, `status` and `cancel`. A mapping does not approve generated work, authorize publication or substitute for an adoption decision. Other actions require their own admitted native path. Provider origin, installation or workspace scope, repository grants and current mapping are checked by the server.

| Operation | Python SDK | Factory MCP |
| --- | --- | --- |
| Read verified accounts | `scm_actor_binding_candidates` | `list_scm_actor_binding_candidates` |
| Delegate or revoke | `configure_scm_actor_binding` | `configure_scm_actor_binding` |
| Read current mapping heads | `scm_actor_bindings` | `list_scm_actor_bindings` |
| Read the original receipt | `scm_actor_binding` | `get_scm_actor_binding` |

All methods take the project and repository IDs. Candidate and current-head reads accept an optional cursor and return at most fifty entries. Pass the returned `next_cursor` unchanged to read another page. A head page lists the current revision of each mapping; it is not a complete revision history. Exact reads take the original request ID and can retrieve an older receipt under current access.

Retain that request ID before configuring a mapping. A timeout, conflict or permission refusal does not establish whether a write committed. Use the exact receipt read to recover the original result; these clients never retry a write, invent another identity or reset the previous digest automatically. MCP marks configuration as an explicit authority-changing operation. Its three reads do not change mappings or invoke provider discovery.

## Delivery requests and recovery

Publication and cancellation are worker-mediated requests. Python `execution_requests(run_id)` and MCP `get_execution_requests` read their status and events. An accepted request is still pending until its worker result is retained; use the recorded URL and receipts to identify successful publication.

Python `reconcile_execution_request(run_id, request_id, reconciliation_id)`, MCP `reconcile_execution_request`, and CLI `forge reconcile-execution-request` ask the server to match existing evidence. They accept a stable 32-character hexadecimal reconciliation ID and no caller-supplied outcome. They do not dispatch delivery or repeat an uncertain effect. Read [delivery request recovery](recovery.md#reconcile-worker-delivery-requests) for authority checks, idempotency and unknown-result handling.

## Exact observation attempts

V2 source-verification attempts can distinguish an immutable local refusal from an uncertain operation. They read an existing flycanon source and its citations; they do not create or ingest a source. The original observation and verified-receipt APIs remain available with their existing UUID request identifiers.

`observe_learning_projection_attempt(record_id, request)` accepts an `obs2_` prefix followed by 32 lowercase hexadecimal characters. Its strict request includes `source_id` and the expected `projection_id`, `submission_request_id`, `workspace_id`, `target_revision`, `payload_digest` and `content_sha256` from the saved projection. `source_id` can be omitted to select the projection's admitted source. A recorded source cannot be replaced. Enter a source identifier, never a token, URL or credential.

Send a request once, then read its exact local receipt:

```sh
forge learning observe-attempt RECORD_ID observation.json
forge learning observation-attempt RECORD_ID --attempt-id obs2_0123456789abcdef0123456789abcdef
```

The Python read method is `learning_projection_observation_attempt(record_id, attempt_id)`. Factory MCP exposes `observe_learning_projection_attempt` and the read-only `get_learning_projection_observation_attempt`. The API routes are `POST /api/v1/learning/records/{id}/projection/observation-attempts` and `GET /api/v1/learning/records/{id}/projection/observation-attempts/{attempt_id}`. A successful POST response still needs exact GET readback before a client treats the request as confirmed or correctable.

| Retained state | What it establishes | Permitted recovery |
| --- | --- | --- |
| `not_dispatched` | This exact request was refused locally before Canon dispatch | After a matching, authorized receipt read and current status read, explicitly review a fresh request with a different attempt ID |
| `verified` | The exact attempt is associated with an immutable observation event | Read the referenced observation receipt and verify its identity, digest, actor and scope |
| `preparing`, `dispatched`, `unknown` | No terminal refusal or exact verification is established | Keep the original identity and use readback; never replay or infer an outcome from elapsed time |

The server commits a request claim before preflight and commits dispatch admission before opening the Canon client. The attempt's origin and terminal receipt are protected by native PostgreSQL guards. Unadmitted selected input is retained only as digests. `input_digest` hashes the complete declared request, including the default `source_id: null`; `source_digest` hashes `{"source_id": INPUT_VALUE}`. Both use SHA-256 over UTF-8 JSON with sorted keys, comma/colon separators and no ASCII escaping.

The fixed refusal reason, `local_admission_refused`, does not classify an engineering failure or prove the input was wrong. Credential-admission unavailability can also refuse a local request. Current review authority and credential admission are required to read a receipt. No receipt read contacts Canon.

A fresh v2 identity is admitted only when every prior v2 attempt for the record has an intact `not_dispatched` receipt. The server checks this under the same record lock as request admission. Changing the request ID cannot bypass an in-flight, unknown or already verified attempt.

An HTTP 400 or 404 response, malformed receipt, generic verified projection or legacy marker cannot authorize another request. Validation, authorization, storage or the 100-attempt history bound can refuse a request without creating a recoverable claim; absence remains unresolved. Cancellation, a lost commit acknowledgement and failure after dispatch do not authorize replay. There is no abandonment or retry operation in this contract.

## Product conversations

```sh
forge assistant start REPOSITORY_ID --project PROJECT_ID --agent bridge
forge assistant list REPOSITORY_ID
forge assistant ask REPOSITORY_ID CONVERSATION_ID \
  --request-id 0123456789abcdef0123456789abcdef \
  --message "Help define the customer journey and expected failure behavior"
forge assistant get REPOSITORY_ID CONVERSATION_ID
```

Use the project's canonical product repository when starting a project conversation. `bridge` uses the configured bridge connection; `agentic` uses its configured model. Starting a conversation freezes settings without invoking a model. Asking a question makes a bounded model request and retains its response as an unapproved proposal. It does not edit the saved product or source, queue a run, resume a worker or approve a review.

Choose a dialogue purpose and, when needed, a named profile. The supported purposes are `product`, `functions`, `design` and `architecture`. A reviewed AS-IS selection requires its project, analysis ID and exact digest together:

```sh
forge assistant start REPOSITORY_ID --project PROJECT_ID --task functions \
  --model-profile product-planning --analysis ANALYSIS_ID \
  --analysis-digest REVIEWED_DIGEST --analysis-component api
forge run --repository REPOSITORY_ID --brief "Implement the accepted change" \
  --model-profile source-builder --review-model-profile independent-reviewer
forge handoff RUN_ID --model-profile alternate-builder
```

Omit `--agent` when using a task binding or named profile. An explicit agent must match that profile's provider. `forge resume RUN_ID` retains the original frozen selection. An explicit profile handoff records the previous and new selections without resetting repair or browser budgets. The legacy `forge handoff RUN_ID --agent agentic` remains available and returns to the provider's configuration. Server-side project permissions and runner requirements still apply.

The SDK accepts the same fields through `create_assistant_conversation(..., task=..., model_profile_id=..., analysis_id=..., analysis_digest=..., analysis_component_ids=[...])` and `handoff(run_id, model_profile_id=...)`. Its run request remains the native `RunInput`, including `model_profile_id` and `assurance.review_model_profile_id`.

Reuse the same request ID only for the same message and attachment selection. If a client times out, inspect the conversation before making another request; the original request may have completed. The client retains a longer timeout for design turns and source analysis, while the server owns model and inspection limits. Uncertain responses remain visible and are not automatically resubmitted.

### Conversation files

Upload a selected file from the CLI's own machine, then use its saved ID in a later turn:

```sh
forge assistant upload REPOSITORY_ID CONVERSATION_ID --file ./requirements.pdf \
  --request-id 0123456789abcdef0123456789abcdef
forge assistant files REPOSITORY_ID CONVERSATION_ID
forge assistant file REPOSITORY_ID CONVERSATION_ID ATTACHMENT_ID
forge assistant ask REPOSITORY_ID CONVERSATION_ID \
  --request-id fedcba9876543210fedcba9876543210 --attachment ATTACHMENT_ID \
  --message "Use these requirements to identify unanswered journey questions"
```

The CLI accepts a regular local file up to 1 MB and rejects symbolic links. The factory parses supported documents and images in its bounded decoder, stores an immutable conversation-scoped receipt, and allows at most 20 files per conversation. Upload does not invoke a model or index Flycanon. The upload ID is stable: repeat it only for the exact filename and content. After an uncertain response, list files before retrying.

Repeat `--attachment` to select up to eight saved files. Omitting it inherits the previous turn's selection. `--detach-attachments` explicitly clears that selection and cannot be combined with file IDs. File IDs from another conversation or repository are rejected. Images require verified image-input capability for the conversation's frozen model; uploading an image alone does not establish that capability.

Python exposes `assistant_attachments`, `assistant_attachment` and `upload_assistant_attachment`. Upload accepts native `AttachmentInput` or an equivalent dictionary with `request_id`, a plain `filename`, and bounded `content_base64`; it never requests a path on the factory host. Returned file receipts contain metadata, not source bytes, extracted text or image pixels. Pass `attachment_ids=[...]` through `assistant_turn` to select files, `[]` to detach, or omit it to inherit. The SDK suppresses upload errors that could reflect source content.

Read the conversation's frozen-provider image capability with `forge assistant capabilities REPOSITORY_ID CONVERSATION_ID`, Python `assistant_capabilities`, or MCP `get_product_conversation_capabilities`. This read does not invoke a model. A verified receipt applies to the frozen configuration shown by that conversation.

For an expired active turn, Python `recover_assistant_turn` accepts `AssistantRecoveryInput` or a dictionary with a stable `request_id`, the explicitly observed `expected_epoch` and optional `legacy_confirmed_stopped`. MCP exposes the same fields through `recover_product_design_turn`; the CLI uses `forge assistant recover`. Recovery records reconciliation and fences late replies without resubmitting a model request. Read [Conversation recovery](recovery.md#recover-a-product-conversation) before using the legacy confirmation flag.

## Role instructions and worker answers

```sh
forge roles get
forge roles save role-document.json
forge roles repository-get REPOSITORY_ID
forge roles repository-save REPOSITORY_ID project-role-selection.json
forge feedback list RUN_ID
forge feedback answer RUN_ID REQUEST_ID --response-id answer-1 \
  --actor operator --answer "Support account owners receive the alert"
forge resume RUN_ID
```

Save the `document` object from the registry response, rather than the whole response envelope. Registry edits affect future runs. Binding fields and mandatory platform checks remain enforced. Answers are journaled and leave the run paused until explicit resume; they do not grant publication or deployment approval.

## Current identity and project membership

`identity_configuration()` reads the public `/api/v1/auth/configuration` response. `identity_session()` reads the authenticated caller's current identity, memberships and server role permissions. The native session route accepts a verified API bearer or a valid browser session cookie; it returns neither credential. The SDK constructor still requires an operator token, and CLI/MCP use the configured bearer. These read bindings do not perform browser sign-in, exchange codes or export a cookie.

| Action | Python method | CLI command | MCP tool |
| --- | --- | --- | --- |
| Public authentication configuration | `identity_configuration()` | `forge identity configuration` | `get_identity_configuration` |
| Current authenticated caller | `identity_session()` | `forge identity session` | `get_identity_session` |
| Project membership list | `project_members(project_id)` | `forge project members PROJECT_ID` | `list_project_members` |
| Set or revoke membership | `set_project_membership(project_id, user_id, request)` | `forge project member PROJECT_ID USER_ID --role viewer` or `--revoke` | `set_project_membership` |

Membership reads and writes require current project-owner or factory-administrator authority. The target user must already be registered. The native `MembershipInput` requires `role`: `owner`, `maintainer`, `developer`, `reviewer`, `viewer`, or explicit `null` to revoke. CLI requires exactly one of `--role` and `--revoke`; MCP accepts the same closed native body under `request`. The server preserves its last-owner safeguard and records the actor and membership revision. Revocation applies at the next authorization boundary, including subsequent member reads and writes.

A membership write has no request-ID or compare-and-swap field in the native contract. Writes to an existing membership advance its revision. Revoking a registered user who has no membership leaves it absent. The clients dispatch once; after a lost response, inspect current membership before making another decision.

Connection OAuth configuration now also accepts optional CLI `--expected-revision` and MCP `expected_revision`. Read the current connection revision, then supply it to reject a stale edit. `0` asserts that the registration does not yet exist. Omitting the field preserves existing no-revision behavior; it does not make a write idempotent or grant consent. The existing tool schema adds only this optional bounded integer field.

## Read role lifecycle and identity administration

The following reads use the existing native API without creating users, changing membership, sending invitations or reconciling an identity operation.

| Read | Python method | CLI command | MCP tool |
| --- | --- | --- | --- |
| Global role lifecycle | `agent_role_lifecycle()` | `forge roles lifecycle` | `get_agent_role_lifecycle` |
| Repository role lifecycle | `repository_role_lifecycle(repository_id)` | `forge roles repository-lifecycle REPOSITORY_ID` | `get_repository_agent_role_lifecycle` |
| Registered users | `identity_users()` | `forge identity users` | `list_identity_users` |
| Eligible registered users | `project_eligible_users(project_id, query)` | `forge project eligible-users PROJECT_ID --query "Stock owner"` | `find_project_eligible_users` |
| Identity administration readiness | `identity_administration()` | `forge identity administration` | `get_identity_administration` |
| Retained identity operations | `identity_operations()` | `forge identity operations` | `list_identity_operations` |

Global lifecycle requires authentication. Repository lifecycle requires current repository read permission; its returned edit flags describe current authority. Finding eligible users requires project membership-administration permission, a query of 3 to 120 characters and an enabled registered identity. The server returns at most 20 matches and treats wildcard characters literally. The registered-user directory, administration readiness and operation receipts require current factory administrator authority.

The clients preserve the server's bounded contact fields, permission flags and readiness distinctions. A configured administration connection does not prove user provisioning or mail delivery. Reading a retained operation preserves its status and journal evidence without retrying an unknown effect. These six reads are a bounded addition; they do not establish universal API, SDK, CLI and MCP parity.

## Identity writes

Five identity mutations use the existing administrator-only routes and native input models. They grant no new permissions or project memberships. Python methods accept the native request model or an equivalent dictionary; local user and operation IDs are validated before dispatch.

| Write | Python method and MCP tool | CLI command |
| --- | --- | --- |
| Enable or suspend a local identity | `set_identity_user_enabled` | `forge identity set-enabled USER_ID --enable` or `--disable` |
| Request provider-managed account creation | `provision_identity_user` | `forge identity provision user.json` |
| Request a separate invitation | `invite_identity_user` | `forge identity invite USER_ID --request-id REQUEST_ID` |
| Reconcile an inspected uncertain outcome | `reconcile_identity_operation` | `forge identity reconcile OPERATION_ID --outcome OUTCOME --note "Inspection evidence"` |
| Confirm an interrupted owner has stopped | `recover_identity_operation` | `forge identity recover OPERATION_ID --request-id REQUEST_ID --owner-stopped --note "Owner stopped"` |

`user.json` uses `UserProvisionInput`: a stable 32-character hexadecimal `request_id`, `username`, and optional `email`, `first_name` and `last_name`. Passwords, role shortcuts and caller-supplied provider results are rejected. Enable/suspend requires an actual boolean in Python or MCP and an explicit CLI flag. MCP marks all five tools as mutations.

Python `invite_identity_user(user_id, request)` takes `UserInviteInput`; MCP accepts `user_id` and `request_id` directly. Reconciliation takes `AdministrationReconciliation` under the MCP `request` argument. `created` requires the exact provider `subject`, passed as `--subject` in the CLI; the server freshly checks the provider account's original request markers. The other outcomes retain the server's explicit operator-attestation labels. Recovery takes `AdministrationRecovery`: `request_id`, `owner_stopped: true` and `note`. It fences an executing operation for reconciliation and does not submit another remote effect.

Each client dispatches once and preserves the returned receipt. An identical provision or invitation request returns its existing receipt without repeating the effect. After a transport failure, inspect `forge identity operations` before deciding what to do; no client retries automatically or invents an outcome. Unknown operations require inspection and explicit reconciliation. Reconciliation is not a replay-safe polling action, and mail provider acceptance still does not prove delivery. Read [user administration](user-administration.md) before using these writes. Coverage of all other API routes remains separate.

## Python client

```python
import os
from firefly_forge.client import ForgeClient

with ForgeClient(os.environ["FORGE_URL"], os.environ["FORGE_API_TOKEN"]) as factory:
    projects = factory.projects()
    project = factory.project(projects[0]["id"])
    evidence = factory.project_analyses(project["id"])
    roles = factory.agent_roles()
```

The client validates project definitions, analysis reviews, role documents, design messages and worker responses against the native DTOs. Its project and dialogue identifiers are normalized before constructing request paths. Explicit configuration methods and model turns are writes; listing projects, evidence and conversations does not invoke a model.

## Model administration and product changes

Factory administrators can configure named profiles and future task bindings through the same API:

```sh
forge models list
forge models save source-builder profile.json --credential-file /private/path/provider.key
forge models test source-builder
forge routing get
forge routing save routing.json
forge product preview-change REPOSITORY_ID change.json
forge product apply-change REPOSITORY_ID change.json
```

`profile.json` contains the native `ModelProfileInput` metadata and nonsecret `values`. Supply an API key or bridge token only through the separate private credential file, owned by the CLI user with mode `0600` or stricter. Links are rejected. The CLI never accepts an inline secret value in profile JSON, and configuration responses omit raw keys. Official CLI profiles use their own signed-in account and do not accept a credential file. A connection test reports readiness; it does not prove code generation or visual reasoning.

`routing.json` contains `{"bindings": {"functions": "product-planning", "implementation": "source-builder"}}`. The named profiles must already exist and support their selected tasks. Changes affect future invocations; queued snapshots remain unchanged.

`change.json` uses `FeatureChangeInput`: a stable `request_id`, exact `base_revision` and `base_digest`, and bounded feature/integration changes or appended journeys and acceptance criteria. Preview does not save a revision. Apply uses the same validated request and the server's stale-base and replay guards. It preserves unaffected definition entries, records an immutable change receipt and clears affected reviews. Saving a contract revision does not implement its code. Integration credentials remain secret references, not values in the definition. See [Incremental product changes](product-lifecycle.md) for the complete contract.

Python exposes `model_profiles`, `save_model_profile`, `test_model_profile`, `task_routing`, `save_task_routing`, `preview_product_change` and `apply_product_change`. Typed configuration methods can receive secrets from an application secret manager. The SDK filters profile responses and suppresses configuration errors that could reflect a submitted secret.

## MCP

Operators can inspect an uncertain learning invalidation through Python `learning_invalidation_receipt(record_id, request_id)`, `forge learning invalidation-receipt RECORD_ID --request-id REQUEST_ID`, or `get_learning_invalidation_receipt`. Each performs the same authorized GET for the exact committed request. The returned historical decision does not replace the current record state. A 404 remains unobserved evidence and never triggers a second write. Keep the original request identity and reason; see [invalidation receipts](learning.md#inspect-an-uncertain-invalidation).

Run `forge mcp` from a compatible MCP client with `FORGE_URL` and `FORGE_API_TOKEN` supplied to that process. The transport is stdio. The tool catalog includes:

| Task | Tools |
| --- | --- |
| Project topology | `list_projects`, `configure_project`, `get_project`, `get_project_revision` |
| Existing source | `analyze_project`, `list_source_analyses`, `get_source_analysis`, `review_source_analysis` |
| Product dialogue | `start_product_conversation`, `list_product_conversations`, `get_product_conversation`, `request_product_design_turn` |
| Conversation files | `upload_product_attachment`, `list_product_attachments`, `get_product_attachment` |
| Roles and feedback | `get_agent_roles`, `save_agent_roles`, `get_repository_agent_roles`, `save_repository_agent_roles`, `get_worker_messages`, `respond_worker_request` |
| Component implementation | `create_run` with optional `project_id`, `component_id` and `roles` |
| Named model profiles | `list_model_profiles`, `configure_model_profile`, `test_model_profile`, `get_task_routing`, `save_task_routing` |
| Incremental contracts | `preview_product_change`, `apply_product_change` |

`handoff_run` accepts a named `model_profile_id` or a legacy `agent`. `start_product_conversation` accepts the dialogue task, named profile and reviewed AS-IS fields described above. `configure_model_profile` accepts typed metadata and an optional `credential_file` path on the **MCP host**. It rejects inline `values.api_key` and `values.token`; neither a key nor its file contents appear in the tool result. The remaining tools preserve their native authorization and review requirements.

`upload_product_attachment` takes an explicit bounded base64 request and returns metadata only. It does not read an arbitrary host path. `request_product_design_turn` accepts optional `attachment_ids` with the same inherit, detach and selection behavior as Python and the CLI. Listing or reading a file receipt does not invoke a model; asking a design turn remains a separate action.

The existing run, repository, knowledge, application and deployment tools remain available. Publication and deployment require separate explicit calls and retain their native gates. Role tool grants constrain available native executors; official CLI worker permissions remain a separate provider boundary. See [Factory roles](agent-roles.md) and [Repository workflow](repository-workflow.md) for source-control permissions and questions.

## Product delivery campaigns

Campaigns coordinate the selected component DAG and its dependencies. They freeze the source commits, product contract and configured role/runner settings. Each child keeps its own execution journal. A dependent waits for matching verification and checkpoint receipts, or publication receipts when the launch requires that gate. Upstream artifacts remain references; admission does not merge code or deploy it.

Create a launch JSON using the native `CampaignLaunch` fields:

```json
{
  "request_id": "0123456789abcdef0123456789abcdef",
  "brief": "Apply the reviewed account visibility behavior across API and workspace",
  "component_ids": ["api", "workspace"],
  "agent": "codex",
  "operation": "build",
  "dependency_gate": "verified",
  "publish": false
}
```

```sh
forge campaign launch PROJECT_ID launch.json
forge campaign list PROJECT_ID
forge campaign get PROJECT_ID CAMPAIGN_ID
forge campaign cancel PROJECT_ID CAMPAIGN_ID --request-id CANCEL_REQUEST_ID
forge campaign resume PROJECT_ID CAMPAIGN_ID --request-id RESUME_REQUEST_ID
forge campaign retry PROJECT_ID CAMPAIGN_ID --request-id RETRY_REQUEST_ID --component api
```

Use a distinct 32-character hexadecimal request ID for each intentional control action. Reuse an ID only to inspect or replay the exact same request. Inspect the campaign after a timeout: its action may have completed. The clients do not automatically retry writes. Unknown effects, pending worker questions and active dependent work retain the server's recovery guards. Explicit component retries retain the frozen admission and its bounded retry budget, including the original project settings. The response retains all attempts and marks the current ones with `current: true`.

Python exposes `project_campaigns`, `launch_campaign`, `project_campaign`, `cancel_campaign`, `resume_campaign` and `retry_campaign`. The request arguments accept the native campaign DTO or its JSON dictionary. Listing and reading campaigns invoke no model. Launch admits and queues work; it does not prove that the product has been built.

## Runner defaults, search and browser evidence

Save a native `SandboxInput` JSON for a repository's future runs:

```sh
forge runner get REPOSITORY_ID
forge runner save REPOSITORY_ID runner.json
forge runner readiness REPOSITORY_ID
forge runner probe runner.json
forge runner screenshot EVIDENCE_ID --output review.png
```

Host mode uses the trusted service account without container CPU, memory or network isolation. Docker mode requires a configured context and an immutable image digest, and cannot fall back to host execution. Optional browser and search capabilities must be configured and granted in the run's role snapshot. Readiness reports actual runner availability; cached image presence alone does not prove that a project server or its Playwright package works. A probe does not pull images or execute project commands.

The SDK methods are `repository_runner`, `save_repository_runner`, `runner_readiness` and `probe_runner`. `browser_screenshot` retrieves authenticated PNG bytes from an existing capture. `download_browser_screenshot` streams at most 2 MB, checks the PNG header and creates an owner-only file without overwriting an existing destination. Downloading a capture does not perform another browser check or approve its visual design.

Supply the search provider key through a private regular file owned by the CLI process, with mode `0600` or stricter. Symlinks and hardlinks are rejected. Create that file from your secret manager; do not paste keys into command arguments, launch JSON, prompts or source files.

```sh
forge search configure --key-file /private/path/brave.key
forge search get
forge search test
forge search query "official framework documentation" --count 3
forge search citation web-CITATION_DIGEST
forge search configure --clear-secret
```

Configuration returns readiness metadata and never returns the key. `search test` makes one bounded provider request and can consume provider quota. Query results are untrusted evidence with cached citation IDs; they cannot change execution permissions or approve a product decision. A citation lookup reads its existing cache entry without fetching the external page. The SDK methods are `search_configuration`, `configure_search`, `test_search`, `internet_search` and `search_citation`; keys supplied to the typed Python configuration method should come from your secret manager.

The additional MCP tools retain the same API contracts:

| Task | Tools |
| --- | --- |
| Delivery campaign | `list_project_campaigns`, `launch_project_campaign`, `get_project_campaign`, `cancel_project_campaign`, `resume_project_campaign`, `retry_project_campaign` |
| Runner configuration | `get_repository_runner`, `save_repository_runner`, `check_repository_runner`, `probe_runner` |
| Mediated search | `get_internet_search_configuration`, `configure_internet_search`, `test_internet_search`, `search_internet`, `get_search_citation` |
| Recorded browser image | `get_browser_evidence_image` |

`configure_internet_search` takes a private key-file path on the **MCP host**, rather than a model-visible key, or an explicit `clear_secret`. The factory server receives the key through its authenticated write-only configuration API. `get_browser_evidence_image` returns native MCP image content from an existing authenticated capture. The forge transport remains stdio; these tools do not create a public HTTP MCP connector.
