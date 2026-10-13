<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Separate API admission from execution

Choose the runtime topology before starting an installation. The default combined service serves the API and owns execution. A split installation has API replicas and one headless worker. Both use PostgreSQL and the same private configuration, workspace and artifact storage.

| Process | Start command | Responsibility |
| --- | --- | --- |
| Combined | `forge serve --role combined --data-dir /srv/forge` | Serve the API and execute queued work |
| API replica | `forge serve --role api --data-dir /srv/forge` | Authenticate requests, check project access, admit work and read results |
| Headless worker | `forge worker --data-dir /srv/forge` | Execute queued work and retain command, verification and delivery receipts |

`FORGE_SERVICE_ROLE` selects the role when no explicit serve option is given. `forge worker` always selects the worker role and opens no HTTP listener. Configure the database and identity settings described in [installation](installation.md) and [sign-in](identity.md). Serve remote users through one HTTPS origin.

## Share the actual retained state

Every process in an installation must use the same database, schema, private storage root and operating-system identity. The topology record binds the resolved storage path and owner ID. Matching path names alone do not prove that two machines share a filesystem: mount and verify the same storage before starting them.

The first startup records `combined` or `split` in PostgreSQL. Later processes cannot change that choice. Starting an API replica against an existing combined installation is rejected; starting a second execution owner is also rejected. There is no automatic topology conversion. Plan an explicit maintenance procedure and preserve the ledger and source together before changing an existing installation.

This split supports API replicas and **one execution worker**. The worker can run several bounded jobs according to its configured concurrency. It is not a distributed worker pool; do not start multiple worker leaders or treat replica count as execution capacity.

## Follow admission through delivery

An API replica may resolve bounded Git metadata, but it does not run project commands, publish branches, deploy products or execute rollback commands. Publication and cancellation create persisted execution requests; deployment and rollback have their own persisted operation records. The worker rechecks authority and source before dispatch.

HTTP acceptance means the request was retained. It does not mean a pull request exists or a worker has stopped. Inspect the request's status and receipts, then the recorded publication URL or deployment checks. [Recovery](recovery.md#reconcile-worker-delivery-requests) explains what happens when an effect has an uncertain result.

Admission snapshots preserve the requesting identity, but they do not freeze its authority indefinitely. The worker checks current local identity status and project permissions before effects. An OIDC administrator also needs a current, unexpired validated API-client grant matching the admitted audience. An expired or removed global role cannot authorize host coding or host verification. Current project membership may still permit isolated work. Refresh sign-in before relying on administrator authority for a long-running job; a queued administrator flag is not a renewal. See [identity](identity.md) for token ordering and provider-revocation limits.

Product conversations and identity administration can run on an API process. Their own ownership and recovery controls apply. Some explicit channel messages and verified-source knowledge indexing still require the execution owner; they are not queued through an API replica. Check the operation's contract before routing every write to a split API.

## Read health accurately

`GET /api/v1/health` reports the process role, `admission_ready` and `execution_ready`. A healthy API replica reports `execution.state = api_only` and local execution disabled. It can accept authorized work without claiming that a remote worker is available; `remote_worker_state` remains `unknown`.

On an execution process, every required consumer is supervised: coding, delivery requests, deployments, campaigns, repository-channel work and application observations. A stopped consumer degrades health; a headless worker drains its lifecycle and exits so its supervisor can handle the failure. Inspect retained work before restarting. Neither a successful health response nor an automatic restart resolves an unknown external effect.
