---
description: Scope large products across components and assess the factory's current capacity boundaries.
---

# Work on a large product

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

A product can outgrow one repository, one delivery campaign and one model conversation. Keep its customer journeys and integration contracts in the product definition. Assign implementation work to components with explicit source roots, dependencies and executable checks. The project remains the shared product identity across deliveries.

## Choose the unit of work

| Scope | Responsibility | Progress evidence |
| --- | --- | --- |
| Product | Journeys, features, integration contracts, architecture and its own design system. | Reviewed behavior and assembled-product acceptance. |
| Component | A source root, supported stack, responsibility and checks. | Exact source and matching check receipts. |
| Campaign | Selected components and their upstream dependencies. | Component attempts and dependency gates. |
| Run | One build, maintenance or test execution. | Plan, task attempts, source and journal. |
| Checkpoint | A specific intermediate source change. | Commit, tree and local or remote delivery receipt. |
| File window | A requested portion of a retained diff. | Selected manifest, byte range and content hash. |

For example, a CRM might separate identity, contacts, sales and its web application. A contact-import feature selects the contacts component and its dependencies. Assembled-product checks still need to cover authentication, imports and the resulting customer journey.

A monorepo uses nonoverlapping component roots within one registered repository. Separate repositories retain their own delivery identities. forge currently permits one active run per repository; dividing a monorepo into components does not add concurrent write ownership.

## Keep context proportional to the task

Retain complete source evidence outside the model conversation. Give the worker its reviewed task, relevant contracts, selected knowledge and exact source references. A preview helps navigation; it does not establish that every changed file was inspected.

The [diff archive](git-diff-archive.md) stores changes in chunks with a paged file index. An authorized client selects files and follows their content windows. Page size controls one read's memory and transport cost. The optional total-byte budget controls how much diff work the operator permits.

## Configure the delivery

1. Review the affected journeys, integration contracts and failure behavior, including API-only processes.
2. Select component roots and supported stacks. Configure dependency direction and component and assembled-product checks.
3. Choose [coding and review providers](model-profiles.md) and check actual runner readiness.
4. Configure [checkpoint policy](development-workflow.md) and diff storage work. Allow space for temporary Git and verification output above the storage floor.
5. Admit the campaign and retain its frozen source and configuration references. Inspect component evidence before accepting a dependency or delivery gate.
6. Resolve an uncertain remote effect through its existing receipt. A timeout does not establish whether a remote write happened.

The automatic worker handoff uses [prepared source identity](source-catalog-preparation.md) for command and subsequent provider admissions. Its catalog is streamed outside writer units and retains the existing canonical source digest. Other callers and transports still have separate constraints.

## Current capacity boundaries

| Area | Current behavior | Consequence |
| --- | --- | --- |
| Diff retention | Chunked local artifacts; `diff_budget.max_bytes: null` adds no fixed total-patch ceiling. | Deadlines, free-space checks and deployment resources still apply. |
| Legacy source identity | Remaining synchronous callers reject more than 100,000,000 source bytes or 10,000 inventory entries. A source-inventory path also has a 4 MiB output limit. | Elastic diffs alone do not qualify repositories beyond these constraints. |
| Product definition | At most 50 journeys, 100 features, 50 integrations and 20 processes, within 80,000 serialized JSON characters. | Detailed contracts can exceed the aggregate bound before the individual list limits. Versioned, paged product entities remain required for larger definitions. |
| Component topology | Project topology allows 50 components; the separate product architecture model allows 30. | These description boundaries still need a coordinated extension for larger products. |
| Project discovery | The native listing returns the newest 200 projects without a cursor. | Older projects can be absent from discovery; known-ID access still requires current permission. |
| Execution | One active run per repository. | Separate repositories can run in parallel; monorepo write execution is serialized. |
| Source acquisition | Git clone runs on the controller with time and output limits. | Clone disk-growth supervision is not implemented; runner container controls do not cover this step. |
| Runner archive transport | Default workspace allowance is 64 MiB, configurable up to 256 MiB. | A streamed diff does not remove this separate archive-transfer ceiling. |
| Campaign report | Snapshot bounds and a shared 16 MiB evidence budget apply before display pagination. | An oversized snapshot returns unknown evidence without partial verified totals. See [report limits](project-campaigns.md#read-a-campaign-evidence-report). |
| Artifact durability | Storage is local. | Object storage, resumable multipart transfer and distributed replication require further implementation and qualification. |
| Agent inspection | Typed API, SDK and read-only MCP file-page reads exist. | Agents must explicitly select pages; automatic task-based page selection is not implemented. |
| Cross-repository delivery | Separate Git and publication outcomes. | A component checkpoint does not establish an atomic product-wide release. |

Qualify the intended source volume, file count, dependency graph, journal history and concurrent workload. Measure memory, storage and elapsed time with that workload. Exercise restart and unresolved-effect recovery against the same source identities before claiming capacity for that product.

See [factory concepts](factory-journeys.md), [campaigns](project-campaigns.md), [knowledge reuse](learning.md) and [capabilities](capabilities.md).
