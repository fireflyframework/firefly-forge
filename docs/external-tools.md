---
description: Configure reviewed external MCP connections, approve tool schemas and grant exact project repositories while keeping credentials in the control plane.
---

# External tools

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

forge can expose reviewed external tools to a native agentic or Bridge worker. A factory administrator controls the connection, schema review and project grant. Credentials remain in the control plane; the project image and worker arguments receive no API key.

External MCP complements the native source-control, flycanon and browser integrations. It does not replace them. Direct Codex and Claude CLI execution uses the CLI's own authority and does not inherit these Python tool grants.

Follow the [provider setup checklists](tooling-prerequisites.md#github) for GitHub, Figma, Sentry, Grafana and native Playwright. Each checklist states the account, scope and adapter requirements; blocked templates remain blocked.

## Available connectors

All templates start disabled. The catalog separates an unmet prerequisite from an enabled connection that still needs discovery, review or a project grant.

| Connector | Current execution boundary |
| --- | --- |
| GitHub | Fixed `https://api.githubcopilot.com/mcp/readonly` endpoint. Reviewed `get_file_contents`, `get_commit`, `list_commits` and `list_branches` tools can read the repository assigned to the run. |
| Sentry | Template only. A project-scoped adapter and reviewed server skill constraints are still required. The current upstream issue tool can enrich results through Seer and related data; forge does not admit those calls. |
| Figma | Template only. Figma requires an approved client or waitlist admission and an authorized OAuth connection. Registering a template does not satisfy those requirements. |
| Playwright | The native isolated BrowserRunner remains the supported execution path. forge does not launch an external stdio server on the host. |
| Grafana | Template only. A reviewed managed HTTPS endpoint and project-scoped adapter are still required. Arbitrary self-hosted URLs are not accepted. |

The initial GitHub adapter accepts bounded text results. Binary blocks and resource links are rejected; forge never follows a returned resource URL. A remote tool that returns those blocks needs a reviewed adapter before it can be used. No hosted account was used to qualify this implementation: the native API, SDK transport and execution lifecycle were tested with owned local fixtures.

## Connect, review and grant

The same actions are available through the authenticated API, Python SDK, operator CLI and forge's own MCP server. Only a factory administrator can change external connections or grants. Project ownership alone does not authorize the use of a platform credential.

1. **Configure the connection.** Supply a narrowly scoped authorized credential in an owner-private file. No arbitrary URL or executable is accepted.
2. **Discover the schemas.** This makes an explicit bounded remote request. Discovery never runs a tool or grants it to an agent.
3. **Review the exact digest.** Select only the known read tools you need. Remote descriptions, instructions and `readOnlyHint` annotations cannot grant permission.
4. **Grant a project repository.** Select a registered repository belonging to the product, exact tools, implementation roles and the `implement` stage.
5. **Grant the worker capability.** The run's frozen role must also allow `external_mcp`. A project grant and a role grant are both required.

```bash
forge tools mcp catalog
forge tools mcp configure github --enable --key-file /private/path/github-key
forge tools mcp discover github
forge tools mcp review github --file /private/path/review.json
forge tools mcp grant PROJECT_ID github --file /private/path/grant.json
forge tools mcp grants PROJECT_ID
```

The private key file must be a regular owner-only file. Do not place it in the repository or project sandbox. Discovery can consume provider quota; invoke it only with an authorized account.

A review file selects the digest returned by discovery:

```json
{
  "digest": "EXACT_DISCOVERY_SHA256",
  "tools": ["get_file_contents"]
}
```

The project grant binds that review to a repository:

```json
{
  "repository_id": "REGISTERED_REPOSITORY_ID",
  "review_digest": "EXACT_DISCOVERY_SHA256",
  "tools": ["get_file_contents"],
  "roles": ["implementer"],
  "stages": ["implement"]
}
```

The API derives the GitHub owner and repository from the registered HTTPS source URL. Worker arguments must match that binding. Credential paths, parent-directory traversal and fields outside the reviewed schema are rejected.

Campaigns capture each repository's authorized MCP grants before freezing its tool catalog. They recheck the factory configuration after preflight and during admission. A grant changed during launch requires a new launch request; a revoked grant or a grant for another project does not expose the capability.

## Execution and retained evidence

The native worker calls `external_mcp(connector, tool, arguments)`. The engine inspects the actual Git base, HEAD and source tree before admitting the invocation. The PostgreSQL broker reserves the frozen call budget and writes an intent under the run lock before sending a remote request.

Each receipt links the run, task, role, project, repository, source state, schema review and credential revision. The returned packet has an invocation ID and an `untrusted` marker. Results are source material; they are not instructions and do not prove that the implementation is correct.

The broker makes one `tools/call` dispatch per invocation. It does not replay a lost response. An interrupted, rejected or unadmitted exchange remains unresolved until an operator records a reconciliation decision; unresolved outcomes block new mediated calls and source consolidation. See [Tool invocation evidence](tool-broker.md) for the durable outcome contract and explicit abandonment command.

The MCP session has a 30-second total bound, 15-second request/read bounds, a 12-request protocol budget and a 1 MB response-stream bound. The admitted result is limited to 256 KB and must also fit the run's frozen tool-response limit. Schema review rejects remote and recursive references, regular expressions, invalid schemas, excessive depth and excessive node counts. Argument objects, arrays, strings and total bytes have independent bounds.

Native tracing may add a bounded W3C `traceparent` to the request metadata. It does not alter the approved tool arguments or grant authority. Sampling, elicitation, host root listing, URL resource fetching, redirects and ambient proxy settings are not enabled by the adapter.

## Revoke or rotate access

```bash
forge tools mcp revoke PROJECT_ID GRANT_ID
forge tools mcp configure github --disable
forge tools mcp configure github --disable --clear-secret
```

Revoking a grant or disabling the connection stops future calls, including calls from an already queued run. It cannot undo a request already sent to the provider. Clearing the connection secret revokes the connector's grants.

A credential rotation creates a private immutable revision and requires discovery and review before new grants are admitted. Already queued runs retain their original credential and schema revisions while their grant remains active. To stop those runs from making another call, revoke their grant or disable the connection. Historical revision files remain private for traceability; rotation does not delete them.

Existing queued runs without an MCP snapshot have no external-tool permission. New runs capture exact project and repository grants. Project campaigns include the registry in their frozen configuration vector so a configuration change cannot silently change a child admission.

## API and client contract

| Action | Route |
| --- | --- |
| Catalog and readiness | `GET /api/v1/tools/mcp` |
| Configure fixed connector | `PUT /api/v1/tools/mcp/{connector}` |
| Explicit schema discovery | `POST /api/v1/tools/mcp/{connector}/discover` |
| Exact schema review | `POST /api/v1/tools/mcp/{connector}/review` |
| Project grants | `GET /api/v1/tools/mcp/projects/{project}/grants` |
| Grant reviewed connector | `POST /api/v1/tools/mcp/projects/{project}/connectors/{connector}/grants` |
| Revoke grant | `POST /api/v1/tools/mcp/projects/{project}/grants/{grant}/revoke` |

Python SDK methods use the same names: `external_mcp_catalog`, `configure_external_mcp`, `discover_external_mcp`, `review_external_mcp`, `external_mcp_grants`, `grant_external_mcp` and `revoke_external_mcp`. forge's own MCP exposes the equivalent administrative tools; its configure tool accepts a private key-file path rather than an API key argument.

A live connector acceptance test requires a separately authorized account, the exact returned schemas and a repository-specific result. A template, simulated transport or schema discovery is not that acceptance test.

Official connector references: [GitHub remote server](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md), [Figma remote installation](https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/), [Sentry toolkit](https://github.com/getsentry/toolkit), [Playwright MCP](https://github.com/microsoft/playwright-mcp), [Grafana MCP](https://github.com/grafana/mcp-grafana).
