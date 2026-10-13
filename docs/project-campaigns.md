---
description: Coordinate product components through a dependency graph, immutable run admissions and evidence-based delivery gates.
---

# Deliver a product across components

A delivery campaign coordinates the configured components of one product project. It admits every child run together, then releases components as their dependencies finish. Different repositories can run in parallel; the factory keeps one active run per repository.

Before launch, configure each component's source root, stack, dependency list and executable verification commands. Choose the coding provider and, when needed, a separate review provider. A campaign does not infer a customer's requirements or replace a reviewed product definition.

## Launch and inspect

Send an authenticated `POST /api/v1/projects/{project_id}/campaigns`:

```json
{
  "request_id": "65d93d60cb5342c6b6e6bdb0ba9e24ed",
  "brief": "Implement the approved inventory reservation behavior",
  "agent": "codex",
  "operation": "maintain",
  "assurance": {"review_agent": "claude"},
  "dependency_gate": "verified",
  "publish": false
}
```

Generate a fresh UUID without hyphens for `request_id`. Repeating the same request returns its recorded campaign without admitting duplicate children. Reusing that identifier with different content is rejected. Optional `component_ids` selects components and includes their upstream dependencies.

Use `GET /api/v1/projects/{project_id}/campaigns` to list campaigns and `GET /api/v1/projects/{project_id}/campaigns/{campaign_id}` for current child references, attempts, statuses, blocked reasons and the verified campaign journal. Child `run_id` values link to the normal run, plan, verification and artifact endpoints.

All admissions commit in one PostgreSQL transaction. The factory freezes the complete project revision, component paths and checks, canonical product definition, brand and reviewed design, policy and role selections, coding/reviewer configuration revisions, runner configuration and knowledge/search revisions. Preflight resolves actual source commits and probes runner capabilities before taking admission locks. A settings change during this process refuses admission instead of mixing revisions.

Source-control application identity is retained separately. Repository grants are checked afresh for each action, expiring credentials refresh, and revoked access is honored. A campaign does not freeze source-control permissions.

## Understand the dependency gate

The default `verified` gate requires an upstream run to succeed with nonempty passing executable checks, matching source/checkpoint receipts, and an approved matching independent-review receipt when review was requested. A successful status alone is insufficient.

The optional `published` gate additionally requires an actual branch/pull-request publication receipt. If launch explicitly sets `publish: true`, that requested publication is also part of child completion. Test-only campaigns cannot request publication.

Downstream context records upstream run IDs, actual source commits/trees, branches, artifacts and publication receipts. It retains its own frozen source base. Those references **do not merge upstream code** into the downstream repository or prove an assembled product passes integration tests. Publication does not imply a pull request was merged or an application deployed. Configure and execute whole-product integration verification separately.

## Stop and recover explicitly

Every action requires a fresh `request_id` in its request body.

| Action | Endpoint suffix | Result |
| --- | --- | --- |
| Cancel | `/{campaign_id}/cancel` | Stops release and requests cancellation of current child runs. Successful children retain their receipts. |
| Resume | `/{campaign_id}/resume` | Resumes eligible paused/interrupted children in their existing workspaces and keeps their identities and frozen configuration. |
| Retry | `/{campaign_id}/retry` | Admits one fresh attempt for explicitly selected failed, cancelled or interrupted components, preserving the original graph, source base and admission snapshots. |

Retry also requires `component_ids`, for example:

```json
{"request_id":"a5717fe447cc42379284a0e3e4386272","component_ids":["inventory-api"]}
```

Each component allows three explicit retries. Repeating the same action request does not create another attempt. An active downstream component must stop before its upstream component can be retried. Resume does not answer a pending worker question or mark a failed component successful; answer the question or choose an explicit retry.

An unfinished effect intent, uncertain publication or interrupted cancellation holds new releases. Inspect the child plan and reconcile its recorded effect using the existing run reconciliation controls. The coordinator never automatically repeats an unknown external effect. If a cancellation request stopped before its receipt, stop its recorded children and reconcile their journals first; explicit campaign resume then records the observed stopped outcome. Cancelled children still require an explicit retry to start again.

Campaign events and child admissions are immutable PostgreSQL history. The coordinator verifies its hash-linked journal before releasing work. Loss of the factory execution lease stops coordination; restart against the same owned data directory and schema to retain the campaign and its current child references.
