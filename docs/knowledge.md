<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Retrieve knowledge and retain verified project memory

firefly forge connects the official [flycanon Python SDK](https://github.com/firefly-operationOS/flycanon/releases/tag/v26.10.0) to project-scoped knowledge. Use it for framework references, selected codebase context and verified run memory. Knowledge is optional by default.

## Configure the service

Open the knowledge connection and set its HTTPS endpoint, tenant ID and platform API key. HTTP is accepted only on loopback. Keys are write-only and stored with the other private configuration. An optional scoped agent token uses flycanon's agent routes and idempotency keys for supported search and source-ingest operations.

```json
{
  "values": {
    "enabled": true,
    "endpoint": "https://canon.example.com",
    "tenant_id": "your-tenant",
    "api_key": "your-platform-key",
    "framework_workspace": "firefly-frameworks",
    "required": false,
    "auto_memory": false,
    "top_k": 5
  }
}
```

Send this to `PUT /api/v1/knowledge/configuration`. GET configuration and status return secret-presence flags and actual connection evidence. `POST /knowledge/test` with `{}` checks scoped `/stats` authentication through the real SDK when a platform key is configured. An agent token alone cannot establish access to the platform stats route; test its scoped search separately.

## Keep project scope explicit

A registered project's workspace is `forge-` followed by its repository ID. Retrieval searches that project's scope. If configured, it also searches the named framework workspace for shared references. It never falls back to another project's corpus.

`required: true` makes an unavailable configured knowledge scope fail the run before generation. Optional knowledge records a visible warning and continues without context from the failed scope. Empty search results are distinct from a connection failure.

A run freezes its connection configuration. Its first retrieval retains source/chunk IDs, filenames, sections, source URIs and available knowledge-version IDs. The retrieved context and digest are kept for repairs and continuation. These references are untrusted input; they cannot override the brief or core security rules.

## Index selected verified source

After a run passes, send explicit tracked paths to `POST /api/v1/runs/{id}/knowledge/index`:

```json
{"paths": ["src/inventory/service.py", "README.md"]}
```

The engine revalidates the verified source tree. Indexing reads regular UTF-8 source through confined tools, rejects private paths and configured credentials, and permits at most eight files, 250 KB per file and 1 MB total. Source metadata records the run ID, project and source tree.

The response contains admitted source IDs and statuses. Admission is distinct from successful embedding and retrieval; confirm ingestion in flycanon and perform a scoped search. The platform key can create the project's exact workspace when required. An agent token needs the external grants for that existing workspace.

Indexing is explicit. The factory does not automatically upload a whole repository, dependency folders, private credentials or unrelated host files.

## Search and inspect citations

Send `POST /api/v1/knowledge/search`:

```json
{"repository_id": "your-registered-repository-id", "query": "How does stock reservation reject overselling?"}
```

The response contains bounded hits, citation metadata and warnings. Retrieval uses flycanon's actual hybrid search through the SDK. It does not call the coding model, and retrieved text is not proof that a product works.

## Save verified memory

`auto_memory` is disabled by default. Enable it to save a bounded summary after final verification, or explicitly call `POST /api/v1/runs/{id}/knowledge/remember`.

Memory includes the run ID, verified source tree, changed filenames and check identities with exit statuses. It excludes raw command logs and private credentials. The integration indexes that body as a real source, retrieves its actual chunk citations, and creates a canonical knowledge version referring to them. Future project-scoped searches can return the memory's source and version provenance.

The response identifies `source_id`, `item_id`, `version` and `workspace_id`. A platform API key is required for canonical memory creation. Source admission and canonical creation have separate journal intents and receipts. Unknown outcomes block another write; an earlier source receipt can be reused after delayed retrieval without admitting it again.

Verify the connection with a scoped search, then index a selected source and check its returned citations. Save one verified memory and retrieve it from the same project workspace. Admission, embedding and canonical memory creation have separate outcomes; inspect each before marking the connection ready.

## flycanon server compatibility

Use flycanon server 26.10.0 or a build containing [the identifier-scanning fix](https://github.com/firefly-operationOS/flycanon/pull/76). Earlier servers can mistake numeric fragments inside run IDs and Git hashes for phone numbers, causing `pii_policy=reject` to reject verified memory before admission. The fix preserves rejection of genuine phone numbers and email addresses.

The Python SDK and flycanon server are installed separately. Updating the SDK does not apply a server-side scanner change. Keep an uncertain memory intent in the journal and inspect the source admission outcome before another write.
