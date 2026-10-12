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

## Understand automatic limits

Eligible rate-limit and transient pauses use their recorded delay and a budget of at most three automatic retries. Other pauses require an operator decision. Local concurrency is bounded globally and per project; work is selected across projects rather than filling both worker slots with one backlog.

The journal's hash chain detects a changed record sequence or payload. It is an integrity check within the operator's PostgreSQL ledger, not an external signature or protection against a database administrator replacing the entire history.
