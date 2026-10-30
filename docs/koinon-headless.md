---
description: Configure the Koinon headless candidate, map repository scopes, retrieve cited sources and inspect SQL request custody.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Connect the Koinon headless candidate

New knowledge configurations select Koinon. This candidate connects Forge's knowledge manager, HTTP API, Python SDK, MCP tools and Studio to Koinon's native headless SDK. It supports scoped cited search, catalog access checks, dataset schemas, version-bound document chunk pages and explicit SQL requests.

The dependency is pinned to [Koinon commit `17871694`](https://github.com/fireflyframework/firefly-koinon/tree/17871694c7d85cfcfa0313397bb8ebda85f63342), whose SDK version is `26.10.0a1`. This is a development candidate. A stable SDK release, live service acceptance, native database qualification and the remaining migration workflows are release gates.

## Map an existing catalog scope

A factory administrator opens **Connections → Knowledge** and enters the Koinon API address, tenant and scoped machine token. Select a Forge repository and supply its existing Koinon workspace and project catalog IDs. An optional collection narrows document retrieval; explicit dataset IDs authorize schema and SQL targets. A repository name never establishes a remote mapping.

Use a machine grant restricted to the mapped workspace and required operations. Retrieval needs `knowledge:search`; catalog and schema reads need `catalog:read`; SQL needs `query:execute`. Forge checks its current repository permissions as well as Koinon's current `workspace_permissions`. A tenant administrator label alone does not supply a workspace grant. Browser cookies are not forwarded. The server retains the token privately; Studio and configuration reads receive only its configured state.

**Save Koinon mapping** stores configuration. **Check mapped access** verifies authentication, the mapped catalog ancestry and catalog readiness. It does not prove retrieval, indexing, successful queries or migrated memory. Use **Try a project search** to inspect an actual cited reference.

An optional shared framework mapping uses its own explicit workspace/project/collection. Exact-version official framework corpus refresh is still a separate migration gate. Existing frozen framework pins are never replaced with today's catalog mapping.

## Read sources and schemas

| Operation | HTTP API | Python SDK | MCP tool |
| --- | --- | --- | --- |
| Save connection | `PUT /knowledge/koinon/configuration` | `configure_koinon_knowledge` | `configure_koinon_knowledge` |
| Inspect saved connection | `GET /knowledge/configuration` | `knowledge_configuration` | `get_knowledge_configuration` |
| Check mapped access | `POST /knowledge/test` | `check_koinon_knowledge` | `check_koinon_knowledge` |
| Retrieve cited sources | `POST /knowledge/search` | `search_knowledge` | `search_project_knowledge` |
| Read dataset schema | `GET /knowledge/schema` | `knowledge_schema` | `get_project_knowledge_schema` |
| Read a document page | `GET /knowledge/chunks` | `knowledge_chunks` | `read_project_knowledge_chunks` |

All paths use the `/api/v1` prefix. Schema reads require `repository_id` and `dataset_id`. Chunk reads require `repository_id` and `document_id`, with `offset` and `limit`. Continue a chunk page using its returned `next_offset` and `source_identity_sha256` as `expected_identity`. Changed source/version/extraction identity refuses the next page instead of joining different versions.

Search uses Koinon's native 8,000-character question and 1–100 result contract. It does not apply the retained Flycanon adapter's shorter query, content or result caps. Schema fields and chunk pagination remain native SDK responses. Missing version metadata stays `null`; recorded citation metadata still has `availability: unverified` until independently checked. Foreign tenant, workspace, project, collection or citation identities refuse the result.

## Submit explicit SQL once

`POST /runs/{id}/knowledge/queries` accepts exactly `request_id`, `dataset_id` and `sql`. The request ID is a fresh 32-character lowercase hexadecimal identity. The dataset must be mapped to the run's frozen Koinon connection. Current execute permission is required.

The factory records a native journal intent before its one remote call and a receipt when it observes the response. The receipt includes Koinon's query ID, execution status, reported snapshot pins and response digest. Result rows are returned to that caller but are not retained in the journal. Repeating the same completed request returns its receipt with `result_state: not_retained`; it does not run the query again.

After a lost response, use `GET /runs/{id}/knowledge/queries/{request_id}`, `knowledge_sql_request` or `get_project_knowledge_sql_request`. An uncertain request reports `outcome_unknown` with no invented query ID. The factory refuses another dispatch for that request and fresh SQL requests on the same run while an earlier outcome remains uncertain. Status reads inspect local WAL only; they do not claim that the remote service cancelled or never received the request.

This endpoint supplies SQL explicitly to Koinon's native read-only SQL path. It does not select a model or generate SQL from a question. Model-generated queries need a separate explicit model, usage budget, durable operation identity and uncertain-outcome reconciliation workflow before enablement. Controlled HTTP tests do not qualify the live executor's confinement or dataset policies.

## Preserve retained integrations during migration

A saved provider-less Flycanon configuration and queued run snapshot retain their existing format and credentials. The candidate refuses to relabel that binding as Koinon. Fresh Koinon configuration does not copy legacy sources or memories and does not fall back to legacy writes.

Source ingestion, verified-memory publication, official framework refresh, corpus browsing and migration receipts remain pending. Memory publication must preserve versioned source identity and use Koinon's preview/confirmation contract. AI-derived memory must have `external_proposal` origin; it cannot become a `user_statement`. The current Koinon writer paths refuse with an actionable migration message. Required retrieval remains enabled; a refusal is not an empty corpus or successful migration.

The retained [Flycanon knowledge guide](knowledge.md) and [Flyquery analytics guide](structured-knowledge.md) describe those existing bindings. Their managed provisioning, Compose defaults and migration to Koinon still need integration work before a complete platform cutover can be claimed.
