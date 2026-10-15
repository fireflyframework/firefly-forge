---
description: Configure bounded host or Docker execution, command policies, source transfer and honest runner capability checks.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Configure isolated execution

Choose a runner before queueing a run. firefly forge freezes its configuration, resource limits and command budget with the run. Repository defaults apply to new runs; an explicit run configuration takes precedence. Changing defaults or handing work to another coding provider preserves the existing run's execution policy.

## Choose the execution boundary

| Mode | Behavior |
| --- | --- |
| `host` | Commands run as the development service account with a filtered environment, timeout and output limit. The operating system account can access other host files and networks. |
| `docker` | Commands run in a digest-pinned image with the component workspace mounted, a read-only root filesystem, dropped capabilities and resource limits. An unavailable image or context fails execution. |
| `pool` | Administrator opt-in command execution through an authenticated local controller, with frozen profiles, durable admission quotas and archive transport. Requires a separately configured Factory worker. |

The runner applies to independent verification and the native Firefly Agentic `execute_command` tool. Native Codex and Claude Code processes use their official CLI policies. Their own file, command and network tools execute outside this Docker runner. Native Python tool grants cannot enforce restrictions inside those CLIs.

Cloud provisioning and deployment commands use the separately configured [deployment target and credentials](deployment.md). Their credential workflows are outside this code runner.

## Recheck authority after waiting for a runner

After acquiring its run lock and execution capacity, the runner rechecks coordinator ownership, cancellation and the recorded actor's current execution authority. It checks again before a new create intent and before host execution or Docker preparation and start. Revocation or cancellation observed at these checks prevents dispatch. These are point-in-time checks: they do not lock permissions through process launch or undo effects already performed. Legacy and internal executions without a recorded actor retain their existing admission contract. Returning a retained result also requires admission; the original result and journal remain intact when access is refused.

Cleanup can still remove the invocation's owned containers and volumes after revocation or cancellation, with the existing ownership-label and coordinator checks. Removing execution permission must not prevent those resources from draining. Effects a command already performed remain part of its retained history and recovery process.

## Keep Python verification tied to current source

Factory commands receive a fresh execution-owned Python cache namespace and `PYTHONDONTWRITEBYTECODE=1`. The namespace replaces inherited or caller-supplied `PYTHONPYCACHEPREFIX` values. This prevents normal Python imports from accepting an earlier timestamp-and-size cache after a same-size source edit. Disabling bytecode writes alone would still allow an existing stale cache to be read.

Host execution uses a private temporary directory and removes only that owned directory after the process has exited or been terminated. Docker and pool commands use their existing fresh container `/tmp` filesystem. Project caches are not cleared; command arguments, runner resource limits and journal recovery rules stay unchanged.

These settings govern Python interpreters that honor their environment. `-E` or `-I` ignores them; an explicit interpreter cache-prefix option or a custom loader can select other behavior. Source-less `.pyc` modules remain explicit project artifacts, and this policy does not establish their source equivalence. Keep source available and avoid those overrides when a verification check must evaluate current Python source. Native coding CLIs retain their separate execution boundary.

## Prepare an image and context

Use a dedicated Docker context and a non-root development service account. Prepare an image containing the project's actual Python/PHP/Node runtimes, package tools and dependencies. Copy its immutable `repository@sha256:...` reference from the image inventory. Runtime commands use `--pull=never`; image preparation belongs to the operator.

With network policy `none`, dependency installation and external databases are unavailable. Prepare dependencies in the image or choose the explicit `egress` policy. `egress` uses Docker's bridge network and permits outbound access; it does not implement a hostname allowlist. Browser checks always use network `none`.

Post repository defaults to `PUT /api/v1/repositories/{id}/runner`:

```json
{
  "sandbox": {
    "mode": "docker",
    "image": "ghcr.io/your-team/project-runner@sha256:REPLACE_WITH_64_HEX_DIGEST",
    "context": "your-isolated-context",
    "cpus": 1,
    "memory_mb": 1024,
    "pids_limit": 128,
    "timeout": 300,
    "max_output_bytes": 1000000,
    "network": "none",
    "command_limit": 50
  }
}
```

Replace the image reference before submitting this example. `GET /api/v1/repositories/{id}/runner` returns the saved default. `GET /api/v1/repositories/{id}/runner/readiness` reports the selected context, cached image evidence and native tool availability. `POST /api/v1/runners/probe` accepts a sandbox configuration for a read-only image check. Cached image presence does not prove that a project's checks or browser startup command work.

Each container receives only the component source mount. Verification additionally sees the repository's `.git` directory through a nested read-only mount. Implementation commands cannot read that metadata through the source mount. The container receives no host home directory, login credentials or Docker socket. Its writable temporary directory and private shared memory are bounded. Docker controls include CPU, memory, process count, no-new-privileges and bounded daemon logs. See the official [Docker execution options](https://docs.docker.com/reference/cli/docker/container/run/).

