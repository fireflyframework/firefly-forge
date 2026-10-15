---
description: Configure required flycanon retrieval, inspect citations and retain verified project memory with repository scope and source provenance.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Retrieve knowledge and retain verified project memory

[flycanon](https://github.com/fireflyframework/flycanon) is part of the managed forge installation. The official [Flycanon Python SDK](https://github.com/fireflyframework/flycanon/releases/tag/v26.10.4) retrieves framework references, selected codebase context and verified project memory from its authenticated service. A fresh managed installation enables required knowledge and automatic verified memory. Existing configured installations retain their saved choices and queued snapshots.

## Pinned service and SDK

The managed installer verifies the published flycanon `26.10.4` server, source archive, runtime requirements and configuration before installation. Forge uses the matching Python SDK. The server runs in a separate framework environment; its qualified pyfly `26.09.04` event contract does not replace forge's pyfly `26.10.6` dependency.

Workspace creation requires the body `id` to equal `X-Workspace-Id`. Forge's knowledge connection selects the target workspace before creating it, including project-scoped learning workspaces. A mismatch returns `400 workspace_scope_mismatch` before storage or event publication. Existing credentials, corpora and saved run configuration retain their original state; upgrading the SDK does not authorize a retry of an unknown remote effect.

## Service responsibilities

| Service | Responsibility | Authority |
| --- | --- | --- |
| forge PostgreSQL and journal | Run admission, project permissions, verified source/check evidence and learning eligibility | Current factory records authorize work and retain observed outcomes. |
| flycanon | Cited framework manuals, admitted project documents and canonical versions of verified memory | Source/chunk/version receipts establish provenance within the selected workspace. |
| flyquery, assessed optional integration | Questions over explicit structured snapshots, such as check durations or procedure outcomes | No forge adapter, managed service or configuration screen is implemented. |

A structured query result cannot change a run's status, approve source, satisfy a delivery gate or create verified memory. [Structured analytics architecture](structured-knowledge.md) defines the proposed flyquery boundary and its admission prerequisites. Keep flycanon required for the managed factory.

## Set up knowledge from a fresh installation

1. Complete [installation and identity setup](installation.md#run-the-complete-local-stack), then start the supplied Compose stack. Bootstrap, database migration, model download and framework initialization have separate outcomes.
2. Check that `knowledge-initialize` completed successfully. It verifies embedding-backed retrieval; Flycanon's version health check alone cannot establish this.
3. Sign in to Studio and open **Knowledge**. Review the saved address, tenant, shared framework workspace, required retrieval and automatic memory settings.
4. Select **Check knowledge access**, then choose the registered repository under **Try a project search**. Inspect the expected framework or project citation, including its workspace and source identity.
5. Run a small project with executable acceptance checks. Inspect its source/checkpoint evidence, index an explicit verified path when needed, then retrieve its saved memory from the same repository scope.

Studio's Knowledge setup is a settings form followed by an authentication check, project search and source explorer. **Save knowledge settings** retains configuration; **Check knowledge access** tests platform authentication. Neither button installs a server or downloads an embedding model. The private CA is configured through the API; the current form has no CA input. There is no flyquery setup wizard.

## Structured learning evidence

The learning-ledger runtime retains local procedure outcomes separately from the memory summary. An outcome needs passing command receipts and a matching checkpoint in the write-ahead journal. Source inspection must match the retained base revision, checkpoint revision and verified tree. Framework and dependency declarations come from bounded regular files in that exact Git tree; edits to the working directory cannot supply a different version for verified evidence.

Reuse follows the consuming run's frozen policy. The effective expiry is the earlier of the original record's expiry and its verification time plus the consuming run's maximum age. Reobserving the same outcome preserves its original verification time. A stricter project policy can therefore require refresh without changing the older record.

Projection into flycanon records an intent before writing. An execution worker checks its current PostgreSQL ownership before remote writes, after source submission and before accepting the receipt. A manual reviewer needs current project review permission. Lost ownership, revoked access or an uncertain response leaves an unknown outcome for explicit observation; another submission does not replay it. Source admission is separate from indexed, content-matching retrieval.

Project learning uses `forge-learning-` plus a 48-character digest of the full project and repository identities. This 63-character name fits flycanon's workspace contract and keeps those scopes separate. The full identities remain in the evidence body and journal. Unassigned repository learning retains its existing repository-based name. Once a projection records its target, observation continues against that exact workspace; a naming change cannot redirect an uncertain or admitted source.

These contracts do not grant shared framework learning, an automatic projection scheduler or a negative procedure that can bypass testing. Those capabilities require separate admission and verification.

<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/knowledge-lifecycle-narrow.svg">
  <img src="../diagrams/knowledge-lifecycle.svg" alt="Initialize authenticated knowledge, retrieve exact project and framework citations, implement and verify source, then index selected files and save bounded memory with write-ahead receipts.">
</picture>

[Open the knowledge lifecycle](diagrams/knowledge-lifecycle.svg) · [Portrait layout](diagrams/knowledge-lifecycle-narrow.svg)

## Initialize the managed service

The [complete local stack](installation.md#run-the-complete-local-stack) includes flycanon server 26.10.2, its PostgreSQL database with pgvector, durable original objects and a local Ollama embedding service. Bootstrap creates a private platform key, database password, CA certificate and service certificate on first setup. The internal endpoint is `https://knowledge:8500`, tenant `forge`, with shared framework workspace `frameworks`. The knowledge port is not published to the host.

The bootstrap operation is also available as a CLI command:

```sh
uv run forge knowledge bootstrap --data-dir .forge --service-dir .forge-knowledge
```

This command writes configuration and the service bundle; it does not start Flycanon or make its internal DNS name reachable from a host process. Run service initialization from a process on the managed stack's network, using its mounted private data directory:

```sh
forge knowledge initialize --data-dir /data
```

Initialization creates the authenticated framework workspace and admits the starter catalog plus 14 licensed core manuals: seven for [pyfly](https://fireflyframework.org/frameworks/python/) 26.10.1 and seven for [larafly](https://fireflyframework.org/frameworks/php/) 26.10.1. Each framework bundle covers getting started, architecture, dependency injection, web, relational data, transactions and testing. The bundled Markdown retains its original source URI, pinned revision, SHA-256 and Apache-2.0 license.

Initialization checks embedding-backed retrieval against the admitted sources. It fails while the configured local `nomic-embed-text` model is unavailable or retrieval cannot return the expected source citations. The core bundle is bounded; additional framework modules and another product's framework version need their reviewed manuals imported into the appropriate scope. Framework initialization retains per-document admission and source receipts in private installation state. Run-level source indexing and memory use the execution journal described below; keep both records with the installation backup.

Compose runs bootstrap and initialization for the managed setup. Keep the original service bundle with the installation: repeated bootstrap preserves its credentials and validates the files. Repeated initialization reuses recorded source IDs and checks matching server receipts. A source admission with an unknown outcome and no matching server evidence blocks repetition. A missing or changed managed bundle requires restoration of its original files. Bootstrap refuses an existing externally configured knowledge connection and keeps its settings intact. Continue operating that service through its existing deployment, or plan an explicit migration after backing up the retained configuration and data. The managed Compose bootstrap cannot invent credentials for an external service. Keep private keys, passwords and connection revisions outside Git and product workspaces.

## Upgrade or use an existing Flycanon service

Before upgrading, back up factory state and Flycanon data together. An existing connection keeps its endpoint, keys, framework workspace and previous `required` / `auto_memory` settings. Review those settings in **Knowledge**; a restart does not silently replace an external service or rewrite an admitted run's snapshots.

For an existing disabled connection, configure and verify a service, then enable required retrieval and automatic verified memory for new work. Use an administrator's explicit configuration change. Preserve uncertain old memory intents and frozen connection revisions until their outcomes have been inspected.

## Configure an external service

Open **Knowledge** and set the authorized HTTPS endpoint, tenant ID and platform API key. HTTP is accepted only on loopback. A private HTTPS endpoint can provide its CA PEM through `ca_certificate`; hostname verification stays enabled. Keys are write-only and stored with the other private configuration. An optional scoped agent token uses flycanon's agent routes and idempotency keys for supported search and source-ingest operations.

```json
{
  "values": {
    "enabled": true,
    "endpoint": "https://canon.example.com",
    "tenant_id": "your-tenant",
    "api_key": "your-platform-key",
    "framework_workspace": "firefly-frameworks",
    "required": true,
    "auto_memory": true,
    "top_k": 5
  }
}
```

Send this to `PUT /api/v1/knowledge/configuration`. GET configuration and status return secret-presence flags and actual connection evidence. `POST /knowledge/test` with `{}` checks scoped `/stats` authentication through the real SDK when a platform key is configured. An agent token alone cannot establish access to the platform stats route; test its scoped search separately.

## Keep project scope explicit

A registered project's workspace is `forge-` followed by its repository ID. Retrieval searches that project's scope. If configured, it also searches the named framework workspace for shared references. It never falls back to another project's corpus.

`required: true` makes an unavailable configured knowledge scope fail the run before generation. Keep it enabled for the managed factory. An existing installation can retain a previous optional setting; that mode records a visible warning when retrieval fails and continues without that context. Empty search results are distinct from a connection failure.

A run freezes its connection configuration. Its first retrieval retains source/chunk IDs, filenames, sections, source URIs and available knowledge-version IDs. The retrieved context and digest are kept for repairs and continuation. These references are untrusted input; they cannot override the brief or core security rules.

## Add framework manuals and project references

The default bundle contains the 14 core manuals described above, plus framework links and factory conventions. Import additional reviewed, versioned framework manuals into the configured shared framework workspace through Flycanon's authorized ingestion interface. Give each source its framework version, original URI and document identity; confirm embedding and a cited search before using it for detailed API work.

Place project journeys, architecture decisions, integration contracts and selected codebase references in that project's exact workspace. Review sharing permissions before moving a project source into the common framework scope. Product-assistant file attachments are conversation inputs; attaching a file does not implicitly admit it into Flycanon or establish its embedding status.

Use the Knowledge explorer to inspect scoped sources and canonical knowledge when the platform key permits browsing. A returned citation identifies its source; it does not approve an outdated framework API or a proposed architecture. Freeze the product and framework versions that implementers should follow with the run's references.

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

`auto_memory` is enabled on a fresh managed installation. It saves a bounded summary after final verification. An existing installation retains its previous choice; enable it through the knowledge configuration when adopting automatic verified memory. An authorized operator can also call `POST /api/v1/runs/{id}/knowledge/remember`.

Memory includes the run ID, verified source tree, changed filenames and check identities with exit statuses. It excludes raw command logs and private credentials. The integration indexes that body as a real source, retrieves its actual chunk citations, and creates a canonical knowledge version referring to them. Future project-scoped searches can return the memory's source and version provenance.

The response identifies `source_id`, `item_id`, `version` and `workspace_id`. A platform API key is required for canonical memory creation. Source admission and canonical creation have separate journal intents and receipts. Unknown outcomes block another write; an earlier source receipt can be reused after delayed retrieval without admitting it again.

Verify the connection with a scoped search, then index a selected source and check its returned citations. Save one verified memory and retrieve it from the same project workspace. Admission, embedding and canonical memory creation have separate outcomes; inspect each before marking the connection ready.

## Review failures and retained data

A saved configuration proves which service was selected. `POST /api/v1/knowledge/test` with `{}` checks platform authentication and `/stats`; a scoped search establishes retrieval. Inspect both before starting a project. Source admission, embedding and canonical memory creation require their own receipts.

| Observed failure | Operator action |
| --- | --- |
| Missing private service bundle | Restore its original files and configuration; do not create a new identity over retained state |
| TLS or hostname validation failed | Check the endpoint name, certificate validity and retained CA; keep verification enabled |
| Authentication rejected | Check the selected tenant and platform-key grants without printing the key |
| Embedding model unavailable | Make the selected local model available and repeat initialization after the service is ready |
| No expected framework citation | Inspect ingestion and embedding status; preserve the admitted source before retrying a write |
| Required project retrieval failed | Check that exact workspace and grants; avoid broadening the query to another project |
| Memory write has no receipt | Inspect Flycanon for the recorded source or knowledge version; reconcile before another write |

Automatic memory retains source identifiers, run/check evidence and provenance until the operator applies the service's retention process. forge does not impose an automatic deletion period. Document the project's retention and deletion policy before admitting sensitive material. Keep original objects, vector metadata and canonical versions consistent when deleting or restoring data. Raw command logs stay in the factory's execution evidence and are excluded from the bounded memory body.

Back up Flycanon PostgreSQL and original object storage with forge's ledger and private state. Preserve CA trust, service keys, model settings and queued connection revisions. [Factory infrastructure](factory-infrastructure.md) covers the complete storage and production network layout.

## Flycanon server compatibility

Use flycanon server 26.10.0 or a build containing [the identifier-scanning fix](https://github.com/fireflyframework/flycanon/pull/76). Earlier servers can mistake numeric fragments inside run IDs and Git hashes for phone numbers, causing `pii_policy=reject` to reject verified memory before admission. The fix preserves rejection of genuine phone numbers and email addresses.

The Python SDK and flycanon server are installed separately. Updating the SDK does not apply a server-side scanner change. Keep an uncertain memory intent in the journal and inspect the source admission outcome before another write.

## Learning and reuse decisions

Verified project memory preserves observed source and check outcomes. The [learning architecture](learning.md) adds explicit question-to-evidence associations, freshness and version eligibility, scoped failure observations and the required communication contracts. Its implementation boundary is stated separately from the current retrieval and memory APIs.
