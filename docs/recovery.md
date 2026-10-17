---
description: Recover paused and interrupted runs with persisted task state, explicit reconciliation and preserved source evidence.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Recover paused and interrupted work

Use the durable plan and journal to determine what happened before continuing a run. The same run can retain partial source, provider and policy snapshots, earlier failed checks, a real checkpoint commit and remote-effect receipts.

## Settle an uncertain run submission

Run creation has a separate identity from the run it may admit. `POST /api/v1/run-creation-requests/{request_id}` accepts the original `{"version":1,"input":...}` envelope under a retained 32-character hexadecimal request ID. A new admission returns HTTP201; an exact replay by the original user and authentication method returns HTTP200 with the unchanged receipt. The legacy `POST /api/v1/runs` route remains a separate operation.

The receipt binds the original input, actor and admitted run. Read it with `GET /api/v1/run-creation-requests/{request_id}` after a lost response. Its admitted status is historical; open the referenced run to inspect its current state. A timeout, refusal or missing receipt alone cannot establish that no run was created.

If the original request must be abandoned, POST `/api/v1/run-creation-requests/{request_id}/cancel` with only its version, original input digest and scope:

```json
{
  "version": 1,
  "request_digest": "the-64-character-original-input-digest",
  "repository_id": "the-original-32-character-repository-id",
  "project_id": null,
  "component_id": null
}
```

For an assigned component, supply both its original project ID and component key. The server requires the same enabled native user and current read access to that scope. A different session authentication method for that same user may settle cancellation; it cannot replay the original admission under a different method. Unassigned requests require a factory administrator.

Admission and cancellation share a request fence. When cancellation wins, the immutable `cancelled` decision prevents even a delayed original POST from admitting work. When admission has already won, cancellation returns `admitted` with the existing version-1 receipt. It does not stop that run; use the run's separate cancellation operation if required.

Read `GET /api/v1/run-creation-requests/{request_id}/decision` after an uncertain cancellation response. It returns the original admitted receipt or the immutable cancellation. HTTP404 leaves the request unresolved. Release a held form only after verifying its exact terminal decision; preserve current edits, and review them before explicitly submitting a new request.

A small ownership reservation commits before preparation, so cancellation can settle while source checks are still running. Reservations and cancellations store digests and scope, without duplicating the raw brief or commands. A user may hold at most 50 unresolved reservations; admitted and cancelled requests do not count. This limit never blocks cancellation. The additive schema-28 migration preserves existing version-1 admission receipts without synthesizing historical reservations.

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

This helper refuses unfinished tools, retries, vision, deferred or changing tools, dynamic instructions and unsupported message parts. It has no configuration switch or public endpoint. Engine activates the supported implementation slice only through the explicit checkpoint setting described below. Assurance and Product Assistant do not activate it.

### Internal checkpoint storage

The native `AgentCheckpoints` service can retain a captured boundary beside its unchanged Bridge request intent in one PostgreSQL transaction. An exact admission replay must match both records. A failed checkpoint append rolls back the new intent and hold as well. Records use the existing immutable journal table; this adds no SQL migration or historical backfill.

Version-1 consumption requires the original retained successful HTTP response and its exact request, checkpoint, wire digest and task attempt. Authenticated remote terminal evidence cannot substitute for that version-1 HTTP record. The service commits a single consume receipt before its response callback succeeds. A second consumption is refused, including after cancellation or interruption. A failure after the consume commit can leave a consumed boundary without delivering its response; it cannot re-arm that boundary. Exact receipt reads return checkpoint and consume evidence without returning the completion body.

Checkpoint payloads are limited to 192 KiB, journal envelopes to 256 KiB and consume envelopes to 4 KiB. Each run permits at most 100 checkpoints and 4 MiB of checkpoint/consume evidence, with pending consume space reserved inside the shared 16 MiB completion budget. Scalar SQL bounds run before loading checkpoint JSON. Admission checks current execution authority, current reader access when present, owned run/source/configuration and task state. Credential admission occurs outside native transactions; current authority and credentials are checked again before commit and return. Generic journal reads also admit the new checkpoint payloads under their existing reader fence.

Checkpoint operations prepare an immutable credential registry outside native units. Locked rejection scans that registry and checks a process-local configuration-write cut without taking a file lock or reading settings. A local configuration write invalidates the cut and refuses the writer transaction. External credential sources are read again outside the transaction before exposure; a refusal after consumption remains consumed. This does not establish atomic cross-process configuration admission.

Without explicit checkpoint selection this storage service remains unused by execution. Reading a terminal result or checkpoint never resumes a graph or enables generic resume, handoff or Retry.

### Explicit closed-text implementation continuation

New Bridge implementations may select `checkpoints: required` together with `completion_recovery: required` in saved provider configuration or an implementation model profile. These optional fields freeze into the new run; absent fields leave historical snapshots and defaults unchanged. The initial slice requires an explicit static grant containing only `list_files`, `read_file`, `write_file`, `search_text`, `edit_lines` and `git_diff`. Unsupported grants, streaming, vision, QA state, deferred tools, retry state, dynamic instructions or dynamic tool catalogs refuse before completion submission. Direct API providers and independent review do not support required checkpoints; use a separate review profile with checkpoints disabled.

