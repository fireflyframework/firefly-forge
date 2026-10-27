---
description: Continue a consumed, rejected evidence tool turn through a qualified same-attempt migration while preserving original history, custody and execution budgets.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Continue after a rejected evidence read

A consumed model response can stop an older required-checkpoint worker when its sole `read_run_evidence` call contains a query rejected before the evidence reader starts. Same-attempt continuity reconstructs that known negative outcome offline and prepares a new finite ownership lease. It preserves the original task, source, user, configuration, consumption, history, and execution limits.

This implementation is a private preview. A qualified and registered current profile covering the complete installed migration code is required before native admission. PostgreSQL and controller qualification must also pass before use on retained work.

## Exact eligibility

The candidate verifies the complete native journal and original request/checkpoint/response/consume closure. The old package runtime and pre-callback validator must match registered historical identities. The final original response must contain exactly one granted `read_run_evidence` function call with an object query accepted by the old advertised schema but rejected by its pinned validator.

A valid query, an unknown or writable call, sibling calls, unsupported response content, a missing consume, or later unaccounted effects cannot use this mechanism. A logged error message supplies no authority. Existing unresolved command or tool custody remains subject to its original native reconciliation and acknowledgement rules.

## Offline reconstruction and tool migration

A sealed in-process HTTP transport exposes the already consumed response to the public SDK parser once. It never contacts a provider or repeats native consumption. The only reconstructed tool outcome is `{"status":"invalid_query"}`; it invokes no original evidence-reader callback. Capture stops before any new request can leave the process.

The installed `execute_agentic` entry point is independently run against another sealed transport to capture its actual rendered tool definitions and instruction parts. Migration permits exactly these changes:

| Contract element | Approved successor |
| --- | --- |
| `read_run_evidence.parameters_json_schema` | Current strict worker envelope, with page default and maximum of ten |
| `read_run_evidence.description` | Registered prompt 1.0.0 to 1.0.1, explaining correction and checkpoint-safe `invalid_query` results |

Every other contract element must remain equal, including instructions, other descriptions and schemas, tool order and grants, return schemas, output contract, model settings, history, SDK usage, limits, and public compatibility versions. Unsupported changes refuse migration. Newly derived message metadata is deterministically bound to native consumption time and the original public message run identifiers; the stored historical prefix and raw response remain unchanged.

## Historical policy epochs

An older checkpoint-mode adoption and original-selection readmission remain immutable. A package-owned compatibility catalog recognizes only the reviewed historical selection/mode/acknowledgement tuple. Recognition exists only inside this namespaced continuity preparation or execution context, under the current qualified source profile and enabled finite-lease policy. The current identity, original execution actor, provider configuration, selection, source, commands, unknown-effect custody, and original scope are still checked.

Ordinary mode preparation and restore APIs keep their strict current-policy behavior outside that context. Existing restore APIs do not gain permission to replay consumed responses or restore expired claims. The migration receipt records both policy epochs and binds them to the exact old native references, current source profile, and current lease-policy revision.

## Operator flow

1. A current factory administrator configures `PUT /api/v1/invalid-read-continuity/policy`. The versioned policy is disabled by default. Its finite window and queue bounds are explicit settings. Read it back with `GET` on the same route.
2. A currently authorized execution operator reads `GET /api/v1/runs/{run_id}/invalid-read-continuity/candidate`. Inspect its proof, current qualification, policy revision, source and custody digests, and holds. Candidate preparation creates no execution claim.
3. Submit `POST /api/v1/runs/{run_id}/invalid-read-continuity/requests/{request_id}` with the exact candidate/run/journal/policy digests, a bounded `valid_for_seconds`, and all three acknowledgements: no new grant, original consumption, and renewed same-attempt lease.
4. Read the same request identity back after a lost response. An identical request is idempotent. Another identity cannot acquire a second ticket for the same consumed graph.

Candidate and admission source inspection use read custody under the current authenticated actor. They require an eligible held run, its exact task attempt and journal, unchanged source and configuration, and no queued or executing native claim. The API acquires no worker ownership. Worker execution retains the existing ownership checks, and ordinary restore admission keeps its original eligibility and deadline rules.

The native transaction appends one migration settlement and its adjacent ownership claim, then queues the migrated checkpoint through the existing execution-request repository. The old claim and deadline remain recorded unchanged. The new lease is bounded by the factory policy, frozen provider timeout, and any recognized remaining active deadline. Unsupported time-budget protocols refuse admission.

The lease changes ownership validity only. It adds no request, token, tool, source-reader, command, repair, or attempt allowance. Admission preserves the completed-round count; a later ordinary known worker completion advances progress through the existing Engine behavior. The next request uses the migrated checkpoint rather than a fresh brief or child run.

Queue counts default to 50 tickets per run and 500 pending tickets. An administrator may configure larger positive integer counts. Native admission enforces the configured counts atomically; changing queue capacity does not change any lifetime execution allowance. The 7200-second maximum lease window follows the existing provider-timeout configuration protocol and remains bounded by the original frozen timeout and any remaining active deadline.

Command usage is measured from canonical retained native execution rows. For example, a retained ledger at 11 of 22 remains at 11 during admission. Two newly executed commands bring it to 13 of 22, with the old eleven receipts unchanged. Reading the same completed command or ticket again adds no charge. The remaining nine commands may run under the original limit; a twenty-third command is refused before execution.

## Failure and validation boundaries

Policy revocation, expired ownership, changed source/configuration/profile/scope, cancellation, actor loss, unknown effects, or a changed native ticket stop execution. Claimed tickets are never automatically requeued after owner loss. Their outcome requires explicit native reconciliation.

Host controls cover the actual Engine and installed worker adapter, strict evidence correction, two new file-tool cycles, two durable command cycles, unchanged attempt and allowances, original-consumption reuse, idempotency, expiry, revoked authority, and incompatible policy or contract changes. Prepared PostgreSQL controls use only a dedicated fictional `_test` database and in-process model transport. Host tests do not establish PostgreSQL atomicity, current installed-profile qualification, controller composition, or acceptance of a particular retained run.
