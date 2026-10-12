<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Operate the factory

Use a dedicated development identity with access to the intended repositories, provider accounts and PostgreSQL schema. The operator API token permits code execution and connection management on that host.

## Inspect actual work

Open a run to inspect its plan, journal, source diff, checkpoint revision and verification. The floor displays real project work; use receipts and exit statuses to determine outcomes.

`verification` preserves every check attempt. `final_verification` identifies the checks supporting success, and `source_tree` identifies the checked Git tree. `checkpoint_revision` is an actual local commit reference. A worker's final summary alone does not establish that a product works.

## Build, maintain and test

Build and maintenance use the selected coding provider followed by independent verification. Maintenance tells the worker to preserve the existing product and add focused regression coverage. Test mode verifies existing source without invoking a model or coding repair.

Empty command lists use a detected repository gate. Selected policy quality gates add their real commands. If no executable verification is found, the run fails. There are at most 20 effective verification commands per run; each retains its own timeout and output.

Failed checks return to the worker for bounded repairs. `FORGE_REPAIR_ATTEMPTS` defaults to `2`, allowing an initial implementation and two repairs; its range is `0..3`. Earlier failures remain visible after a later passing attempt. [Workflows](workflows.md) explains mode selection.

## Handle provider limits

A rate limit pauses the current task with its retry time and retained workspace. The scheduler allows up to three automatic retries when the recorded failure permits them. Quota exhaustion and authentication failures require an operator action. Configure access or credits at the provider, then explicitly resume or hand off to another configured worker.

The factory records reported usage and provider errors. It does not infer an account's remaining prepaid balance from token usage. A CLI login, a model listing and a successful generation establish different facts.

## Cancel, resume or retry

Cancellation stops queued or active work and terminates worker process groups. It cannot undo a completed external effect. Shutdown preserves interrupted work; an unacknowledged journal intent requires inspection before continuation.

Resume continues the same authorized workspace and snapshots. Handoff creates a new provider configuration and plan revision while retaining the source, policies and evidence. Retry creates a fresh run and clone. Use [recovery](recovery.md) to choose the correct action.

## Back up the ledger and source together

Stop the service for a straightforward consistent backup. Back up the dedicated PostgreSQL database with its ordinary PostgreSQL tools, and preserve the private data directory containing configuration revisions, workspaces and artifacts. Protect both backups as sensitive development data.

Restoring only PostgreSQL can leave a run referring to missing source or credential revisions. Restoring only workspaces loses its durable task and verification history. Missing or changed retained workspaces require reconciliation; they must not be reported as accepted source.

The service enforces ownership before recovering or dispatching work. Give independent installations their own data directories and ledger schemas. Global concurrency defaults to two active runs; one run per project is active at a time. There is no automatic artifact-retention policy. Review evidence before removing a completed workspace.

## Publish and deploy deliberately

Publication uses the existing local checkpoint commit, pushes a branch and creates an external review request. It never merges automatically. A changed tree or an uncertain previous publication prevents a blind repeat.

Deployment requires an explicit target tied to the repository, configured commands and post-deployment verification. Rollback uses its retained command snapshot and verification. [Deployment](deployment.md) covers these operations and their remote-effect boundaries.

For remote use, serve one HTTPS origin, restrict host headers and manage operator access securely. The service does not provide per-user roles or tenant isolation.
