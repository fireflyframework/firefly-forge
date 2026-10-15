---
description: Coordinate product components through a dependency graph, immutable run admissions and evidence-based delivery gates.
---

# Deliver a product across components

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

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

## Read a report in Studio

In **Product projects**, select a product and choose **Load campaigns**. Select an existing campaign, then choose **View report**. Both actions read retained records; they do not launch, resume or retry work. The server checks current project and repository read permissions.

The report separates the server's evidence state from recorded workflow status. Its compact summary counts components; select one component to inspect its tasks, executable check receipts, recorded source and checkpoint references, prior attempts and journal references. Lists show at most 20 items per page. An unavailable snapshot has unknown counts, rather than reporting zero components or verified completion.

The digest is displayed as a server reference; Studio does not independently verify it. SCM freshness remains **not observed**, and milestones and waves are **not configured**. Report reads are explicit and do not poll. An HTTP error or an unmatched response does not establish a report state; a changed session or denied reread clears private report evidence.

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


## Read a campaign evidence report

Use `GET /api/v1/projects/{project_id}/campaigns/{campaign_id}/report` to read the campaign's current component attempts and their saved evidence. The same read is available through `ForgeClient.project_campaign_report(project_id, campaign_id)`, `forge campaign report PROJECT_ID CAMPAIGN_ID`, and the read-only MCP tool `get_project_campaign_report`. Current read access to the project and every referenced repository is required, including repositories retained by earlier attempts.

The report counts current attempts by state. `verified` requires the existing campaign delivery gate: passing executable checks, matching source and checkpoint receipts, and any requested review or publication. `in_progress` describes a recorded nonterminal run; it does not establish that a worker is currently alive. `blocked` covers stopped runs and unmet dependency gates. `unknown` preserves reconciliation obligations and missing verification evidence. Tasks with matching immutable completion receipts are labeled `receipted`; this alone does not establish component verification. Each completion must match its current task attempt and a preceding exact intent. A started unresolved task holds component verification even when the run status says `succeeded`. An unstarted optional remote task does not imply an unknown effect. Earlier attempts remain references and do not increase current totals.

Each component includes its run and admission digests, plan digest and journal head. Task, branch, source-tree and checkpoint references identify the exact journal sequence and digest. Checks include their command digest, argument count, actual exit code and receipt reference. Command arguments and output are omitted. Follow those references through the existing run plan and journal endpoints. Campaign event count and head refer to the existing campaign journal.

A report reads one PostgreSQL repeatable-read, read-only snapshot. Its `digest` is SHA-256 of the entire returned JSON object except `digest`, with recursively sorted keys, compact separators and unescaped Unicode. Version, defaults and limits are included. Identical retained evidence produces the same report; a later read can produce a different digest. Reports are returned to the caller and are not stored as a new report history. Credential admission runs after the snapshot closes, with repository permissions checked before and after it.

The snapshot allows 200 child attempts, 1,024 campaign events, 128 tasks per current run, and 2,048 journal entries and run events per current run. A shared 16 MiB JSON evidence budget is checked in PostgreSQL before rows are loaded. The transaction has a ten-second deadline. Exceeding a limit or detecting inconsistent evidence returns `unknown` with a reason, no component totals and no partial verified result.

The report follows the campaign's frozen component graph. The current project model has no milestone or wave configuration, so both are reported as `not_configured`. It never contacts Git or a source-control provider; `scm_freshness` remains `not_observed`. Saved branches and commits do not prove current remote synchronization, a merged pull request, deployment or whole-product integration acceptance. Milestone definitions, wave grouping, reports across campaigns and a Studio report view remain separate work.