### Transfer workspaces to a remote or isolated daemon

`workspace_transport` defaults to `bind`, which requires the selected daemon to read the exact host workspace path. For a remote daemon, or a virtual machine without host filesystem sharing, select `archive`. The executor copies source into private anonymous Docker volumes and collects the completed result through the Docker API. The daemon never needs a host workspace mount. The selected context is operator-owned; its authentication belongs to the service account, outside the command container.

Archive mode also requires the administrator to set `FORGE_SANDBOX_TRANSFER_IMAGE` to a cached, trusted `repository@sha256:...` image containing `/app/.venv/bin/python`. The project image does not perform privileged initialization. A separate helper uses the administrator image, an absolute Python entrypoint with `-I -S`, and a fixed ownership initialization script for the two archive volumes. This helper has a read-only root, network `none`, no host mounts or credentials, and only the `CHOWN` capability. Repository code runs later in the non-root command container with all capabilities dropped. Missing transfer images fail without a pull or host fallback. The execution journal records the selected transfer image and helper cleanup.

Set `workspace_max_bytes` to bound transferred file contents (64 MiB by default, 1–256 MiB permitted). Archive overhead has an additional 1 MiB ceiling. Only regular files and directories transfer: symbolic links, hardlinks, special files, absolute paths, parent traversal, duplicate paths and oversized results fail. `.env` files and host credential directories stay local; configured credentials in transferable source cause rejection. Verification receives Git metadata through its separate read-only volume, and result collection never imports `.git` changes.

Source reads and result extraction use directory-relative descriptors with no-follow file opens. Collection prepares the complete replacement tree privately, preserves local Git and credential metadata, and publishes it with an atomic directory exchange. It verifies both exchanged directory identities and the retained original tree, then rolls back if concurrent source or metadata changes are detected. Preparation failures leave the original workspace untouched; no original files are deleted to make room for an incomplete copy. Successful publication retains the original tree at `workspace_backup_path` with its source hash. If the filesystem also rejects rollback, the exchanged backup remains available and publication stays uncertain until reconciled. Include these retained workspace trees in operator-managed backup and retention procedures.

The host workspace filesystem must support Linux `renameat2(RENAME_EXCHANGE)` or macOS `renameatx_np(RENAME_SWAP)`. Unsupported filesystems fail closed. Completed source collection is idempotent during receipt recovery. A returned archive is retained privately before publication; its hash and actual command outcome are recorded in the execution receipt. Failed imports record `workspace_import=requires_reconciliation`; retrying that invocation does not report success or run the command again. Inspect its retained archive and source before explicit stage recovery. If the archive cannot be retained locally, the exited owned containers remain available for reconciliation.

Command timeout, rejected archives with retained evidence, and failed initialization clean up the owned command container, helper and anonymous volumes under their exact durable labels. Include private `.forge-sandbox-results` archives with the execution journal in operational backups; they can contain source and Git metadata.

## Administrator opt-in local command pools

Pool selection uses the explicit `forge-factory-pool-v1` policy and `forge-project-bundle-v1` transport. Earlier `pool_binding` fields remain disabled and never activate a pool or change a frozen run. The trusted Factory worker retains user, project, repository and stage authority; project containers receive neither its PostgreSQL credentials nor its controller token, TLS material, provider credentials, other workspaces or Docker socket. The controller alone owns the administrator Docker context.

Start the controller with an explicit private configuration through `python -m firefly_forge.runner_controller_runtime --configuration /absolute/private/controller.yaml`. Its runner schema must differ from the Factory schema in the same PostgreSQL database. Mutual TLS, a private controller token, cached approved image digests, the observed Docker engine identity and an administrator-controlled absolute `git_binary` are required for project bundles. No image pull or host fallback occurs. Factory enables this adapter only when `FORGE_RUNNER_WORKER_CONFIGURATION` points to a private owner-controlled YAML or TOML file with the `forge.runner.worker` section, an active `transport` and an absolute `git_binary`. Transport binds the HTTPS endpoint, certificate, private key, CA, token, profile ID and immutable profile revision. The repository policy must match that approved private profile. Keep these files private and outside project source.

Factory transfers sanitized component source and a synthesized parentless Git snapshot of those exact files. This snapshot supports local source diffs; it does not expose or reproduce the original repository's history, remotes, configuration or hooks. The controller independently reconstructs the minimum Git snapshot before admission and before creating project resources. Git metadata stays read-only and is never imported into the Factory repository. Transferring original repository objects would grant repository-read authority; component selection alone would not make those objects confidential.

Project commands use network `none`, a read-only source volume, bounded writable workspace tmpfs, a non-root user and an independent PID1 deadline. Workspace tmpfs consumes the container memory budget. Export has a separate bounded grace period; a lost export leaves an unknown outcome. One profile identity owns each physical engine, with capacity and project quotas shared across its immutable revisions. Quarantined resources continue reserving capacity until exact backend observations establish drain.

