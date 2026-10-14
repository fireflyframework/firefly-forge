---
description: Inspect durable tool intents, bounded outcomes, frozen budgets and explicit reconciliation for agent search and approved external MCP calls.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Review mediated tool execution

Native knowledge lookups, agent internet searches and approved external MCP calls pass through a run-scoped broker. Before sending a request, forge records its intent and reserves a call from the run's frozen budget in PostgreSQL. The same transaction appends the intent to the run's hash-chain journal.

An invocation retains its correlation ID, execution stage, repository and project association, active role grants, actual source commit and tree, provider revision, hostname restrictions and policy. A successful result includes its `invocation_id`. Citations and provider usage receipts remain part of the recorded result.

## Read the activity

Use `forge tools invocations RUN_ID` or `GET /api/v1/runs/{id}/tool-invocations` to inspect an authorized run. The existing journal also contains the broker's events:

| Event | Meaning |
| --- | --- |
| `tool_intent` | The request was admitted and its budget reservation committed before dispatch. |
| `tool_receipt` | A bounded result or known provider rejection was retained. |
| `tool_unknown` | The call was interrupted, timed out or returned an outcome that could not be admitted. |
| `tool_reconciled` | An operator explicitly abandoned an unresolved invocation without replay. |

Invocation states are `pending`, `succeeded`, `rejected`, `unknown` or `abandoned`. A process crash can leave a committed `pending` intent without a receipt. That intent remains unresolved; its absence of a receipt never means that the provider received no request.

The database prevents changes to admitted requests, changes to completed outcomes, and deletion of invocation records. The journal's existing immutable hash chain retains every admitted intent and reconciliation.

## Set limits for new runs

A factory administrator can read `forge tools policy` and replace the policy using a JSON file:

```json
{
  "calls_limit": 30,
  "per_tool_limit": 10,
  "timeout_seconds": 30,
  "max_result_bytes": 256000
}
```

```bash
forge tools configure-policy --file tool-policy.json
```

These are the defaults. The total can be set from 1 to 100 calls, each tool from 1 to 30 calls, the invocation timeout from 1 to 60 seconds, and admitted result size from 1,024 to 750,000 bytes. The search provider transport keeps its own stricter request and response limits.

The policy freezes when a run is queued. Updating the factory policy changes new runs and leaves queued revisions intact. Pre-broker runs without a saved policy use the fixed version-one defaults. Campaign admission also checks the policy revision so its component runs cannot silently capture different limits.

Successful calls, provider rejections and unresolved attempts consume budget. A repeated correlation ID with the same source, arguments and configuration returns its stored result or disposition without dispatching again. Reusing the ID for different input is rejected.

This budget limits admitted call attempts. It does not set a provider's spending cap or guarantee a price. Standalone administrator search queries and connection tests use their existing routes separately from the run broker and can consume normal provider quota.

## Reconcile an uncertain outcome

forge permits one mediated invocation at a time in a run. An unresolved intent blocks further broker calls and blocks implementation delivery, even if the coding worker reports completion. Other runs retain their own budgets and journal records.

If a provider response is lost, inspect the run's activity and the provider account's usage. To record a decision to abandon that invocation without retrying it:

```bash
forge tools abandon RUN_ID INVOCATION_ID --request-id RECONCILIATION_ID
```

The reconciliation ID must contain 32 lowercase hexadecimal characters and identify this decision. Repeating that same decision is idempotent. Using it for another invocation is rejected. The API route is `POST /api/v1/runs/{id}/tool-invocations/{correlation}/reconcile` with `{"request_id": "RECONCILIATION_ID"}`.

A known quota, authentication or rate-limit rejection pauses the execution stage with its typed classification. A lost or unadmitted response moves the run to `needs_reconciliation`. Native FireflyAgent execution disables its internal whole-run rate-limit retry so the durable supervisor controls continuation; the coding model cannot turn a failed tool call into an automatic replay.

An invocation that may still be running cannot be abandoned until its bounded lease expires. Abandonment does not claim successful execution, restore consumed budget or send another provider request. A later request needs a new correlation ID. Inspect the run's overall recovery state before [resuming implementation](recovery.md); the implementation intent has its own recovery contract.

## API, Python and MCP access

Factory policy routes require a factory administrator. Run activity follows the existing project read permission. Reconciliation follows the run's execution permission and publication guards. The authenticated decision actor is recorded with the reconciliation event.

The Python SDK provides `tool_policy()`, `configure_tool_policy()`, `tool_invocations()` and `abandon_tool_invocation()`. forge's MCP server provides `get_tool_policy`, `configure_tool_policy`, `get_run_tool_invocations` and `abandon_tool_invocation` through those same API routes.

The broker currently mediates native knowledge lookup, agent internet search and approved external MCP calls. Workspace operations, commands and browser checks retain their existing execution guards and evidence mechanisms; this does not move every native or official CLI tool into the broker. External MCP calls require the [approved registry and project grants](external-tools.md). See [agent tools](agent-tools.md), [search providers](search-providers.md) and [capability limits](capabilities.md).
