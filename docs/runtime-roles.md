<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Separate API admission from execution

Choose the runtime topology before starting an installation. The default combined service serves the API and owns execution. A split installation has API replicas and headless worker replicas, with one execution leader and waiting standbys. Every process uses PostgreSQL and the same private configuration, workspace and artifact storage.

| Process | Start command | Responsibility |
| --- | --- | --- |
| Combined | `forge serve --role combined --data-dir /srv/forge` | Serve the API and execute queued work |
| API replica | `forge serve --role api --data-dir /srv/forge` | Authenticate requests, check project access, admit work and read results |
| Headless worker | `forge worker --data-dir /srv/forge` | Wait for leadership, then execute queued work and retain command, verification and delivery receipts |

`FORGE_SERVICE_ROLE` selects the role when no explicit serve option is given. `forge worker` always selects the worker role and opens no HTTP listener. Configure the database and identity settings described in [installation](installation.md) and [sign-in](identity.md). Serve remote users through one HTTPS origin.

## Share the actual retained state

Every process in an installation must use the same database, schema, private storage root and operating-system identity. The topology record binds the resolved storage path, owner ID and a retained random `runtime-storage.identity` marker. That marker must remain a private regular file with one link and mode 0600. Separate storage roots with the same path and owner ID have different markers and are rejected.

A copied backup can carry the same marker, so marker equality does not prove a live shared filesystem. Multiple machines require a verified read-write shared volume with working file locks, atomic publication and durable writes. Mount and verify the actual shared storage before starting workers. Local multiprocess qualification does not establish multi-host failover.

Workspace and runner receipts retain absolute storage paths. Every failover node must see the same retained files at those paths before it can reconcile or continue an operation.

Before an older installation first adopts the marker, stop **all** existing runtime nodes, including API replicas. The first upgraded startup preserves the original immutable topology row and records an additional immutable storage attestation after validating its mode, path and owner. A PostgreSQL execution fence excludes a live leader during adoption; old API processes are not detectable through that fence. Keep the new marker with the installation's private state and backups. Never delete or replace it to make a different storage root pass admission.

The first startup records `combined` or `split` in PostgreSQL. Later processes cannot change that choice. Starting an API replica against an existing combined installation is rejected; a second combined execution owner is also rejected. There is no automatic topology conversion. Plan an explicit maintenance procedure and preserve the ledger and source together before changing an existing installation.

In a split installation, additional headless workers remain in `standby` while the existing leader holds either ownership fence. A worker must acquire both the private storage lock and the PostgreSQL schema advisory lock before recovering retained work or starting any execution consumer. Standbys retry contention every half second; database, topology and configuration errors cause startup to fail. A stop signal cancels waiting startup without promoting the worker.

After a leader exits, a standby can acquire the released fences, recover retained work and become the new leader. Recovery rechecks ownership between services. Interrupted work with an intent but no durable receipt requires reconciliation; promotion does not repeat uncertain commands or remote effects. Losing the database ownership session drains the old leader and stops its consumers before its storage fence is released. A stuck process or an unavailable shared filesystem can therefore delay promotion; supervisors must stop the old process and verify retained work.

`FORGE_CONCURRENCY` sets the active leader's coding/test-job budget, from 1 to 64 (default 2). The scheduler also admits at most one active job per repository. Standbys add availability, not execution capacity: two replicas still share one leader's budget. This is not an active distributed worker pool, and it does not provide independent storage replicas or deployment-target failover.

For a new Compose installation, use `docker compose -f compose.split.yaml up --build --scale worker=2 -d` with the same database and identity environment required by the default Compose file. The split example shares one private data volume and exposes only the API service on loopback. Put that API behind the installation's HTTPS origin. Do not start the combined Compose service against the split schema. Moving workers to multiple machines requires separately verified shared storage, matching paths and the same operating-system owner; a same-host Compose volume alone does not establish cross-host availability.

## Follow admission through delivery

An API replica may resolve bounded Git metadata, but it does not run project commands, publish branches, deploy products or execute rollback commands. Publication and cancellation create persisted execution requests; deployment and rollback have their own persisted operation records. The worker rechecks authority and source before dispatch.

HTTP acceptance means the request was retained. It does not mean a pull request exists or a worker has stopped. Inspect the request's status and receipts, then the recorded publication URL or deployment checks. [Recovery](recovery.md#reconcile-worker-delivery-requests) explains what happens when an effect has an uncertain result.

Admission snapshots preserve the requesting identity, but they do not freeze its authority indefinitely. The worker checks current local identity status and project permissions before effects. An OIDC administrator also needs a current, unexpired validated API-client grant matching the admitted audience. An expired or removed global role cannot authorize host coding or host verification. Current project membership may still permit isolated work. Refresh sign-in before relying on administrator authority for a long-running job; a queued administrator flag is not a renewal. See [identity](identity.md) for token ordering and provider-revocation limits.

Product conversations and identity administration can run on an API process. Their own ownership and recovery controls apply. Some explicit channel messages and verified-source knowledge indexing still require the execution owner; they are not queued through an API replica. Check the operation's contract before routing every write to a split API.

## Read health accurately

`GET /api/v1/health` reports the process role, `admission_ready` and `execution_ready`. A healthy API replica reports `execution.state = api_only` and local execution disabled. It can accept authorized work without claiming that a remote worker is available; `remote_worker_state` remains `unknown`.

On an execution process, `execution.state` distinguishes `standby`, `recovering`, `ready`, `stopped` and `degraded`. Only `ready` reports `execution_ready = true` and a nonzero `max_parallel_jobs`. A standby has admitted its topology but has no execution consumers or capacity; headless workers expose no HTTP listener, so their supervisors must also track process state and the retained ledger. An API replica continues to report remote worker availability as unknown.

Every required leader consumer is supervised: coding, delivery requests, deployments, campaigns, repository-channel work and application observations. A stopped consumer degrades health; a headless worker drains its lifecycle and exits so its supervisor can handle the failure. Inspect retained work before restarting. Neither a successful health response nor an automatic restart resolves an unknown external effect.
