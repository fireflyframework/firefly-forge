<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Configure isolated execution

Choose a runner before queueing a run. firefly forge freezes its configuration, resource limits and command budget with the run. Repository defaults apply to new runs; an explicit run configuration takes precedence. Changing defaults or handing work to another coding provider preserves the existing run's execution policy.

## Choose the execution boundary

| Mode | Behavior |
| --- | --- |
| `host` | Commands run as the development service account with a filtered environment, timeout and output limit. The operating system account can access other host files and networks. |
| `docker` | Commands run in a digest-pinned image with the component workspace mounted, a read-only root filesystem, dropped capabilities and resource limits. An unavailable image or context fails execution. |

The runner applies to independent verification and the native Firefly Agentic `execute_command` tool. Native Codex and Claude Code processes use their official CLI policies. Their own file, command and network tools execute outside this Docker runner. Native Python tool grants cannot enforce restrictions inside those CLIs.

Cloud provisioning and deployment commands use the separately configured [deployment target and credentials](deployment.md). Their credential workflows are outside this code runner.

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

Source collection validates the complete returned archive and checks the original source digest before applying changes. A host edit made while the command runs requires reconciliation instead of being overwritten. Completed source collection is idempotent during receipt recovery. Command timeout, rejected archives and failed initialization clean up the owned command container, helper and anonymous volumes under their exact durable labels.

## Read execution and recovery evidence

The native PostgreSQL ledger records creation, start and cleanup intents before each effect, then records actual receipts. Invocation IDs bind the run, stage, stage attempt and command ordinal. The frozen total command budget applies across repairs, resume and provider handoff.

At startup, the execution owner inspects containers with the exact persisted ownership labels. An exited container can supply its real exit code and bounded logs. A running or missing container with an uncertain command outcome requires reconciliation. Recovery cleans up owned containers and retains the decision in the journal. It does not start the uncertain command again.

A timeout or output overflow terminates the local process group and removes the owned Docker container. Host executions have no container to inspect after a service crash; an uncertain host command outcome requires operator review. Keep the [run journal and private configuration backups](recovery.md) together when restoring an installation.

The executor permits at most two active commands across runs and serializes commands within each run. The engine's project scheduling controls still apply above that limit.
