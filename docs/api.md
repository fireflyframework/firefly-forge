---
description: Use authenticated forge HTTP endpoints to manage products, repositories, execution, source evidence and project operations.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Use the HTTP API

The API prefix is `/api/v1`. Hosted installations use an OIDC access token with
the configured API audience. Browser sessions use the server's secure session
cookie and CSRF protection. The explicitly enabled local bootstrap also accepts
`Authorization: Bearer <FORGE_API_TOKEN>` from loopback clients. See
[Sign in and manage project access](identity.md) for each mode.

Request and response bodies use JSON unless a route downloads an artifact or
file. Project permissions apply to the authenticated identity. The actual
running application's OpenAPI document is the authoritative schema.

## Automatic verification handoffs

New runs freeze `settings.automatic_verification` from `GET /tools/policy`.
The factory default is `{"enabled": true, "max_continuations": 1}`; the strict
maximum is three. The existing administrator-only policy update accepts this
setting with the other tool settings. A frozen snapshot without this setting
does not grant automatic execution.
Older SDK/MCP policy writes that omit this setting preserve its current value.

A pending implementation request with typed `kind: "verification"` can ask
Forge to run all explicit, frozen `run.commands` through `SandboxExecutor`.
Request IDs, question text and context never select commands or grant access.
Product decisions, credentials, architecture and approval questions remain
manual. Current execution authority, cancellation, campaign scope, source,
provider and policy are checked again before effects and continuation.

Native command intents and receipts retain the actual commands and outcomes.
Known failing checks become bounded, clearly attributed factory feedback.
`completed` means every configured command has a known result, including any
failures; it does not mean the product passed acceptance. Unknown effects hold
without automatic replay. Source identity includes untracked public source and
uses the existing review exclusions for dependencies, private files, build
outputs and tool caches, including `.cache`. Those directories are not a place
to store application source.

Each completed, durably resumed handoff grants one additional coding task,
separate from the frozen repair budget. History is preserved, and the existing
four-attempt ceiling remains. A spent, revoked, stale or uncertain grant cannot
start another coding task. The factory response uses protected
`responding_actor.version: 4` / `authentication_method: "factory_verification"`
with its handoff and source/command/policy/provider digests; it is not a human
answer. Pre-admission refusals have a separate `verification_handoff_refused`
event and leave the original question pending.

Older paused runs require explicit native adoption:

1. Read `GET /runs/{id}/verification-handoffs/candidates?agent=codex` (or another
   explicitly selected configured coding provider).
2. Retain one new 32-character `request_id` and POST
   `/runs/{id}/verification-handoffs/adoptions` with `agent` and the exact
   `pending_request_digest`, `source_digest`, `commands_digest`, `policy_digest`
   and `provider_digest` from that read.
3. If the response is lost, read
   `GET /runs/{id}/verification-handoffs/adoptions/{request_id}`. Do not generate
   a replacement ID or retry an uncertain command.

Adoption records the current native operator and exact request body. Reusing an
ID with different content refuses. It binds only that pending question and
partial source, not later questions. Selecting a provider explicitly captures
its current configuration through the normal native handoff; old frozen
provider executables and snapshots stay immutable. Host coding providers still
require current administrator authority. Admission queues work for the normal
Engine consumer and never executes commands in the HTTP request.

The Python SDK exposes `verification_handoff_candidates`,
`adopt_verification_handoff` and `verification_handoff_adoption`. These methods
perform one explicit request each and do not create IDs, apply a fallback or
resume a run implicitly.

## Failed official-provider recovery

A native factory administrator can review `GET /runs/{id}/provider-recoveries/candidate?agent=codex` or `claude`, explicitly POST the returned candidate digest under one caller-owned request ID to `/runs/{id}/provider-recoveries`, and read its exact receipt at `/runs/{id}/provider-recoveries/{request_id}`. Admission reserves one dispatch in the same run. It does not execute a model in the request or reset repair and verification budgets.