In Studio, open **Connections → Coding AI → Bridge → Configure**. Under **Original model results**, select **Required: retain the original result**, then **Required: save implementation checkpoints**. Saving these settings does not test the connection or change an existing run. **Keep current default (unset)** preserves an absent field; the other choices explicitly save `legacy`/`required` recovery or `disabled`/`required` checkpoints. The equivalent provider API is `PUT /api/v1/providers/bridge/configuration` with `values` containing the selected fields. Named implementation profiles use `PUT /api/v1/model-profiles/{id}`.

#### Review a held result in Studio

1. Open the held Bridge run and choose **Plan → Original model result**. The panel requires current project execution access and, for a publishing run, release access. It reads saved checkpoint references without contacting Bridge or continuing work.
2. Select the original implementation attempt. Check its saved time, original deadline and result state. **Check original result** requests an observation of that original Bridge completion when its result still needs checking.
3. When the original result is saved, choose **Review original result**. Read the continuation steps and acknowledge that the result is consumed once. **Continue from original result** admits that specific continuation; verification and delivery retain their separate outcomes.
4. If a response is lost, choose **Read exact saved request**. Studio retains the observation or continuation identity in this tab before submission and keeps it across reload. A missing receipt or access error holds that identity. A recorded continuation discovered on the server can also be read without submitting another one.

Storage must be writable and readable before a new request can be sent. Changing the factory, account, run or current permission context resets the displayed surface and fences late responses. A new browser session can discover server-recorded continuation tickets; an observation or request that never reached the server still depends on its original tab marker. The panel shows references and states, not the model's private history or response body.

An **Original implementation receipt recorded** message confirms the original implementation receipt. Check the run's verification, any later repairs and delivery records before accepting the run. A consumed result cannot be re-armed after a later failure.

The real Agentic implementation captures a closed native message history at the exact HTTP request boundary. Version-2 checkpoint envelopes bind the original composed instructions, runtime and prompt contracts, cumulative native usage and limits, absolute deadline, task attempt, file-tool state and retained owned workspace. The original Bridge intent and checkpoint commit together before POST. Both ordinary and restored results commit a consume receipt before reaching the native parser. Version-1 checkpoints and HTTP-only consume receipts retain their original semantics and cannot authorize restoration.

An authorized operator first selects an immutable original HTTP result or retains a successful original-result observation through the existing `POST /api/v1/runs/{run_id}/bridge-requests/{completion_request_id}/observations` route. Then POST `/api/v1/runs/{run_id}/agent-checkpoints/{completion_request_id}/restores` with a retained 32-character hexadecimal `request_id`, `expected_checkpoint_digest`, `expected_intent_digest` and a tagged `source`. An `http_response` source has its `record_digest`; a `bridge_observation` source also has `observation_id` and the original `terminal_digest`. The server does not accept an arbitrary response body. GET the same route with `/{request_id}` for exact readback; a missing or uncertain response never authorizes another admission. Each checkpoint permits one ticket, within a maximum of 50 restore tickets per run and the shared bounded worker queue.

Discover completion request IDs through `GET /api/v1/runs/{run_id}/bridge-requests` and read the selected intent at `/{completion_request_id}`. The authenticated run journal (`GET /api/v1/runs/{run_id}/journal`) contains the matching `agent_checkpoint`, any `bridge_request_response` and their immutable record digests. Select the version-2 checkpoint whose idempotency key matches that request; its record digest is `expected_checkpoint_digest` and its payload binds `expected_intent_digest`. For an observation source, retain the observation POST response and its successful original terminal digest. Keep the client-generated restore request ID before POST, then read that exact ticket after a lost response; do not generate a replacement ID.

The execution owner claims that ticket once, repeats current operator and execution-actor authority, source/configuration and credential checks, and enters the same unfinished implementation attempt. It calls public `FireflyAgent.run(prompt=None, ...)` with retained native history and usage. The first reconstructed request must match its original wire bytes and receives only the selected original result, with zero replacement POSTs. An authenticated remote observation stays a distinct result source; it is never relabeled as an original HTTP response. Earlier file tools are not executed again. Genuinely new tools and requests may follow, each new request retaining its own checkpoint. Successful continuation rejoins the ordinary implementation receipt and verification path.

The original deadline includes downtime. Changed or missing workspaces, unsupported state, current permission loss, cancellation and stale claim generations refuse. This requires the same retained local workspace; it does not reconstruct source on another host or make filesystem checks atomic against unrelated local processes. A workspace that cannot be treated as exclusively owned is ineligible. A crash after consumption, including before delivery to the parser or before the next checkpoint, cannot re-arm that boundary. Executing tickets recovered after owner loss need reconciliation and are never automatically requeued. Generic completion holds remain in place, including for old unknown runs without an eligible checkpoint. This is continuation at the supported model boundary, not arbitrary graph or Python-stack restoration.
