---
description: Choose build, maintenance or test work and follow the retained execution plan, verification results and delivery checkpoint.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Follow build, maintenance and test workflows

The execution model has four real roles. The orchestrator controls the queue and workspace. One selected coding worker implements and repairs source. The verification runner executes commands. The publisher creates an external review request only when explicitly requested. Verification and publication are deterministic services, not additional model agents.

`GET /api/v1/workflow` returns the actual roles, stages and tools for Studio and automation.

## Build a product

Choose `operation: "build"`, the default for existing requests. Supply a product brief, repository, stack and worker. The factory freezes connection and policy settings, clones the default branch, and creates an isolated run branch. The selected worker receives the framework profile and exact selected policy instructions.

The factory requires real source changes and executable verification. Empty `commands` use a detected `make check`, package script or supported project test command. If no check exists, the run fails. A failed check returns its retained output to the same worker for bounded repairs.

## Maintain existing software

Choose `operation: "maintain"` and describe the specific bug, dependency update or behavior change. Maintenance uses the same isolated workspace and verification pipeline as a build. Its worker instructions require preserving public behavior, inspecting the existing implementation and adding focused regression tests.

A useful maintenance brief states the triggering input, current failure, expected result and compatibility constraints. Select the project's policy defaults and supply its real checks when automatic discovery is insufficient.

## Test existing software

Choose `operation: "test"`. This workflow clones existing source and runs supplied or detected checks directly. It never invokes a coding provider and never repairs a failing check automatically. A passing run may contain no source changes.

Test operations retain the exact immutable product revision as historical context without validating, approving, or materializing its saved design. An outdated design review therefore does not prevent checks of existing source; a passing test does not approve that review or authorize publication. Build and maintenance still require any saved design review to match the product definition and current brand.

```json
{
  "repository_id": "your-repository-id",
  "brief": "Verify the current stock reservation behavior.",
  "operation": "test",
  "backend": "pyfly",
  "commands": [{"argv": ["make", "check"], "timeout": 600}]
}
```

Selected quality-gate policies still execute. Recommendations are retained in the policy snapshot, but no worker receives them in a test-only workflow. Checks that modify source fail the source-integrity comparison. Test runs retain command evidence and a verified tree, with no changed-source archive or implementation branch to publish.

## Review each stage

| Stage | What happens | Evidence |
| --- | --- | --- |
| `queued` | Input, provider settings and policy selection are frozen | Run request and snapshot IDs |
| `preparing` | The repository is cloned into an isolated workspace | Starting Git revision and run branch |
| `developing` | The selected worker implements or maintains source | Provider events and source changes |
| `validating` | Commands run independently against the source | Exit status, output and attempt number |
| `repairing` | Failed checks return to the same worker | Earlier failures remain retained |
| `succeeded` | Final checks passed against the recorded tree | `final_verification`, `source_tree`, diff and available export |
| `publication` | Explicit publication pushes and creates a review request | Remote request URL or publication error |

Failures, cancellations and interruptions remain distinct outcomes. Passing commands establish their tested behavior; inspect the diff and test coverage before accepting a product. Browser build results require separate interaction and visual review. [Operations](operations.md) covers cancellation and recovery; [deployment](deployment.md) covers configured deployment targets.