The strict body requires exactly one native process digest or explicit historical operator attestation. New-protocol unknown outcomes stay held. Current authority, source, provider and policy are checked again before launch, and all implementation intents count toward the four-dispatch limit. See [failed official CLI recovery](recovery.md#recover-a-known-failed-official-cli-attempt) for the proof fields, provenance and lost-response procedure. This change exposes HTTP endpoints only.

### Final native recovery allowance

The separate `/runs/{id}/provider-recoveries/allowances` surface reserves one fifth dispatch after a known native fourth-attempt failure. It requires a current native administrator with current execution authority. It accepts no historical process attestation or budget override.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/runs/{id}/provider-recoveries/allowances/candidate?agent=codex&adopt_current_executable=true` | Review the exact eligible failure and selected executable |
| POST | `/runs/{id}/provider-recoveries/allowances` | Reserve and consume the one lifetime final allowance |
| GET | `/runs/{id}/provider-recoveries/allowances/{request_id}` | Read the exact receipt as the original current operator |

The strict POST body contains `request_id` (32 lowercase hexadecimal characters), `candidate_digest`, `native_process_digest`, `mitigation_evidence_digest` (each 64 lowercase hexadecimal characters), `agent` (`codex` or `claude`), `adopt_current_executable` (required literal `true`), and a nonblank `reason` of at most 2,000 characters. The mitigation digest is an administrator attestation, not server-generated QA evidence. HTTP201 retains a version-2 receipt with protocol `native-provider-recovery-allowance-v1`; the HTTP request does not execute inference.

Same ID, body and current operator return the existing receipt. A stale candidate, different request, revoked authority or spent allowance refuses without another reservation. A missing response never renews eligibility. Legacy recovery routes retain their existing schemas and four-dispatch limit; reading a final allowance through the legacy receipt route returns HTTP409 identifying the new endpoint. See [the recovery procedure](recovery.md#reserve-one-final-native-provider-attempt) for launch checks, executable limits and lost-response handling.

Fifth-attempt worker feedback uses `protocol: "native-provider-recovery-allowance-v1"`, literal `attempt: 5`, `role_id: "implementer"`, `task_id: "implement"`, `allowance_id` and `allowance_digest`, alongside the usual feedback fields. The native journal checks the actual consumed reservation and current authority before accepting it. Legacy `WorkerFeedback` remains limited to attempts 1–4.

### Scoped coding-check grants

These routes require a current native factory administrator and execution authority. They grant selected retained checks to one preallocated final recovery request; they do not reserve or execute the coding dispatch.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/runs/{id}/coding-check-grants/candidate?recovery_request_id=ID&bind_port=0` | Review the retained commands, runner, source/authority cut and remaining budget |
| POST | `/runs/{id}/coding-check-grants` | Record the explicit grant once |
| GET | `/runs/{id}/coding-check-grants/{request_id}` | Read the original grant or revocation receipt |
| GET | `/runs/{id}/coding-check-grants/current` | Inspect current grant identity, revision, status, receipt fingerprint and expiry |
| POST | `/runs/{id}/coding-check-grants/revocations` | Revoke the exact current grant revision |

The strict `CodingCheckGrantInput` rejects extra fields:

| Field | Required review |
| --- | --- |
| `request_id` | Grant request identity: 32 lowercase hexadecimal characters |
| `recovery_request_id` | Future final-allowance identity: 32 lowercase hexadecimal characters, retained before granting |
| `candidate_digest` | The candidate's opaque 64-character SHA-256 fingerprint |
| `expected_revision` | The candidate's nonnegative grant revision |
| `command_indices` | One to twenty distinct configured indices, each 0–19 and present in the candidate |
| `call_limit` | One to twenty invocations, no more than available interactive capacity; calls may repeat a selected index |
| `valid_seconds` | Lifetime of 30–3600 seconds, also enforced during invocation |
| `bind_port` | `0` by default for automatic loopback selection, or a fixed port from 1024–65535 |
| `reason` | A nonblank explanation of at most 2,000 characters |

The candidate separates `interactive_commands_remaining` from `independent_final_check_count`. Configured and policy checks together remain subject to the existing final-verification limit of 20. Grant creation cannot reset that limit or the frozen shared runner budget.

The immutable grant receipt returns an opaque `receipt_digest`. Compare it with `/current`, along with grant ID, revision and expiry; do not recompute a native receipt hash from client-side JSON numbers. Only `active` current status can authorize the linked path. `none`, `revoked` and `expired` retain their distinct meanings, and an old receipt never overrides current permission or revocation checks.

A revocation request requires its own `request_id`, the exact `grant_id`, current positive `expected_revision` and a `reason` of 1–2,000 characters. Read that original request ID after an uncertain response. The active grant and revocation records remain in the WAL; revocation cannot erase prior effects.

After grant confirmation, refresh the final-allowance candidate and include `coding_check_grant_id` when reserving under the original `recovery_request_id`. Legacy CLI-only allowance bodies keep their existing wire shape. The scoped MCP tool accepts only `command_index` and its own 32-character request ID; it cannot choose a run, provider, directory, arguments, environment or timeout. See [the recovery procedure](recovery.md#grant-scoped-configured-checks-to-an-official-cli).

## Factory endpoints

| Method | Path | Operation |
| --- | --- | --- |
| GET | `/health` | Inspect server health |
| GET | `/providers` | Read configured coding-provider readiness |
| GET | `/openapi.json` | Read the authenticated OpenAPI contract |
| PUT | `/providers/{id}/configuration` | Save write-only provider fields |
| POST | `/providers/{id}/test` | Test configured access |
| GET | `/skills`, `/policies`, `/workflow` | Inspect active policies and real stages |
| POST | `/policies/sync` | Fetch a pinned data-only central catalog |
| GET | `/profiles` | Read backend and frontend contracts |
| GET | `/repositories` | List registered repositories |
| POST | `/repositories` | Register a Git source |
| GET/PUT | `/repositories/{id}/policies` | Read or select project policy IDs |
| GET | `/runs` | List run history |
| POST | `/runs` | Queue generation and verification |
| POST | `/run-creation-requests/{request_id}` | Admit the exact retained run input once under its original identity |
| GET | `/run-creation-requests/{request_id}` | Read the original immutable admission receipt |
| POST | `/run-creation-requests/{request_id}/cancel` | Settle cancellation against a delayed original submission |
| GET | `/run-creation-requests/{request_id}/decision` | Read the exact admitted or cancelled terminal decision |
| GET | `/runs/{id}` | Read a run's events and evidence |
| GET | `/runs/{id}/plan`, `/runs/{id}/journal` | Inspect durable tasks and receipts |
| GET | `/runs/{id}/agent-checkpoints/candidates` | Discover bounded original-result references without continuing work |
| POST | `/runs/{id}/agent-checkpoints/{completion_id}/restores` | Admit one reviewed original-result continuation |
| GET | `/runs/{id}/agent-checkpoints/{completion_id}/restores/{request_id}` | Read that exact continuation ticket without dispatch |
| GET | `/runs/{id}/execution-requests` | Read worker delivery and cancellation requests with their receipts |
| POST | `/runs/{id}/execution-requests/{request_id}/reconcile` | Settle an unknown request only from matching recorded server evidence |
| POST | `/runs/{id}/runner-continuations` | Admit one explicit continuation of the original eligible queued pool command |
| GET | `/runs/{id}/runner-continuations/{request_id}` | Read that exact retained continuation without dispatching work |
| POST | `/runs/{id}/resume`, `/runs/{id}/handoff` | Continue retained source; optionally select a new worker |
| GET | `/deployment-targets` | List delivery targets |
| PUT | `/deployment-targets/{id}` | Configure a delivery target |
| GET | `/deployments` | List delivery records |
| POST | `/runs/{id}/deploy` | Explicitly deploy verified source |
| POST | `/deployments/{id}/rollback` | Run retained rollback commands and checks |
| GET | `/scm/connections` | Read native application setup and status |
| PUT | `/scm/connections/{provider}` | Save write-only native application settings |
| GET/PUT | `/knowledge/configuration` | Read status or save write-only knowledge settings |
| POST | `/knowledge/test`, `/knowledge/search` | Check scope or retrieve cited context |
| POST | `/runs/{id}/knowledge/index`, `/runs/{id}/knowledge/remember` | Explicit verified-source admission or memory |
| POST | `/runs/{id}/cancel` | Request cancellation |
| POST | `/runs/{id}/retry` | Create another attempt after failure, cancellation or interruption |
| POST | `/runs/{id}/publish` | Queue publication of an already verified local change |
| GET | `/runs/{id}/artifacts/changes.zip` | Download retained changed files |
| GET | `/learning/records/{id}/projection/observations/{request_id}` | Read the exact committed projection observation with current review access |
| GET/POST | `/repositories/{id}/assistant/conversations/{conversation_id}/attachments` | List metadata or upload a conversation file |
| GET | `/repositories/{id}/assistant/conversations/{conversation_id}/attachments/{attachment_id}` | Read scoped file metadata |
| GET | `/repositories/{id}/assistant/conversations/{conversation_id}/attachments/{attachment_id}/content` | Download the original conversation file |
| GET | `/repositories/{id}/assistant/conversations/{conversation_id}/capabilities` | Inspect the frozen model's image-input capability without inference |
| POST | `/repositories/{id}/assistant/conversations/{conversation_id}/turns/{turn_id}/recover` | Fence an expired ownership epoch for explicit reconciliation |

The [original-result recovery guide](recovery.md#explicit-closed-text-implementation-continuation) describes the checkpoint, intent and tagged result bindings required by the restore routes. Candidate discovery returns at most 16 saved implementation references and no model history or response body. Retain the original 32-character hexadecimal request ID before a restore POST; exact readback after an uncertain response never creates a replacement request.

The [uncertain run-submission guide](recovery.md#settle-an-uncertain-run-submission) explains exact admission, cancellation and decision readback. The same enabled user can read or settle cancellation with current read access after signing in again. Replaying admission also requires the original authentication method and current execution authority.

## Use explicit recovery from the Python SDK

`ForgeClient` exposes the same original-request and checkpoint contracts. Retain
the request ID and submitted input before calling a write method. These methods
use the client's existing authenticated identity; they do not choose another
identity, retry a request or fall back to legacy `create_run`.

| SDK method | HTTP operation |
| --- | --- |
| `request_run_creation(request_id, request)` | POST the original `{"version": 1, "input": ...}` envelope to `/run-creation-requests/{request_id}` |
| `run_creation_request(request_id)` | GET `/run-creation-requests/{request_id}` for the original admission receipt |
| `cancel_run_creation_request(request_id, request)` | POST the original version-1 digest and repository/project/component scope to `/run-creation-requests/{request_id}/cancel` |
| `run_creation_decision(request_id)` | GET `/run-creation-requests/{request_id}/decision` without submitting cancellation or admission |
| `agent_checkpoint_candidates(run_id)` | GET `/runs/{run_id}/agent-checkpoints/candidates` without continuing work |
| `request_agent_checkpoint_restore(run_id, completion_id, request)` | POST the explicitly reviewed restore input to `/runs/{run_id}/agent-checkpoints/{completion_id}/restores` |
| `agent_checkpoint_restore(run_id, completion_id, request_id)` | GET that exact restore ticket without dispatching another request |

Write methods accept the corresponding native `RunCreationInput`,
`RunCreationCancelInput` or `RestoreInput` model, or a dictionary validated by
that model. Run creation keeps omitted `RunInput` fields omitted, including
default policy selection; an explicit empty `skills` list remains explicit.
Request, run and restore IDs must be lowercase 32-character hexadecimal values;
the original completion ID retains its `fbr1_` prefix.

Cancellation requires the retained `request_digest`, `repository_id`,
`project_id` and `component_id`; unassigned project/component fields are
explicitly null. Do not recompute the digest from current form edits. An
`admitted` cancellation decision identifies an already admitted run and does
not cancel that run.

Restore input contains the retained `request_id`,
`expected_checkpoint_digest`, `expected_intent_digest` and the selected tagged
`source`; it has no additional version field. An `http_response` source carries
its `record_digest`. A `bridge_observation` also carries its original
`observation_id` and `terminal_digest`. Discovery and readback do not select,
observe or consume a result on the caller's behalf.

HTTP errors retain the existing SDK `RuntimeError` behavior; transport errors
propagate from HTTPX. A missing result or uncertain response leaves the outcome
unresolved. Choose exact readback as a separate call, retaining the same IDs;
the SDK never generates a replacement ID or automatically resubmits. Successful
methods return the server's JSON dictionary without projecting or rewriting
receipt actor, input, scope or digest fields. They do not return raw HTTP bytes
or independently verify receipt cryptography.

### Explicit CLI commands and MCP tools

The CLI and MCP tools below call those same SDK methods once under the caller's
existing authentication. Every ID is supplied by the caller; none is generated.
CLI write commands take a saved JSON object document, capped at 1 MB before the
native request validation. They preserve omitted input fields and explicit nulls.
MCP writes take the same native object in a required `request` argument.

| CLI command (after `forge`) | MCP tool | SDK method |
| --- | --- | --- |
| `request-run-creation REQUEST_ID DOCUMENT` | `request_run_creation(request_id, request)` | `request_run_creation` |
| `run-creation-request REQUEST_ID` | `get_run_creation_request(request_id)` | `run_creation_request` |
| `cancel-run-creation-request REQUEST_ID DOCUMENT` | `cancel_run_creation_request(request_id, request)` | `cancel_run_creation_request` |
| `run-creation-decision REQUEST_ID` | `get_run_creation_decision(request_id)` | `run_creation_decision` |
| `agent-checkpoint-candidates RUN_ID` | `get_agent_checkpoint_candidates(identifier)` | `agent_checkpoint_candidates` |
| `request-agent-checkpoint-restore RUN_ID COMPLETION_ID DOCUMENT` | `request_agent_checkpoint_restore(identifier, completion_id, request)` | `request_agent_checkpoint_restore` |
| `agent-checkpoint-restore RUN_ID COMPLETION_ID REQUEST_ID` | `get_agent_checkpoint_restore(identifier, completion_id, request_id)` | `agent_checkpoint_restore` |

Run and request IDs must be lowercase 32-character hexadecimal strings;
completion IDs must be `fbr1_` followed by 32 lowercase hexadecimal characters.
The creation document is the exact `{"version": 1, "input": ...}` envelope.
Cancellation requires version 1, the original `request_digest`, `repository_id`,
and both `project_id` and `component_id`; use explicit nulls for both when no
project component applies. It settles delayed admission and does not cancel an
already admitted run. Reading its decision is always a separate read command.

The restore document contains its saved `request_id`, the two expected digests
and the selected tagged `source` described above, with no version field. Candidate
discovery never selects a source or requests restoration. Restore submission
can queue continuation; ticket acceptance does not prove execution success.

For example, submit a previously retained creation envelope, then choose exact
readback as a separate action if the response is uncertain:

```shell
forge request-run-creation "$REQUEST_ID" saved-run-envelope.json
forge run-creation-request "$REQUEST_ID"
forge run-creation-decision "$REQUEST_ID"
```

Neither surface automatically retries, reads after a write, allocates a
replacement ID, falls back to legacy `run`/`create_run`, observes a remote result,
or consumes a response. A 404 or transport failure leaves the outcome unresolved.
The four read tools advertise read-only, idempotent behavior; the three explicit
write tools conservatively advertise non-idempotent behavior to avoid prompting
automatic retries. Cancellation additionally advertises its destructive effect.
All seven descriptions are packaged native Agentic YAML templates. Existing
tools and their description bytes remain unchanged. CLI prints the returned JSON
and MCP returns its structured dictionary; neither preserves HTTP serialization
or independently verifies receipt cryptography. Local adapter tests establish
wire behavior, not live-provider or execution acceptance.

## Register a repository

```json
{
  "name": "Meeting rooms",
  "url": "https://github.com/your-org/meeting-rooms.git",
  "provider": "github",
  "default_branch": "main"
}
```

Provider values are `git`, `github`, `gitlab`, `bitbucket`, `azure` and `gitea`.
Repository registration does not establish clone or publication permissions.

## Start a local run

```json
{
  "repository_id": "the-id-returned-by-registration",
  "brief": "Build the meeting-room reservation service described in our brief.",
  "operation": "build",
  "backend": "pyfly",
  "frontend": "none",
  "agent": "codex",
  "commands": [{"argv": ["make", "check"], "timeout": 120}],
  "publish": false
}
```

Backend values are `pyfly`, `larafly`, `java`, `rust` and `dotnet`. Frontend values
are `react`, `angular`, `nextjs`, `vue` and `none`. Agent values include `codex`,
`claude`, `agentic` and `bridge`. Consult `/profiles` and `/providers` before
selecting a combination.

Commands require non-empty argument arrays and a timeout between 1 and 3600
seconds. Empty commands ask forge to discover verification from real project
files. There must still be an executable verification gate.

## Follow evidence and errors

A run response reports its ID and status. Poll `/runs/{id}` to obtain events,
verification, changed files, diff, artifacts, error details and any pull-request
URL. Do not infer success from HTTP acceptance of the queue request.

### Read a human question's screenshots

`GET /api/v1/runs/{id}/worker-evidence?request_id=QUESTION_ID` lists the raster previews retained for that exact question. Each record contains an opaque 64-character `id`, a bare `filename`, `mime_type`, byte count in `bytes`, and `sha256`. A question without recorded screenshots returns an empty list. This read does not generate images or reopen source-control files.

`GET /api/v1/runs/{id}/worker-evidence/{image_id}` returns only that retained image after current authentication, project/repository authorization and native journal checks. It returns `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff` and `X-Content-SHA256`. The body must match the listed MIME type, exact size and SHA-256. SVG, paths supplied by callers and public access tokens are unsupported. These endpoints are read-only; they do not answer the question or resume execution.

The SDK validates metadata, bounds streamed bytes and verifies the returned image before handing it to the caller:

```python
images = client.worker_evidence(run_id, request_id)
if images:
    image_bytes = client.worker_image(run_id, images[0])
```

See [answering an agent's question](agent-lifecycle.md#answer-an-agents-question) for roles, preview controls, waiting across restarts and answer reconciliation. Images remain worker evidence rather than a delivery approval.

### Interpret verification and recovery

Automatic repairs record an `attempt` number on each command result. The full
`verification` list preserves failed and successful attempts; `final_verification`
contains the checks supporting a successful run, and `source_tree` identifies
their source snapshot. Publication verifies that the workspace still matches
that snapshot. The [operations guide](operations.md) explains the bounded repair
budget and data-directory and PostgreSQL schema ownership.

Invalid input is rejected; a rejected token requires correcting authentication.
Provider limits can pause a retained task; unfinished effects can require reconciliation. Inspect `pause`, `display_status`, plan and journal before continuing. Resume takes `{}`; handoff takes `{"agent":"claude"}` and captures new configured access. Retry creates a separate
attempt and preserves the failed result. Cancellation is a request to stop active
work, not a rollback of external effects.

## Use the CLI

Set `FORGE_URL` to the server origin and supply `FORGE_API_TOKEN` in the client
environment. The CLI uses the same authenticated API:

```sh
export FORGE_URL="http://127.0.0.1:8787"
uv run forge repository list
uv run forge run --repository REPOSITORY_ID --brief 'Build the reservation service.' \
  --backend pyfly --frontend none --agent codex --check 'make check' --wait
uv run forge status RUN_ID
uv run forge export RUN_ID --output changes.zip
```

`forge doctor` reads local provider and profile configuration. `forge cancel`,
`forge retry` and `forge publish` operate on the named run. Publication is an
explicit remote write; export writes a new local file and refuses an existing
output path.

## Integrate a Python client

```python
import os

from firefly_forge.client import ForgeClient
from firefly_forge.models import Command, RunInput

with ForgeClient(os.environ["FORGE_URL"], os.environ["FORGE_API_TOKEN"]) as client:
    repositories = client.repositories()
    run = client.create_run(RunInput(
        repository_id=repositories[0]["id"],
        brief="Build the reservation service described in the product brief.",
        backend="pyfly", frontend="none", agent="codex",
        commands=[Command(argv=["make", "check"], timeout=120)],
        publish=False,
    ))
    print(run["id"], run["status"])
```

Register at least one intended repository before using this example. Queueing a
run does not wait for completion; poll `client.get_run(run["id"])` and inspect
verification before accepting the result.

## Read campaign delivery evidence

`GET /api/v1/projects/{project_id}/campaigns/{campaign_id}/report` returns a deterministic local evidence snapshot for one campaign. It requires current project and repository read permissions. SDK `project_campaign_report`, CLI `forge campaign report` and MCP `get_project_campaign_report` use this same read route. See [campaign evidence reports](project-campaigns.md#read-a-campaign-evidence-report) for states, exact task/checkpoint references, the digest contract and snapshot limits. The read performs no source-control refresh or execution.

## Give an external agent MCP access

`forge mcp` runs the standard MCP stdio transport. Configure your MCP client to
launch that executable with `FORGE_URL` and `FORGE_API_TOKEN` in its environment.
The tools list repositories, register a source, create a local run, inspect
evidence, cancel work and explicitly publish a verified run. MCP uses the same
authenticated API and its identity and project permission checks. Keep
publication within the authorized user's explicit intent.

For an eligible queued command after controller replacement, use the
[explicit runner continuation](sandbox.md#continue-one-queued-command-after-a-controller-restart).
Its request ID must be saved before submission. SDK, CLI and MCP do not retry
an uncertain response. A completed continuation admission does not establish
command success; inspect the original command and run receipts.

## Configure connections and project defaults

Provider configuration accepts `{"values":{"model":"MODEL","api_key":"PRIVATE_KEY"},"clear_secrets":[]}`. Field names vary by provider and are listed in `/providers`. Secret values are write-only; GET responses report presence. Empty secret fields retain a saved value; `clear_secrets` explicitly removes it. Queued runs retain an immutable private configuration revision.

Project policies accept `{"skills":["implementation","verification","security"]}`. Omitting `skills` when creating a run uses project defaults; an explicit empty array selects no optional policies. Core framework integration, source safety and verification cannot be disabled.

See [connected accounts](connected-accounts.md#api-and-client-contract) for typed service policy, client registration, browser consent and effect receipts; [SCM](scm.md) for repository discovery, OAuth redirects and grants; [knowledge](knowledge.md) for source selection and project scopes; and [deployment](deployment.md) for target commands, source checks and rollback requests.

## Recover a projection observation

When an observation response is lost, read its original request ID. This GET
reads the local immutable event; it does not contact flycanon, submit a source
or repeat the observation. A `404` means that exact request has no recorded
observation. Another request's verified projection does not establish success.

```python
with ForgeClient(factory_url, access_token) as client:
    receipt = client.learning_projection_observation_receipt(record_id, request_id)
```

The same read is available as `forge learning observation-receipt RECORD_ID
--request-id REQUEST_ID` and the MCP tool
`get_learning_projection_observation_receipt(record_id, request_id)`. Each uses
the authenticated API and current project review permission. The MCP read is
marked read-only and idempotent.

The typed receipt separates the original `event_state` from
`current_record_state` and `current_projection_state`. New observations retain
the original actor, projection, submission request, workspace, target revision,
payload digest, content hash and citations. Historical fields that were never
recorded remain `null`; they are not reconstructed from today's projection.
A later credential registration can block readback of retained values. Neither
a missing receipt nor an unknown legacy field authorizes resubmission.

The projection status GET also exposes its authoritative `payload_digest` for
comparison with the original admitted envelope. Source observation checks the selected
source ID against current credentials before contacting flycanon, verifies the
returned source ID and indexed content, then checks the retained admission
identity again before recording the event. It does not claim that remote
indexing and the local transaction are atomic.

## Read optional structured analytics

Flyquery is optional. Flycanon remains the required knowledge and memory service.
Install the `analytics` extra of the qualified Forge package to make the official
Flyquery 26.10.4 read SDK available. An unconfigured project does not import that SDK
or contact Flyquery.

An authenticated factory administrator binds one existing project to an HTTPS
origin, tenant, workspace and dataset. The origin must use port 443 with a trusted
certificate; custom ports and private CA configuration are unsupported in this
SDK slice. Use a scoped Flyquery agent token with the upstream schema-read grant,
not a user login token. Remote authorization still applies to every read.

| Method | Path under `/api/v1/projects/{project}/analytics` | SDK method |
| --- | --- | --- |
| GET/PUT | `/configuration` | `analytics_configuration` / `configure_analytics` |
| GET | `/catalog` | `analytics_catalog` |
| GET | `/tables/{table}` | `analytics_table` |
| GET | `/tables/{table}/objects/{object_id}` | `analytics_schema_object` |
| GET | `/prepared/{query}` | Unavailable; refuses without contacting Flyquery |

Read configuration first. Use its exact `revision` as `expected_revision` when
saving; use zero only for the first binding. Every save supplies the complete
scope and write-only `agent_token`, including a save with `enabled: false`.
A conflicting revision is refused. There are at most 32 ordinary configuration
changes per project. An enabled revision 32 permits one final disable at revision
33, using `expected_revision: 32`, `enabled: false`, the unchanged endpoint, tenant,
workspace and dataset, and an explicitly supplied token. This terminal binding
cannot be edited or re-enabled. A connection already disabled at revision 32 has
no further changes. Disabling does not import the SDK or contact Flyquery. Each
committed change has an immutable native receipt. Credentials stay
in owner-private revision files, never in public configuration or native receipts.
`configured` means settings were saved; it does not mean remote access was tested.

In Studio, open **Administration → Structured analytics** and choose a project.
A factory administrator can select **Set up Flyquery** or **Edit connection**:

1. Confirm the existing Flyquery service, optional Forge analytics SDK and
   workspace-scoped `flyquery.schema:read` token are available.
2. Enter the HTTPS origin, tenant, workspace UUID and dataset UUID. Enter a token
   for this save; the form never loads a saved token. Clear the enable checkbox
   to disable reads, supplying the complete scope and token again.
   At revision 32, **Disable connection** locks the existing scope and permits
   only the terminal disable. Disabled revision 33 remains readable.
3. Review the scope and expected revision, then select **Save connection** once.
   The browser clears the token on submission. A missing or inconsistent response
   leaves the save unconfirmed; explicitly reload the saved configuration before
   another save. Reloading is a current read, not proof that an earlier request
   can no longer settle. Revision checks still reject conflicting writes.

Project readers use **Read catalog**, choose a table to read its metadata, then
optionally supply a known schema-object UUID. There is no automatic remote read,
polling, retry or connection-test endpoint. Each browser request has a 15-second
deadline; a browser timeout does not prove a save was rolled back. The explorer
shows the observed scope, configuration revision, time and response digest. It does
not list all fields or enable unavailable prepared-result and query features.

```python
import os
from firefly_forge.client import ForgeClient

with ForgeClient(os.environ["FORGE_URL"], os.environ["FORGE_API_TOKEN"]) as forge:
    project = os.environ["FORGE_PROJECT_ID"]
    current = forge.analytics_configuration(project)
    forge.configure_analytics(project, {
        "expected_revision": current["revision"],
        "enabled": True,
        "endpoint": "https://analytics.example",
        "tenant_id": "fictional",
        "workspace_id": os.environ["FLYQUERY_WORKSPACE_ID"],
        "dataset_id": os.environ["FLYQUERY_DATASET_ID"],
        "agent_token": os.environ["FLYQUERY_AGENT_TOKEN"],
    })
    catalog = forge.analytics_catalog(project)
```

Project readers can inspect the bound catalog and exact table/schema-object UUIDs.
Browser headers cannot override the remote scope. Reads reject mixed scope,
incomplete pages, changed configuration, revoked access and changed credentials.
There is no retry or write fallback on 404 or transport failure.

The catalog is limited to 50 tables and one complete page. Exact schema lookup
requires a known object UUID and its parent table's reported current snapshot;
full table-field discovery is unavailable. Each response is limited to 256 KiB,
with at most eight GETs and a ten-second remote deadline (three-second connect).
Current operations use one GET, or two for an exact schema object. One read per
project/actor is active in each API worker. Observations include the local response
digest and observation time. Snapshot UUIDs are reported identity, not data-content
hashes or a guarantee of a current atomic data cut.

Prepared results require a new upstream agent-token GET query-history/result API,
exact tenant/workspace/dataset fencing and corresponding read SDK methods. The
current user-tier query/result routes are not a substitute. No SQL, query
generation, result download URL, model call, agent tool, CLI/MCP command or memory
publication is added by these routes.
