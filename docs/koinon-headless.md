---
description: Configure the Koinon headless candidate, map repository scopes, retrieve cited sources and inspect SQL request custody.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Connect the Koinon headless candidate

New knowledge configurations select Koinon. This candidate connects Forge's knowledge manager, HTTP API, Python SDK, MCP tools and Studio to Koinon's native headless SDK. It supports scoped cited search, catalog access checks, dataset schemas, version-bound document chunk pages, explicit SQL requests, durable source ingestion, external memory proposals and official framework corpus refresh.

The dependency is pinned to [Koinon commit `17871694`](https://github.com/fireflyframework/firefly-koinon/tree/17871694c7d85cfcfa0313397bb8ebda85f63342), whose SDK version is `26.10.0a1`. This is a development candidate. A stable SDK release, live service acceptance, native database qualification and the remaining migration workflows are release gates.

## Map an existing catalog scope

A factory administrator opens **Connections → Knowledge** and enters the Koinon API address, tenant and scoped machine token. Select a Forge repository and supply its existing Koinon workspace and project catalog IDs. An optional collection narrows document retrieval; explicit dataset IDs authorize schema and SQL targets. A repository name never establishes a remote mapping.

Use a machine grant restricted to the mapped workspace and required operations. Catalog access needs `catalog:read`. Retrieval also needs `knowledge:search`, SQL needs `query:execute`, and ingestion needs the mapped workspace’s `catalog:write` and `ingest:write` grants. Forge checks its current repository permissions as well as Koinon's current `workspace_permissions`. A tenant administrator label alone does not supply a workspace grant. Browser cookies are not forwarded. The server retains the token privately; Studio and configuration reads receive only its configured state.

**Save Koinon mapping** stores configuration. **Check mapped access** verifies authentication, the mapped catalog ancestry and catalog readiness. It does not prove retrieval, indexing, successful queries or migrated memory. Use **Try a project search** to inspect an actual cited reference.

An optional shared framework mapping uses its own explicit workspace/project/collection. New official framework refreshes use this explicit destination and retain their release, license, source and native version receipts. Existing frozen framework pins are never replaced with today’s catalog mapping.

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

## Ingest an explicit source once

New source writes require a mapped collection or corpus, current factory edit permission and the remote workspace’s ingestion grant. The factory does not create an implicit tenant, workspace or collection.

| Operation | HTTP API | Python SDK / MCP |
| --- | --- | --- |
| Admit a run source | `POST /runs/{id}/knowledge/ingestion/source` | `ingest_koinon_source` |
| Admit a project document | `POST /repositories/{repository_id}/knowledge/koinon/uploads` | `upload_koinon_document` |
| Read local run custody | `GET /runs/{id}/knowledge/ingestion/{kind}/{request_id}` | `koinon_ingestion` / `get_koinon_ingestion` |
| Observe an exact run source or memory review | `POST /runs/{id}/knowledge/ingestion/{kind}/{request_id}/observe` | `observe_koinon_ingestion` |
| Read a project admission | `GET /repositories/{repository_id}/knowledge/uploads/{id}` | `knowledge_upload` / `get_knowledge_upload` |
| Reconcile a project admission | `POST /repositories/{repository_id}/knowledge/uploads/{id}/reconcile` | `reconcile_knowledge_upload` |

Source bodies contain a fresh `request_id`, a filename and exact UTF-8 `content_base64`. Run sources commit a native WAL intent before the upload. Project documents use a native transaction and content-bound admission record; a fresh request ID for the same admitted document returns the original record. A request ID cannot change its bytes or actor. Neither path blindly resends an uncertain upload.

A submitted receipt records the upload response, not completed indexing. Explicit readback checks the source’s filename, content SHA, mapped ancestry, document identities, native source versions and extraction IDs. Missing version metadata remains unrecorded. Reconciliation proves matching scoped content; `creation_causality: not_proved` does not claim that the uncertain request created that source. Local run-custody reads make no remote request.

The existing verified-source indexing endpoint and `index_verified_source` tool use the run’s frozen Koinon binding. They read only admitted tracked files through the confined workspace reader, recheck their bytes before dispatch and retain native source/version observations. Workspace read policy still applies; the transport does not copy the legacy document adapter’s 1 MB upload ceiling. The Koinon service enforces its actual configured upload capacity.

## Stage memory without fabricating approval

The existing `remember_verified_run` workflow requires successful retained source and final check evidence. With Koinon, it commits an intent and stages an `external_proposal` through the native external-review SDK. The server records the proposal as `authority: unverified`; it never becomes a `user_statement`. A staged response is `review_required`, not saved or retrievable canonical memory.

The pinned SDK intentionally exposes no reader for the upstream private approval challenge. Forge does not invent a nonce, bypass that review or call direct confirmation for an external proposal. An authorized person reviews it in Koinon’s private application. The factory can then observe the exact remote review ID.

A committed readback must match the original owner, tenant, destination, content, evidence and authorization receipt. Forge also reads the exact native knowledge version and verifies its content/evidence hashes, operation ID, publication and serving state before recording saved memory. The external proposal origin remains unchanged after approval. A changed source or connection cannot reuse an earlier saved version as current; its previous WAL and version references remain retained.

A lost staging reply stays unknown. Repeating the workflow does not stage another proposal, and a recorded operation cannot be rebound to another review. Native review, indexing or commit uncertainty is not a cancellation or permission to retry.

## Refresh an exact official corpus

Existing framework discovery, review, refresh, observation and search API/SDK/MCP entrypoints now support the mapped Koinon destination. The existing native refresh admission, actor, lease, source inventory, license, collector policy and resource budget remain in force. Every part records an intent before its one native upload. A lost upload is resolved only by a unique scoped filename/content readback; observation does not upload the pending parts.

Readiness requires recorded native source/version/extraction identities and retrieval from the admitted assets. The corpus manifest binds those receipts as well as the immutable official release inventory. Searches filter the exact corpus assets before ranking and refuse changed native versions. Large corpora are paged and searched in the upstream contract’s 128-asset batches. Native queries retain up to 8,000 characters and the configured native result count; retained Flycanon calls keep their existing outbound limits.

These controlled contracts still require live acceptance with an actual authorized Koinon deployment. They do not establish native extractor, embedding or SQL-executor acceptance.

## Preserve retained integrations during migration

A saved provider-less Flycanon configuration and queued run snapshot retain their existing format and credentials. The candidate refuses to relabel that binding as Koinon. New native ingestion does not copy legacy sources or memories and does not fall back to legacy writes.

A complete cutover still needs an explicit migration manifest matching old and new source counts, hashes, versions and representative retrieval, with preserved rollback bindings. [Native learned outcomes](learning.md#project-native-operational-evidence) use a separate V2 source-evidence contract for fresh Koinon-bound runs. Retained V1 projections keep their original provider, payload and receipts. They cannot be relabeled as native evidence.

The retained [Flycanon knowledge guide](knowledge.md) and [Flyquery analytics guide](structured-knowledge.md) describe those existing bindings. Managed provisioning, Compose defaults, full catalog browsing, stable SDK publication, installed/native qualification and live service acceptance remain release gates. Required retrieval remains enabled; an empty destination or held migration is not successful acceptance.
