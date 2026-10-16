---
description: Recover paused and interrupted runs with persisted task state, explicit reconciliation and preserved source evidence.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Recover paused and interrupted work

Use the durable plan and journal to determine what happened before continuing a run. The same run can retain partial source, provider and policy snapshots, earlier failed checks, a real checkpoint commit and remote-effect receipts.

## Read the records

`GET /api/v1/runs/{id}/plan` returns the current plan revision and task states. `GET /api/v1/runs/{id}/journal` returns its hash-chain entries and a validity flag. Each intent identifies its task and attempt; each receipt records the outcome actually observed.

| State | Meaning | Action |
| --- | --- | --- |
| `paused`, rate limit | Provider access was throttled | Wait for an allowed bounded retry, or explicitly resume later |
| `paused`, quota | The provider reported exhausted credits or quota | Resolve it at the provider or hand off explicitly |
| `paused`, authentication | The selected access failed | Fix access and choose a fresh configured provider snapshot through handoff |
| `interrupted` | The worker stopped before completion | Inspect the retained workspace and task journal |
| `needs_reconciliation` | An intent lacks a trustworthy completion receipt | Establish the actual local or remote outcome first |
| `failed` | The implementation or verification did not complete successfully | Inspect failure evidence; a fresh retry creates a separate run |

A task marked done has a completion receipt. Inspect its evidence: completing a verification command can record a failing exit status. Overall success requires the final passing checks and matching source snapshot.

