<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Bound repairs and review source before delivery

firefly forge verifies product source before retaining a checkpoint. Every run also has a fixed repair budget and a no-progress guard. You can request an additional review from a coding provider in a fresh source-copy context. That review is optional; a run without it records `not_requested`.

## Keep repairs finite

`FORGE_REPAIR_ATTEMPTS` sets the number of repairs after the first implementation round. The server freezes its value when queuing the run, within a range of zero to three. The default is two repairs. Changing the environment, resuming a pause or handing work to another provider does not increase that run's budget.

A completed coding response consumes one implementation round. A known quota or authentication pause keeps its unfinished round available. Verification still has to pass within the remaining budget. The factory stops earlier when two consecutive completed rounds produce the same source tree and the same failing command/exit-code signature. Changing a timestamp in a failure message does not count as progress.

The run retains `repair_budget`, `implementation_rounds` and `repair_history`; the plan retains the frozen budget. Failed checks authorize a repair intent before another coding call. Once the budget or progress guard stops a run, inspect its evidence before creating a fresh run with a better brief or different checks. A fresh retry has its own history and budget.

## Request an independent review

Select a reviewer when creating the run:

```json
{
  "repository_id": "YOUR_PROJECT_ID",
  "brief": "Prevent concurrent reservations from overselling stock",
  "operation": "maintain",
  "backend": "pyfly",
  "frontend": "react",
  "commands": [{"argv": ["make", "check"], "timeout": 600}],
  "assurance": {"review_agent": "codex"}
}
```

Supported reviewer IDs are `codex`, `claude`, `bridge` and `agentic`. Configure and test the selected provider first. Its configuration revision is frozen separately from the implementing worker's revision. You may choose the same provider: independence here means a fresh execution context and a separate source copy, not a promise that two different models or organizations evaluated the product.

The review runs after the final product checks pass and before checkpoint delivery. Test-only runs can also request it; the additional review is an explicit model call even though test mode does not invoke a coding worker.

## Read the verdict and evidence

The reviewer receives the verified Git tree under `source/`, the brief, the frozen customer definition and passing command identities. It must write only `review.json` at the review workspace root. The review workspace is private factory state outside the original product clone; the report is excluded from product commits and source archives.

Reviewed journey designs are written into bounded `docs/design/` reference files before the first build or maintenance worker call. The plan records their content digest and paths; the prompt points to them instead of embedding all SVG bytes. Resume preserves those files and does not silently replace the original reviewed bundle. Test-only runs preserve existing source.

The strict report contains a verdict, summary and at most 30 findings:

```json
{
  "verdict": "changes_required",
  "summary": "The reservation update needs a concurrency guard",
  "findings": [{
    "severity": "high",
    "path": "inventory/reservations.py",
    "line": 24,
    "summary": "Stock can be oversold by concurrent requests",
    "evidence": "remaining = item.stock - quantity"
  }]
}
```

Severity is `low`, `medium`, `high` or `critical`. A changes-required verdict needs at least one finding. An approved report cannot retain high or critical findings. Referenced paths must be relative source paths, quoted evidence must occur in the referenced file, and line numbers must exist. These checks establish that a finding refers to actual source; they do not prove the reviewer's interpretation is correct.

The factory rejects malformed or oversized reports, symlinks, hardlinks, fabricated source references, credential content and changes to either source copy. It also detects changes to the review Git baseline. A `changes_required` verdict blocks delivery; it does not start another automatic repair loop. The run's `review` field retains the validated result and the exact source tree it examined.

## Recover without repeating an unknown review

The durable plan adds a remote `review` task only when one was requested. Its intent precedes the provider call; a validated report produces a receipt. A completed receipt can be reused after an interrupted checkpoint only for the same source tree.

A known rate-limit, quota or authentication failure pauses the review. Resolve the provider issue before resuming. The frozen reviewer configuration remains unchanged; changing saved connection settings does not silently replace it. A new run is needed when a different reviewer configuration is required.

A lost response leaves an unknown outcome and blocks automatic replay. Inspect the plan, journal and retained review workspace. Generic local reconciliation cannot retry that remote task. Do not treat a missing receipt as approval or repeat the call blindly.

## Know the boundary

This is an additional model review with deterministic output and source-integrity checks. It is not a formal proof, a penetration test or isolation from a malicious process running as the same host account. Source text and customer content remain untrusted; the review prompt explicitly prevents them from overriding the review contract. Use dedicated development hosts and the [security guidance](security.md) for stronger operating boundaries.

Before checkpointing, publication or deployment, the factory checks the current source against its verified tree and, when selected, its approved review tree. Downloadable source changes come from that immutable Git tree. A later source mutation requires new verification and review rather than inheriting an earlier approval.

See [recovery](recovery.md) for pauses and handoffs, [provider setup](agents.md) for access, and [deployment](deployment.md) for explicit external effects.
