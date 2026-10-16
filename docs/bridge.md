---
description: Connect forge to a local Firefly Agent Bridge for development testing with explicit provider, access and execution boundaries.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Connect a local agent bridge

Firefly Agent Bridge runs on the machine with your official Codex or Claude Code subscription login. Get its source and installation instructions from the
[Firefly Agent Bridge repository](https://github.com/fireflyframework/firefly-agent-bridge).
Install and operate it separately from forge, then issue a scoped bridge token for the factory's application connection. Bridge execution is intended for local development and testing; provider account terms and subscription limits still apply.

forge includes a client connection to an OpenAI-compatible bridge endpoint.
The bridge runs separately and authenticates its own clients. Configure the
forge server with the endpoint and an authorized bridge token:

```sh
export FORGE_BRIDGE_URL="http://127.0.0.1:8766"
export FORGE_BRIDGE_TOKEN="your-authorized-bridge-token"
export FORGE_BRIDGE_MODEL="bridge/default"
```

Use HTTPS for a remote endpoint. Keep the token in the server environment,
or save it through Studio’s write-only provider configuration. Never put it in Git URLs or generated products. Select **Firefly Agent Bridge**
as the execution agent for a run.

forge connects Firefly Agentic's workspace tools to the configured compatible
endpoint. The bridge does not need direct access to forge's local repository.
Its model output still passes through the factory's ordinary generation and
verification stages.

The catalog distinguishes configured access from a successful connection test. The test authenticates a model listing; a real run establishes generation and workspace tool behavior. If the
endpoint rejects a request, inspect the run error and ask the bridge operator
to verify the authorized configuration.

forge also supports direct [Codex, Claude Code and native Firefly Agentic](agents.md) connections. Choose the provider and execution boundary that meet the project's requirements.

## Verify image input for the selected configuration

Image input requires a successful bounded verification for the bridge adapter's
current configuration and software version. A model listing exposes that
receipt; forge reads it for the conversation's frozen provider. Listing models
does not itself run the verification or invoke a model.

An adapter update can change its configuration revision. Keep historical
receipts with the conversations that used them, and verify the updated adapter
before selecting it for new image requests. A saved image or an earlier
successful answer does not establish current capability. If the bridge is
unavailable, forge reports capability as unverified until it can read the
current receipt again. Restore the connection before repeating a model request.

## Retain each completion for exact observation

Completion recovery is an explicit, frozen model choice. Set
`completion_recovery` to `required` in the Bridge provider configuration or a
named model profile, or set `FORGE_BRIDGE_COMPLETION_RECOVERY=required` before
queuing a new run. The default for existing configurations is `legacy`; an
absent choice adds no field to an old snapshot. Editing a saved configuration
never changes a queued run's revision. The provider catalog adds this optional,
nonsecret choice; existing SDK and MCP input schemas remain unchanged.

Required mode needs a Bridge with version 1 completion-recovery capability.
forge checks that capability with the selected authenticated connection and
refuses before submitting a completion when it is unavailable. Legacy mode
keeps its existing request format and does not claim durable correlation.
Direct product-assistant conversations do not implement this run-owned
recovery contract and cannot use required mode.

For each actual model request, including another tool cycle, forge creates a
separate identity, commits its native journal intent, and reserves bounded
response storage before POST. The intent binds the task attempt, original
execution actor, project, repository, source cut, and coding or review model
revision. It stores the exact wire-byte SHA-256 and size, not the prompt or
credential. That hash is separate from the Bridge's native prepared-request
fingerprint. SDK automatic retries are disabled, and HTTP redirects are
independently disabled. Neither control substitutes for the other.

Required-mode transport responses are buffered up to 256 KiB and pass current
credential and execution admission before the model can consume them. A native
`bridge_request_response` is transport evidence; the authenticated exact Bridge
read supplies the sealed original terminal. A successful HTTP response does not
prove that the Agentic graph or forge task completed.

Use the REST evidence routes under `/api/v1`:

| Operation | Route |
| --- | --- |
| List intent metadata, up to 50 entries | `GET /runs/{run}/bridge-requests?after={sequence}` |
| Read an exact local intent | `GET /runs/{run}/bridge-requests/{request}` |
| Retain one authenticated observation | `POST /runs/{run}/bridge-requests/{request}/observations` |
| Recover that exact local observation | `GET /runs/{run}/bridge-requests/{request}/observations/{observation}` |

The observation input contains `version: 1`, a caller-retained 32-character hex
`observation_id`, and `expected_intent_digest`. Keep that identity before the
POST. The server performs one exact Bridge GET, validates its admission and
terminal digests, and retains the original result when available. It never
submits inference. A repeated observation identity cannot select another
intent or actor. After a lost local response, use exact GET; a 404 does not
authorize another POST, a new identity, or another model request. A late access
refusal can follow a committed observation, so it does not establish that
nothing was written.

Current project and repository access protects every read. New observations
also require current execute permission and release permission for a publishing
run. Current credentials must keep the original endpoint, model, and Bridge
principal. Credential rotation does not rewrite old model revisions or evidence.
An original terminal can remain `succeeded` while the mutable Bridge job has a
later state; both fields are retained separately. Quota and context observations
report only recorded facts. They do not establish available subscription quota
or a known reset time.

`completion_recovery_full` is a known remote `admission_refused` fact with the
original completion identity. forge retains a separate manual
`bridge_admission_refused` hold. It does not infer subscription exhaustion,
inherit a reset from `Retry-After`, or classify this refusal as unknown remote
work. Other uncertain outcomes keep their existing reconciliation holds.

This core provides REST evidence routes and native callbacks for Engine and
independent review. Dedicated Studio, SDK/CLI, and MCP recovery controls remain
outside this cut; product-assistant recovery is also unavailable.

This cut recovers evidence only. The first admitted completion also retains
`bridge_completion_hold` because forge has no durable Agentic message-history
checkpoint for graph reentry. Neither a terminal result nor an observation GET
clears it. Resume, provider handoff, automatic resume, and Retry remain blocked,
including after cancellation or interruption. Legacy unknown runs without a
correlation identity stay held; forge does not search for a similar job or
reconstruct a graph from its brief.

Retention has hard limits: 100 intents per run, 9,000,000 request wire bytes,
256 KiB per transport response or remote observation envelope, 10,000 journal
rows, 16 KiB per intent, and 16 MiB for the bounded journal plus pending response
reservations. Each unresolved transport response reserves 520 KiB; a retained
response consumes that reservation. The byte budget can refuse an admission
before the intent-count limit. No evidence is evicted to make room. Database
transactions retain ordered membership, identity, and run locks through commit;
provider and credential-service awaits happen outside those transactions.
