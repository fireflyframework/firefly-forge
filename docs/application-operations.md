<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Observe and maintain a deployed application

Application operations links a project and deployment target to actual health observations, metric thresholds and incidents. The factory records what it checked; a configured URL or a successful deployment job is not a current health observation.

Configure the application after choosing a target belonging to the same repository. Use PyFly or LaraFly for the primary supported maintenance path, and retain the frontend actually used by that product. The application’s repository identity cannot later change.

## Configure the observation contract

Send authenticated `PUT /api/v1/applications/inventory`:

```json
{
  "name": "Inventory service",
  "repository_id": "YOUR_REPOSITORY_ID",
  "target_id": "production",
  "backend": "pyfly",
  "frontend": "react",
  "health_url": "https://inventory.example.com/health",
  "metrics_url": "https://inventory.example.com/metrics",
  "interval_seconds": 60,
  "max_latency_ms": 1500,
  "metric_thresholds": [
    {"name": "inventory_queue_depth", "operator": "gt", "value": 20}
  ],
  "observability": {
    "logs_url": "https://observability.example.com/inventory/logs",
    "traces_url": "https://observability.example.com/inventory/traces",
    "dashboard_url": "https://observability.example.com/inventory/dashboard"
  }
}
```

Use the actual repository/target IDs and endpoints. Each save creates a configuration revision. Health and metrics URLs require HTTPS, except HTTP on exact loopback addresses. URLs cannot carry credentials, query strings or fragments. These probes do not attach an application bearer token; expose a suitable endpoint through the approved private network or gateway.

Intervals range from 30 seconds to 24 hours. Health latency thresholds range from 1 to 60,000 ms. Up to 30 finite numeric metric thresholds are supported; `gt` and `lt` are strict comparisons. Log, trace and dashboard addresses are safe links to separately operated systems. forge does not ingest OTLP spans or collect those systems’ logs through these links.

## Read the actual observations

`POST /api/v1/applications/inventory/check` requests a check now. The lifecycle poller also checks due applications while it owns the execution lease. HTTP probes have a five-second timeout, do not follow redirects and reject responses above 128 KB. Probe concurrency is bounded to four.

A health response succeeds on HTTP 2xx. The checker does not interpret an arbitrary JSON health body or run a customer journey. Configure the endpoint to use an appropriate non-success status when the application is unavailable. The recorded latency is the factory’s observed probe duration; it is not a customer workload benchmark.

| Observation | Meaning |
| --- | --- |
| `healthy` | Health succeeded and no configured latency/metric condition failed |
| `degraded` | Health responded, but latency, metrics access or a configured threshold failed |
| `unavailable` | Health returned a non-success response or could not be checked within its limits |
| `stale: true` | There is no observation, its configuration revision changed, or it is older than twice the configured interval |

Read `GET /api/v1/applications` or `/applications/{id}` for observations, current configuration, linked incidents and latest deployment evidence. A previous healthy observation does not become current evidence merely because the page was reopened. A configuration change makes earlier observations stale until the new configuration is checked.

## Supply aggregated metric gauges

The metrics reader keeps at most 200 unlabelled finite numeric Prometheus samples. Labelled series, malformed values and nonfinite values are not used for threshold decisions. Publish an explicitly aggregated gauge upstream when a threshold concerns a labelled metric, rate or histogram.

For example, expose `inventory_queue_depth 12` as a gauge. Do not expect forge to sum `inventory_queue_depth{warehouse="north"}` and `{warehouse="south"}`, calculate a counter rate or evaluate PromQL. A configured threshold whose series is absent degrades the observation. The factory must not treat missing data as a passing zero.

The authenticated `/api/v1/metrics` endpoint exposes factory run/deployment counts and application health indicators for an external scraper. Those indicators derive from retained observations and staleness; they do not replace the application’s own instrumentation or service-level objectives.

## Open maintenance deliberately

A nonhealthy observation opens an incident or updates the existing open incident for that application. Read `GET /api/v1/incidents`. A later healthy check does not silently close the incident.

To request a fix, send `POST /api/v1/incidents/INCIDENT_ID/maintenance`:

```json
{
  "brief": "Fix the reservation queue backlog without changing the inventory API. Add a regression test for concurrent reservations.",
  "agent": "codex",
  "commands": [{"argv": ["make", "check"], "timeout": 600}]
}
```

Maintenance requires an open incident and at least one explicit check. The new run uses the application’s project, configured backend/frontend and incident context, with `operation: maintain` and publication disabled. It captures the normal provider, policy, customer and brand inputs and retains its own verification history. The incident stores that maintenance run ID.

A queued maintenance run is not a repaired application. Review its source diff, regressions and final verification. Explicitly publish or deploy the accepted result through the normal controls, then run another application check. Provider quota or authentication pauses use the same [recovery and handoff](recovery.md) controls as other runs.

## Resolve only after fresh recovery evidence

Send `POST /api/v1/incidents/INCIDENT_ID/resolve` with an operator explanation:

```json
{"resolution": "Deployed the reviewed reservation fix and confirmed fresh health and queue-depth observations."}
```

Resolution requires an open incident and a nonstale healthy observation under the current configuration. The explanation and resolution time are retained. The API refuses resolution based on a missing, stale or degraded observation. Use the explanation to connect the deployed change, checks and any external operational evidence; do not paste credentials or customer records.

The factory does not automatically deploy a maintenance result, resolve an incident, reverse a database migration or claim an SLA from an HTTP check. Keep the product’s support and escalation process alongside its [deployment](deployment.md), [cloud recipe](cloud-deployment.md) and [DevSecOps](devsecops.md) evidence.
