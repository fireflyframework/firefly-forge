<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Deploy and roll back a verified product

forge deploys a recorded source snapshot to a named target. The operator configures the target's commands and checks, then explicitly requests deployment from a successful run. Deployment has its own retained result; it does not change the outcome of the build.

## Configure a target

Bind each target to one repository. Provide commands that build or deploy its source, then commands that test the running result. Configure rollback commands and rollback checks together if the target supports restoring a previous version.

```json
{
  "name": "Inventory staging",
  "repository_id": "your-registered-repository-id",
  "commands": [{"argv": ["./scripts/deploy-staging.sh"], "timeout": 600}],
  "verification": [{"argv": ["./scripts/check-staging.sh"], "timeout": 300}],
  "rollback_commands": [{"argv": ["./scripts/restore-staging.sh"], "timeout": 600}],
  "rollback_verification": [{"argv": ["./scripts/check-restored-staging.sh"], "timeout": 300}]
}
```

Save this body through `PUT /api/v1/deployment-targets/staging`. These are example script names: add executable scripts to the product repository that implement your actual target. The deployment host must have the required tools and an authenticated account with permission to deploy. Keep credentials in that host's credential tooling, never in command arguments or source.

Targets can invoke Docker, Kubernetes, a hosting CLI or your deployment scripts. A target's presence in the configuration does not establish that its infrastructure or permissions work. Its recorded execution and checks provide that evidence.

## Deploy the recorded source

Request `POST /api/v1/runs/RUN_ID/deploy` with `{"target_id":"staging"}`. forge rejects a failed run, a target belonging to another project, or source that changed after verification.

The worker exports the verified Git tree to a separate deployment directory. It excludes Git metadata, credentials and unsafe paths. Commands execute in that directory with bounded time and output; they do not mutate the run's original workspace. Both deployment and subsequent checks must succeed before the deployment reports success.

Use `GET /api/v1/deployments` to inspect the target, source tree, operation, command results and retained errors. Concurrent active operations on the same target are rejected. Different targets can proceed independently within the worker's concurrency limit.

## Roll back

Request `POST /api/v1/deployments/DEPLOYMENT_ID/rollback`. The worker uses the original deployment's frozen target configuration and creates a separate operation linked to it. Rollback succeeds only when the configured restore commands and restore checks pass.

The target owner defines what restoring a version means. forge cannot infer a previous database schema, container image or hosting revision from an arbitrary shell command. Verify that the rollback scripts cover the resources your product changes.

## Recover an interrupted deployment

A stopped worker may have changed the external target before recording its result. Interrupted operations remain recorded and are never replayed automatically. Inspect the target and retained evidence before requesting a new deployment or rollback.

Publication, deployment and database migration are separate actions. A pull request does not authorize a deployment, and a successful health check does not establish that every product workflow has passed. Keep the product's behavior tests and production acceptance requirements explicit.
