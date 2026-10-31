---
description: Connect a private GitHub Projects board to a native project and synchronize verified issue and pull request progress.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Track delivery in GitHub Projects

Forge can create a private personal board or connect an existing private personal or organization board. A project/repository binding selects one single-select field and its exact option IDs. The execution owner queues progress updates from recorded runs and their verified issue or pull request receipts.

Board access uses a separately consented native-user OAuth connection. The repository GitHub App and its installation permissions remain separate. A `gh auth` scope does not configure Forge, and these routes never import the CLI's token.

## Connect the account

A current factory administrator registers a confidential GitHub OAuth App through [Connected accounts](connected-accounts.md). Register the callback URL shown for the exact connection. Select `github-projects` for public repository content, or `github-projects-private` when private repository content is required. Both profiles use the fixed GitHub authorization/token endpoints and `https://api.github.com/graphql` resource.

| Profile | Requested OAuth scopes |
| --- | --- |
| `github-projects` | `project` |
| `github-projects-private` | `project repo` |

Register one project-scoped connection for the consenting native user. The existing administrator/browser consent checks apply. The grant retains its native actor, registration revision and encrypted token family. A different actor cannot borrow it. Reconnecting changes the grant family and requires reviewing the board binding again.

```sh
forge connections oauth configure delivery-board \
  --provider github-projects-private \
  --project-id "$PROJECT_ID" \
  --client-id "$GITHUB_OAUTH_CLIENT_ID" \
  --client-secret-file "$PRIVATE_CLIENT_SECRET_FILE"
forge connections oauth prepare delivery-board
```

Open the returned continuation URL in the authenticated factory browser. The flow uses state, S256 PKCE and the callback nonce. GitHub's callback may omit `iss`; only the exact admitted GitHub profile permits that omission. Tokens with an explicit lifetime retain that lifetime. An absent GitHub lifetime remains `null`, including after encrypted storage; Forge does not invent an expiry or a refresh token. Disconnect, current project access and provider-policy checks still apply to every resource request.

GitHub documents the separate [Projects API permissions](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects) and [OAuth browser flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps). Live consent and account access must be verified for the installation that will use the board.

## Create or discover the board

`forge project github-projects create PROJECT REPOSITORY create.json` submits one `BoardCreateInputV1`. Save its request ID before submitting:

```json
{
  "version": 1,
  "request_id": "11111111111111111111111111111111",
  "connection_name": "delivery-board",
  "owner_kind": "user",
  "owner": "YOUR_GITHUB_LOGIN",
  "title": "Delivery",
  "acknowledge_private_creation": true
}
```

Replace the example request ID and login. The current OAuth viewer supplies the owner node ID; the caller cannot supply another node or organization. Forge records a native intent before `createProjectV2`, then checks the returned board and an independent node readback for exact identity, owner, title and private visibility. It adds no issue content until that readback passes.

Use `forge project github-projects creation PROJECT REPOSITORY REQUEST` to read the retained creation receipt. A lost response or interruption leaves `unknown`. Repeating that ID only reads custody. Another connection alias cannot bypass an unresolved creation for the same project, repository, GitHub owner and title. Read-only discovery can establish that a board exists, but cannot prove that an unknown request created it. An operator can explicitly connect the chosen board while preserving the unknown creation record.

For an existing board, submit `BoardDiscoveryInputV1` with `version`, `connection_name`, `owner_kind`, `owner` and `number` to `forge project github-projects discover PROJECT REPOSITORY discovery.json`. Organization boards require the connected viewer's current update permission. Personal boards must belong to the connected viewer. Public, closed, inaccessible or incomplete boards are refused.

Discovery checks the registered GitHub repository's numeric identity and name, follows provider cursors, and returns the exact single-select fields and options. The GraphQL adapter accepts only its fixed operation documents. It follows no redirects, makes no automatic mutation retry, and refuses partial, ambiguous or oversized individual HTTP responses.

## Configure the managed field

Save `BoardConnectionInputV1` through `forge project github-projects configure PROJECT REPOSITORY connection.json`. Its discovery fields are extended with:

- A new `request_id` and the current binding's `expected_digest`, or `null` for the first configuration.
- The discovered `board_id` and `field_id`.
- `state_options`, mapping each of `queued`, `running`, `paused`, `needs_reconciliation`, `failed`, `cancelled`, `interrupted` and `succeeded` to a discovered option ID. Several states may share an option.
- `enabled`, `include_existing` and the explicit `acknowledge_managed_field: true`.

