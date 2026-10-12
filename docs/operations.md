<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Operate a local factory

Run Forge under a dedicated development identity, with a private data directory
and access only to the repositories and provider accounts it needs. Treat the
API token as trusted operator access: runs execute commands on the server.

## Inspect a run

Use **Factory** for real run history and **Run details** for progress, diff,
verification and artifacts. The API exposes the same evidence through
`GET /runs/{id}`. Keep failed and corrected attempts separate.

If generation fails, inspect the provider error and login/model configuration.
Verification failures first enter an automatic repair loop. Forge returns all
failed commands, exit statuses and retained output to the selected coding
provider, which repairs the existing implementation and reruns verification.

`FORGE_REPAIR_ATTEMPTS` defaults to `2` and is bounded to `0` through `3`. These
are additional repair attempts after the initial implementation: `0` disables
automatic repair, while the default permits up to three implementation attempts
in one run. Every command result retains its attempt number. `verification`
contains the history; `final_verification` identifies the final passing checks.
Earlier failures remain evidence even when a later attempt succeeds.

If the repair budget is exhausted, the run fails. Inspect the command output,
correct the brief or environment and use **Retry run** to request a separate run.

## Cancel and recover

**Cancel run** requests cancellation of an active run. Forge terminates the
provider process group and records cancellation. Cancellation cannot undo an
external action already performed by generated code or an agent.

Shutdown stops active work. On restart, the engine reconciles durable run state.
Interrupted work must not be reported as successfully verified. Review the
recorded outcome before retrying.

## Preserve evidence

The data directory contains persisted state and run workspaces. Stop Forge
before copying that directory for a simple consistent local backup. Keep tokens,
provider credentials and sensitive repository content out of public artifacts.

Only one active Forge service may own a data directory. The engine acquires an
exclusive file lock before recovering or dispatching work. A second service
using that directory fails startup; give separate services separate data
directories rather than bypassing ownership. `FORGE_CONCURRENCY` controls the
number of active runs within the owning service.

Forge records the verified source-tree snapshot after commands pass. Checks
that modify the source invalidate that attempt. Before publication, Forge
compares the current source tree with the recorded snapshot and rejects a
changed workspace. Create a fresh run to verify later edits; do not treat an old
passing result as evidence for a changed tree.

There is no automated retention policy in this initial version. Operators own
workspace cleanup after reviewing which evidence and source changes must remain.
Do not remove active workspaces or reset unrelated repositories to clear a run.

## Remote operation

The local default binds to `127.0.0.1`. For remote access, use an HTTPS reverse
proxy, restrict network exposure and manage the operator token securely. This
version does not provide tenant isolation, per-user authorization or a hosted
production support commitment. See [security](security.md).