The pool supervisor reaps adopted child processes while the command runs. Before publishing its authenticated completion status, it terminates and reaps remaining descendants, including children that started a separate session. The original supervisor deadline remains active during this drain; an incomplete drain cannot produce a completion receipt. This controls processes inside the local command container. External services and effects already performed by the command remain outside this lifecycle boundary.

Factory records a local preparation receipt and durable start intent before its single submission. A lost response leaves that start uncertain; repeat calls never resubmit. Returned archives and output are retained before atomic publication. Concurrent host edits reject publication while preserving the returned artifact and host source. Restart recovery observes or cancels the original controller identity, records interruption and drains observed resources without automatically importing source or replaying commands.

### Continue one queued command after a controller restart

A currently authorized operator may request continuation of one original pool command while its Factory worker is still waiting. The Factory run and stage must remain active under the original database owner. This does not resume an interrupted run or submit a replacement command.

Send one authenticated `POST /api/v1/runs/{run_id}/runner-continuations` with a retained request identity and the original `execution_id` from that run's sandbox intent in its journal:

```json
{
  "request_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "execution_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

Choose a new random UUID hex value for the operator request and save it before sending. The execution identifier must be the existing command's identifier. API replicas only record the request. The active Factory leader verifies the original actor, requesting operator, project, repository, stage attempt, source, profile, owner and WAL before sending the exact scoped request to the trusted controller. The controller checks the native Factory admission and current permissions again before effects. It can claim only that command, before its admission expires and with no external create/start intent. An expired, never-started lease may be claimed with a new controller epoch. Normal resource and project limits still apply. A capacity refusal does not schedule a later wake or select another queued command.

The Python client, CLI and factory MCP expose the same request and exact read. The CLI requires both identifiers; none of these adapters generates a replacement identity or retries a failed request. Dedicated Studio controls remain a separate follow-up.

```bash
forge request-runner-continuation RUN_ID --request-id SAVED_REQUEST_ID --execution-id ORIGINAL_EXECUTION_ID
forge runner-continuation RUN_ID SAVED_REQUEST_ID
```

In Python, call `client.request_runner_continuation(run_id, {"request_id": saved_request_id, "execution_id": original_execution_id})` once, then use `client.runner_continuation(run_id, saved_request_id)` to inspect that operation. External agents use `request_runner_continuation(identifier, request)` and the read-only `get_runner_continuation(identifier, request_id)` MCP tools. Retain the request identity in the agent's task record before calling the write tool. If delivery is uncertain, use the read tool rather than allocating another request.

Read `GET /api/v1/runs/{run_id}/runner-continuations/{request_id}` for the retained operation. Neither that read, repeating the same Factory request, nor controller startup dispatches work. The original command's result must still pass its existing credential, authority and workspace-publication checks. An operation marked `completed` with `result.disposition=accepted` confirms continuation admission; it does not prove that the command succeeded or that source was published. Use the original command and run receipts for those outcomes.

There is one explicit continuation identity per command, with at most 50 such admissions per run and the existing bounded worker queue. Commands admitted before their original Factory fence was retained are ineligible. Cancellation, revoked access, a changed stage or source, an expired admission, a stale owner and any uncertain create/start effect are refused. If the continuation response is lost, or the replacement controller also stops, keep the exact operation unresolved and use the existing observation/cancellation/reconciliation path. This first continuation flow does not retry an unknown operation or authorize another controller restart. It adds no command budget, automatic queue recovery, Kubernetes execution or multi-host storage.

Retained artifact reads and command-output returns check the current credential registry, including the first result and later cached results. If a retained value is now recognized as a credential, the read is refused and command-output access requires reconciliation. The private artifact and original receipts remain intact; the command is not resubmitted. This checks registered credential values and their supported escaped forms, so operators must still review source and results for unregistered secrets.

Local qualification covers a trusted Factory host, mutually authenticated controller and isolated command containers. Browser pool profiles, Factory worker container placement, project database sidecars, Kubernetes and shared multi-host storage require separate qualification. Native Codex/Claude provider processes retain their own execution boundary.

## Read execution and recovery evidence

The native PostgreSQL ledger records creation, start and cleanup intents before each effect, then records actual receipts. Invocation IDs bind the run, stage, stage attempt and command ordinal. The frozen total command budget applies across repairs, resume and provider handoff.

At startup, the execution owner inspects containers with the exact persisted ownership labels. An exited container can supply its real exit code and bounded logs. A running or missing container with an uncertain command outcome requires reconciliation. Recovery cleans up owned containers and retains the decision in the journal. It does not start the uncertain command again.

A timeout or output overflow terminates the local process group and removes the owned Docker container. Host executions have no container to inspect after a service crash; an uncertain host command outcome requires operator review. Keep the [run journal and private configuration backups](recovery.md) together when restoring an installation.

The executor permits at most two active commands across runs and serializes commands within each run. The engine's project scheduling controls still apply above that limit.
