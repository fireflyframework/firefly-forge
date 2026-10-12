<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Understand the factory

Forge is a PyFly HTTP application with a background run engine, SQLite state,
isolated Git workspaces and interchangeable coding providers. Studio uses the
same authenticated HTTP API as other clients.

![Studio and API clients enter the authenticated Forge API, which stores runs and dispatches isolated Git generation, verification and optional publication](diagrams/architecture.svg)

**How to read this diagram:** Studio and automation share one API boundary.
The run engine manages local workspaces and provider execution. Git credentials,
CLI logins and API keys belong to the server environment.

[Open the diagram at full size](diagrams/architecture.svg)

## One run, one workspace

Forge clones the repository's configured default branch into a dedicated run
workspace and creates a run branch. The source checkout is not the editing
workspace. Clone isolation preserves local source edits, but it is not an
operating-system sandbox for generated code.

The engine records the starting revision, brief and requested profiles, invokes
the selected provider, then examines actual file changes and executes
verification. It retains a diff, changed paths and artifacts. Command failure,
provider failure and cancellation are visible outcomes.

Failed verification feeds complete failed-check evidence back to the provider
for a bounded repair loop. The default is two additional repair attempts. Forge
keeps earlier check results and records the final passing verification against
the exact source-tree snapshot; publication rejects subsequent source changes.

## Durable records, local execution

The state store persists repository and run records. A stopped process can leave
an interrupted run; startup recovery handles the recorded state. This is a
single-node local execution service, not a distributed scheduler with remote
leases or a multi-tenant control plane.

An exclusive file lock gives one active service ownership of each data
directory. Multiple local runs share that service's configured concurrency.
Separate services require separate data directories.

## Publication is separate

The default request has `publish: false`. Explicit publication uses the source
control adapter after verification, and can push a branch and create a pull
request on a supported provider. Review the publication request and server's
credentials carefully; publication changes a remote repository.

See [operations](operations.md), [Git adapters](scm.md) and [security](security.md)
for practical boundaries.
