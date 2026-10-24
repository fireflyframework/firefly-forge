---
description: Test or maintain a saved checkpoint in a fresh run without changing the original repository, project, or run.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

# Start a fresh unit from a saved checkpoint

A fresh `test` or `maintain` run can use a saved checkpoint while the original implementation remains paused or failed. It uses the same repository, product project, and component. The repository’s default branch stays unchanged. Source selection requires `publish: false` in both modes.

Choose a checkpoint from `GET /api/v1/runs/{run_id}/git-deliveries`. Its remote status must be `draft` or `ready`, backed by a confirmed publication receipt. A draft is unverified work; choosing it for testing does not approve the original implementation or mark its pull request ready.

## Create a test run

Submit the normal creation envelope to `POST /api/v1/run-creation-requests/{new_request_id}`. Choose a fresh request ID and include the checkpoint’s original run ID, delivery request ID, checkpoint digest, and commit revision:

```json
{
  "version": 1,
  "input": {
    "repository_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "project_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "component_id": "api",
    "brief": "Check the retained checkpoint without changing its source",
    "operation": "test",
    "publish": false,
    "commands": [{"argv": ["python", "-m", "pytest"], "timeout": 600}],
    "source_checkpoint": {
      "version": 1,
      "run_id": "cccccccccccccccccccccccccccccccc",
      "request_id": "dddddddddddddddddddddddddddddddd",
      "expected_checkpoint_digest": "3333333333333333333333333333333333333333333333333333333333333333",
      "expected_revision": "1111111111111111111111111111111111111111"
    }
  }
}
```

The IDs and hashes above are examples. Use values from the actual checkpoint receipt. The creation request ID is independent of both the delivery request and the original run.

## Continue implementation in a new maintain unit

Use the same envelope with `operation: "maintain"`, `publish: false`, a fresh request ID, and the intended brief and checks. The ordinary worker clones the selected commit into a distinct run workspace, then uses that new run’s current provider, roles, configuration, and runner admission. It starts with its own implementation attempt, model session, tool history, and command accounting. It does not reset or extend the parent’s spent allowance.

This is a new work unit, not a resume or replay. The parent’s failure, pending question, answers, provider outcome, source grant, tool receipts, and command receipts remain unchanged. No previous model or source budget becomes authority for the child. Current project Git-delivery policy applies independently to ordinary maintain work; `publish: false` does not grant final publication, and this selection does not reuse the parent’s draft or publication authority.

The resolved snapshot records `operation: "maintain"` in its digest. The worker refuses a change from the admitted operation. For compatibility, the existing `test` mode remains the default in the resolved snapshot and is omitted from its serialized representation; existing test-source digests remain unchanged.

The SDK uses `ForgeClient.request_run_creation(new_request_id, envelope)`. The CLI accepts the same JSON document with `forge request-run-creation NEW_REQUEST_ID checkpoint-test.json`. Existing MCP clients use `request_run_creation`. Legacy `POST /runs` and `create_run` refuse checkpoint selection because they do not retain that independent admission identity.

forge checks the signed-in user’s current execution permissions, the original component membership, and the retained checkpoint journal. It observes the exact factory branch through the current source-control connection. Admission does not fetch repository objects or load the project’s files into an API response. The ordinary worker then clones the frozen commit and checks its tree against the checkpoint receipt before running verification commands under the selected runner policy.

The creation receipt returns a new run ID and a server-resolved `source_checkpoint` containing the commit, tree, branch, operation, and parent evidence hashes. It carries no previous model allowance, worker answer, execution grant, or automatic permission to publish. A failed check remains failed; `test` operations do not invoke an implementation provider, coding repairs, or Git checkpoint publication. An explicitly selected independent reviewer can invoke its configured review model and remains subject to the ordinary review admission.

Read `GET /api/v1/run-creation-requests/{new_request_id}` after a lost response. Reusing that exact request and input returns the same admission. Do not create a second request identity merely because the first response was lost.

## Follow the result

The original run keeps its status, question, journal, and delivery history. Each new run has its own commands, runner limits, actor, and verification receipts. Its API record exposes the frozen source reference; its creation receipt remains distinct from the original SCM observation.

For an issue already linked through an authenticated parent receipt, teammates with current access to the same project and component can inspect the child’s status. The reference labels a test or maintain child explicitly and retains its original creator as provenance. Only the parent’s exact issue provenance can be selected. This read-only relationship cannot answer a question, resume a run, request another checkpoint, or launch another run. Those actions require their own current authority.

## Current boundaries

This entry point requires a confirmed remote checkpoint whose factory branch still points to the selected revision. Local-only checkpoints, uncertain publications, changed remote heads, and unavailable source-control grants are refused. An older checkpoint is not silently replaced by the latest commit. Existing creation inputs without `source_checkpoint` keep their previous wire representation and request digest.

Verification commands follow the selected runner policy. Cloning uses the existing controller-side Git command timeout and output bounds; this feature does not add clone disk-growth supervision. Ref-only admission adds no whole-project payload cap and does not download blobs as a preflight. It does not introduce distributed cloning, partial-clone fallback, automatic dependency provisioning, or a new natural-language permission to create test runs.