The [Studio walkthrough](console-tour.md#4-inspect-the-run-before-continuing) shows a fixture failed check and unresolved intent, with the continuation controls in context.

## Resume the same run

Send `POST /api/v1/runs/{id}/resume` with `{}`. Resume preserves the workspace, original brief, policy content and provider revision. Local reconciliation inspects the Git root, authorized origin, HEAD and source tree before continuing an unfinished task.

```json
{}
```

A completed prepare task does not clone the repository again. Completed implementation work remains in place. A failed check can authorize a new bounded repair attempt, with its previous evidence recorded before the next worker call. Retrieved knowledge context and its digest remain available to the continuing worker.

## Hand off to another configured worker

Send `POST /api/v1/runs/{id}/handoff`:

```json
{"agent": "claude"}
```

Handoff is explicit. It creates a new provider configuration snapshot and immutable plan revision, retains earlier revisions, and continues the existing project source. It does not delete partial files, weaken checks or change the frozen project policies.

Test the new provider before handing off. A fresh snapshot can also renew authentication for the same provider. Quota and authentication pauses are not automatically bypassed by selecting another account.

## Reconcile remote work

Publication, deployment, rollback and knowledge writes have separate remote task intents. An unknown remote outcome cannot be retried through generic local reconciliation. Inspect the corresponding provider for the recorded branch, job, source or knowledge version. Retain its identity and outcome as evidence before deciding on the next operation.

Retry uses a fresh run and clone; it is separate from resume. It preserves the failed run's history. A new run does not roll back any external effect from the earlier run.

### Reconcile worker delivery requests

Read `GET /api/v1/runs/{id}/execution-requests` before repeating publication or cancellation. Each request retains its admitted source, actor, status and hash-chain events. A worker records dispatch before the effect and its observed result afterward. An interruption between those records leaves `needs_reconciliation`.

An authorized operator can ask the server to reconcile the exact request:

```sh
forge execution-requests RUN_ID
forge reconcile-execution-request RUN_ID REQUEST_ID \
  --reconciliation-id 0123456789abcdef0123456789abcdef
```

The HTTP body contains only `{"reconciliation_id":"0123456789abcdef0123456789abcdef"}`. The caller cannot assert success or absence of an effect. The server rechecks current project authority and matches retained evidence to the admitted and current source. Publication requires a terminal worker receipt or the matching immutable publication intent and receipt, including its branch, checked source and pull-request URL. Mutable task status or run metadata alone cannot establish publication.

If no matching proof exists, the request stays unknown and the server returns HTTP409. Reconciliation makes no provider call and never repeats the effect. Reusing the same reconciliation ID returns its retained result; a different ID for an already reconciled request is rejected. Cancellation also requires a terminal worker receipt proving that work drained.

A retained rejection before dispatch can settle a request as failed. Publication then permits an explicit bounded retry, up to three admitted attempts, provided no earlier publication intent remains unresolved. Any failure after dispatch retains uncertainty until its terminal receipt is established, even if the provider's error sounds like validation or authorization.

## Understand automatic limits

Eligible rate-limit and transient pauses use their recorded delay and a budget of at most three automatic retries. Other pauses require an operator decision. Local concurrency is bounded globally and per project; work is selected across projects rather than filling both worker slots with one backlog.

The journal's hash chain detects a changed record sequence or payload. It is an integrity check within the operator's PostgreSQL ledger, not an external signature or protection against a database administrator replacing the entire history.

## Recover a product conversation

Product-design turns have their own persisted ownership records. A turn's `ownership` includes its process owner, `claim_epoch`, lease expiry and last heartbeat. The active process renews its lease against PostgreSQL's clock. Only that owner and epoch can retain a response while the lease remains valid. A different API instance can read the conversation without interrupting it.

An expired turn stays `processing` until an editor explicitly recovers it. Inspect the current turn and use its `recovery.expected_epoch`:

```sh
forge assistant get REPOSITORY_ID CONVERSATION_ID
forge assistant recover REPOSITORY_ID CONVERSATION_ID TURN_ID \
  --request-id 0123456789abcdef0123456789abcdef --expected-epoch 1
```

The server locks the conversation and turn, verifies expiry using database time, increments the epoch and records an immutable recovery receipt. The turn becomes `needs_reconciliation`. A late reply from the former owner cannot replace this result. Repeating the same recovery ID and body returns the original receipt; a changed body is rejected. No model retry occurs.

Processing turns created before ownership tracking have epoch zero and no owner. They require `--legacy-confirmed-stopped` as an explicit assertion that their original process has stopped. This assertion is recorded separately from an expiry proof. Do not use it while the original request may still be running.

Graceful factory shutdown stops new dialogue admissions, cancels and drains owned requests, then closes the data layer. An abrupt process loss leaves the durable claim for inspection and explicit recovery. An unknown provider outcome remains unknown even when local ownership has been fenced.

## Agentic model-boundary development

The internal `AgenticBoundary` helper supports an explicit, in-memory model-request boundary using Agentic's public capabilities and typed message codec. It captures closed text history, cumulative native usage and limits, model/tool settings, written-file names and the exact HTTP-body digest. Restoring that boundary uses `prompt=None`; it does not append the original brief again. The pending request keeps its recorded timestamp through the public history objects.

The first restored request must match the original body and state. Its original response passes a one-use consumption callback before reaching the ordinary SDK parser. Earlier tool calls stay in history; genuinely new tool calls and model requests may follow. An invocation-local shared context keeps this distinction across native task boundaries.

Checkpoint messages must have complete native lifecycle states. Usage and numeric limits must be finite, nonnegative and bounded; integer counters cannot exceed `2^53 - 1`. Payloads and responses use immutable bytes, and the response returned after consumption is the exact byte sequence whose digest was consumed.

This helper refuses unfinished tools, retries, vision, deferred or changing tools, dynamic instructions and unsupported message parts. It has no configuration switch or public endpoint. Engine, Assurance, Product Assistant and operator clients do not activate it.

### Internal checkpoint storage

The native `AgentCheckpoints` service can retain a captured boundary beside its unchanged Bridge request intent in one PostgreSQL transaction. An exact admission replay must match both records. A failed checkpoint append rolls back the new intent and hold as well. Records use the existing immutable journal table; this adds no SQL migration or historical backfill.

Consumption requires the original retained successful HTTP response and its exact request, checkpoint, wire digest and task attempt. Authenticated remote terminal evidence cannot substitute for that HTTP record. The service commits a single consume receipt before its response callback succeeds. A second consumption is refused, including after cancellation or interruption. A failure after the consume commit can leave a consumed boundary without delivering its response; it cannot re-arm that boundary. Exact receipt reads return checkpoint and consume evidence without returning the completion body.

Checkpoint payloads are limited to 192 KiB, journal envelopes to 256 KiB and consume envelopes to 4 KiB. Each run permits at most 100 checkpoints and 4 MiB of checkpoint/consume evidence, with pending consume space reserved inside the shared 16 MiB completion budget. Scalar SQL bounds run before loading checkpoint JSON. Admission checks current execution authority, current reader access when present, owned run/source/configuration and task state. Credential admission occurs outside native transactions; current authority and credentials are checked again before commit and return. Generic journal reads also admit the new checkpoint payloads under their existing reader fence.

Checkpoint operations prepare an immutable credential registry outside native units. Locked rejection scans that registry and checks a process-local configuration-write cut without taking a file lock or reading settings. A local configuration write invalidates the cut and refuses the writer transaction. External credential sources are read again outside the transaction before exposure; a refusal after consumption remains consumed. This does not establish atomic cross-process configuration admission.

This storage service is registered in native dependency injection and remains unused by execution. Durable graph restoration, restore tickets, configuration activation and Engine/Assurance wiring require a later implementation and native qualification. Current Bridge completion holds remain in place. Reading a terminal result or a checkpoint never resumes a graph or enables generic resume, handoff or Retry.

## Internal run-admission boundary

Run admission prepares runner capabilities, provider settings, policies, roles and reviewed design files before taking the insertion locks. `Store.prepare_run` retains immutable bytes and original field presence. The locked insert checks the current native actor, project/component and repository/product cuts, then saves the queued run and its first event together. Campaign and incident admissions prepare before entering their own atomic write.

Credential matching uses the prepared registry and process-local configuration generation, with current native OAuth evidence checked inside the transaction. Fresh credential and authority checks also apply before returning the committed result. A refusal after commit does not establish that nothing was saved. These guards do not serialize configuration files written by another process.

This boundary provides no new public request identity or exact creation receipt. Ordinary `POST /runs` retains its existing contract; recovery of a lost creation response needs the separate original-request protocol. It must not be inferred from a matching brief or the latest run list.
