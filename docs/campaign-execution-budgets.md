---
title: Campaign execution windows
description: Separate project size from finite, auditable campaign dispatch reservations.
---

# Campaign execution windows

A project can describe more components than a campaign should start at once. Component selection has no fixed count ceiling. Execution remains controlled by a finite window configured for the project, current permissions, frozen child admissions, and the runner's own resource policies.

The initial policy grants 50 child dispatches and at most four unfinished children per campaign. These are configurable defaults, not maximum supported values. Positive integer values above them are accepted. A dispatch means one distinct child run released from `campaign_blocked` to `queued`; it is not a token, command, time, memory, or monetary allowance. Each child's existing verification, command, provider, and runner controls still apply.

In Studio, open **Product projects** and choose the project. The compact **Execution settings** summary shows the saved window, parallel runs, and dispatch status. Select **Configure** to edit, or **View settings** with read-only access. An authorized factory administrator can save against the displayed policy revision. Closing the editor keeps unsaved values without changing the saved summary; switching projects discards that local draft. The form accepts positive whole numbers without a fixed business maximum; selecting more components does not grant unlimited execution resources.

Select **Review campaign reservations** to inspect an existing campaign. Paused and interrupted children remain in the held-reservation count. **Review renewal** presents the additional dispatches, parallelism, and previous consumption before **Confirm additional dispatches** sends one request. Eligible waiting work may start after confirmation. A declined or unconfirmed save is reread without an automatic write retry; review the current state before making another decision. This screen does not resume cancelled work or reconcile unknown child effects.

Use `GET /api/v1/projects/{project_id}/campaign-execution-policy` to review the current policy. A current native factory administrator with project membership authority can save it with `PUT` using a fresh `request_id`, the reviewed `expected_previous_digest` (`null` before the first save), and `policy`:

```json
{
  "request_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "expected_previous_digest": null,
  "policy": {"enabled": true, "dispatch_window": 200, "max_parallel_runs": 8}
}
```

A configuration response acknowledges that exact request. Repeating an older request returns its historical policy and matching digests without reverting the current policy. Before a new save or budget renewal, read the policy again with `GET`: use that current `digest` for configuration CAS and `effective_digest` for renewal. Read authority alone never permits changing execution policy. Updating a project definition preserves its separately configured execution policy.

Within one coordinator instance, simultaneous launches with the same project, request ID, actor and frozen input wait their turn before snapshot preparation. The server rechecks current execution authority after waiting and before returning, including release authority when publication was requested. Other scopes can prepare concurrently. PostgreSQL request identity and admission locks still protect the durable write, and a real source or configuration change can refuse admission. A refused or lost response does not prove that no campaign committed; inspect the original request identity before deciding what to do next.

A new campaign freezes its initial policy receipt and records its dispatches in the immutable campaign journal. When its finite window is exhausted, remaining children stay blocked with an explicit budget reason. Their identities, snapshots, successful receipts, and previous consumption remain unchanged. Disabling the current project policy holds new dispatches. Lowering current parallelism also holds additional dispatches until capacity is available; it does not cancel existing work.

A parallel reservation is held while its child is unfinished, including while paused or interrupted. A resume uses that same reservation. A terminal child releases its parallel slot; an explicit retry creates a distinct child attempt and consumes another dispatch when released. New budgeted campaigns have no separate fixed three-retry ceiling. Existing campaigns without an execution-budget receipt retain their recorded legacy contract.

Review `GET /api/v1/projects/{project_id}/campaigns/{campaign_id}` before renewing. `execution_budget_state` reports all granted and consumed dispatches, while `journal.head` identifies the exact current evidence cut. Send one explicit `POST .../{campaign_id}/renew-budget`:

```json
{
  "request_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "expected_journal_head": "<reviewed 64-character journal SHA>",
  "expected_policy_digest": "<current effective_digest>",
  "additional_dispatches": 100,
  "max_parallel_runs": 8
}
```

The additional grant cannot exceed the current project's configured window or parallelism. The server rechecks current execution authority, release authority when publication was requested, the exact journal head, current policy, and child custody. It appends the grant and resulting dispatches atomically. Revocation or any failed check rolls back the grant and queue changes. An unknown child effect requires reconciliation; renewal neither retries it nor resets old counters. A cancelled campaign must be explicitly resumed first.

The Python SDK exposes `campaign_execution_policy`, `save_campaign_execution_policy`, and `renew_campaign_budget`. Each write sends one request. After a lost response, inspect the saved request and journal; the SDK does not automatically retry, renew, or resume work.

This slice does not prove complete large-project execution. Admission still freezes and inserts the selected children atomically, campaign listing and report windows remain separate limits, and runner project-slot quotas separately govern actual resource concurrency. No large product definition is added to prompts by this policy. Durable source inventory, provider/command budgets, and paged product design are separate work.