The factory rediscovers the board before saving. Native current project/repository permissions, credential admission and expected revision protect the write. A repeated request returns its original configuration receipt and cannot overwrite a later revision. Disabling preserves the configuration history.

The binding's original run-sequence boundary survives later mapping revisions. `include_existing: false` excludes earlier runs; the background scanner pages through later native runs. Its cursor pages are not a global project or history limit.

## Synchronize and inspect trace

`forge project github-projects sync PROJECT REPOSITORY RUN sync.json` admits a `BoardSyncInputV1` containing `version`, a stable `request_id` and `expected_binding_digest`. It grants no coding, resume, answer, publication or product-completion authority. The execution owner rechecks current actor, project/repository binding, OAuth grant and run facts before effects.

The original run supplies the remote issue and latest proven pull request. A caller cannot choose arbitrary content IDs, URLs or a desired state. A checkpoint child must retain its original project/repository/component lineage. Each add-item and managed-field update has a committed native intent, provider result and exact readback. GitHub's `clientMutationId` correlates the response; it supplies no remote idempotency or compare-and-swap guarantee.

The receipt retains native operation, delivery scope, source checkpoint, branch, checkpoint revision, source tree and campaign identity. GitHub receives issue/pull request cards and the managed state. This slice does not write a custom remote branch/revision field or GitHub milestone. A successful run is scoped unit evidence; the projection never certifies whole-product acceptance. The current projection keeps successful units in the configured paused/review state.

Board items are read by cursor pages. The worker retains the selected item's data instead of buffering the full board; cursor and duplicate-identity metadata still grows with traversal. Missing/redacted content, duplicate matches and an incomplete field read cannot establish absence. Current human field changes produce a conflict rather than being overwritten.

Use `forge project github-projects receipt PROJECT REPOSITORY REQUEST` for exact custody. `observe PROJECT REPOSITORY REQUEST observe.json` accepts `version` and `expected_request_digest`, reads an unknown outcome with current credentials and records an observation. It preserves `unknown`, reports attribution as unknown and authorizes no replay. A predecessor's unknown add remains held even if a later configuration selects another field.

## HTTP, SDK and MCP

The route base is `/api/v1/projects/{project_id}/repositories/{repository_id}/github-projects`.

| Method | Suffix | Contract |
| --- | --- | --- |
| GET | `/connection` | `BoardConnectionViewV1` |
| POST | `/creations` | `BoardCreateInputV1` to `BoardCreationReceiptV1` |
| GET | `/creations/{request_id}` | Retained creation receipt |
| POST | `/discover` | `BoardDiscoveryInputV1` to `BoardDiscoveryV1` |
| PUT | `/connection` | `BoardConnectionInputV1` to current view |
| POST | `/runs/{run_id}/sync` | `BoardSyncInputV1` to retained request receipt |
| GET | `/requests/{request_id}` | `BoardRequestReceiptV1` |
| POST | `/requests/{request_id}/observe` | `BoardObserveInputV1` to retained receipt |

`ForgeClient` exposes the same typed contracts and checks returned native scope. The eight factory MCP tools are `create_github_projects`, `get_github_projects_creation`, `discover_github_projects`, `configure_github_projects`, `get_github_projects_connection`, `sync_github_projects`, `get_github_projects_request` and `observe_github_projects`. Every tool description comes from the packaged versioned YAML catalog. These operator tools are separate from implementation-worker grants. Personal read-only API tokens do not admit this route family.

## Storage and acceptance

Schema revision 34 follows activity revision 33. Board heads, immutable configuration receipts, creation intents and creation receipts use native pyfly Data repositories and the factory PostgreSQL ledger. The upgrade makes only OAuth grant expiry nullable; earlier grant values and ciphertext stay unchanged. Attempt and effect leases retain their expiry requirements. Existing source profiles, run snapshots and original workspaces are not migrated by board configuration.

Controlled HTTP tests and native PostgreSQL checks are separate from live GitHub acceptance. This feature does not claim a live board, OAuth consent, production webhook delivery, remote milestone synchronization or installed source-profile qualification. Studio board setup is not part of this backend/SDK/MCP slice.
