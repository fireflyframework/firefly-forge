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
A conflicting revision is refused. There are at most 32 configuration changes per
project; each committed change has an immutable native receipt. Credentials stay
in owner-private revision files, never in public configuration or native receipts.
`configured` means settings were saved; it does not mean remote access was tested.

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
