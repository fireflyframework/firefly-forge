---
description: Protect factory identity, credentials, repository authority and execution boundaries throughout admission and publication.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Protect trusted development access

firefly forge executes code for authorized operators. A run uses an isolated Git clone. Trusted host execution uses the service account's identity and filesystem access; it is restricted to factory administrators. Configured Docker runners enforce their recorded container controls for supported commands. Use a dedicated service identity with limited access to repositories and external services.

## Control the API

Production uses Keycloak OIDC with a verified issuer and API audience. A `FORGE_API_TOKEN` is available only in explicitly selected loopback bootstrap mode. Native [pyfly](https://fireflyframework.org/frameworks/python/) filters protect `/api/v1`, including health and OpenAPI. The sign-in endpoints and state-bound callbacks have their explicit authentication contracts. Management, admin and actuator surfaces are disabled, with no separate management listener.

Source-control OAuth callbacks are the narrow exception: their exact GET paths accept a single-use server-side state and an HttpOnly browser nonce cookie. Authorization starts through the authenticated operator API. A callback cannot carry or recover the operator token.

Studio uses an opaque HttpOnly session cookie and native CSRF protection; it does not persist authentication tokens in browser storage. Serve remote users through one HTTPS origin with an exact allowed host and browser origin. Host-header validation remains enabled.

## Maintain Studio development tools

Install Studio dependencies from the current lockfile and include development dependencies in security checks:

```bash
npm --prefix studio ci
npm --prefix studio audit
```

Full audits cover test tools such as Vitest. Forge 0.2.33 pins Vitest and its locked companion packages to 4.1.12, which addresses [manual mock code injection](https://github.com/vitest-dev/vitest/security/advisories/GHSA-qxg6-89mg-rqv8) and [Windows redirect path traversal](https://github.com/vitest-dev/vitest/security/advisories/GHSA-xp26-3454-mjgp). Review future lockfile updates and rerun the Studio checks before publishing a source change.

Keep the development server on its configured `127.0.0.1` listener. Operator installations serve the built Studio through the authenticated API origin; see [source development](installation.md#source-development) for the build commands.

## Protect credentials

File-backed service credentials and immutable run revisions are plaintext files with mode `0600` inside a mode `0700` configuration directory. Secret GET responses report presence only. Empty secret fields preserve saved values; explicit clearing removes them. This is owner-level filesystem protection, not encryption at rest.

Connection OAuth client secrets and grant tokens are separately encrypted in PostgreSQL, bound to their connection and revision context. Historical credential fingerprints remain available for admission checks after rotation or local disconnect. Capacity is reserved before code/refresh dispatch; exhausted custody history blocks new admission. Preserve the shared encryption key with the installation backup. [Connected accounts](connected-accounts.md) explains browser consent, policy withdrawal, exact effect receipts and local disconnect.

Configured credentials, URI passwords and private keys are redacted from evidence. Source containing a configured credential is rejected before delivery. Worker and verification processes receive filtered environments excluding the database URL and unrelated server secrets. Git credentials are supplied only to authorized Git operations, through ephemeral askpass configuration, never credential-bearing repository URLs or global Git settings.

Provider-native grants remain authoritative. Repository discovery is restricted to those grants, and clone/publication authentication rechecks the stored repository identity. GitHub tokens use a selected repository scope. Origin checks and disabled credential redirects prevent an agent's changed Git remote from receiving an application token.

## Keep core gates enabled

Optional policy recommendations and quality gates can be selected per project. Core path confinement, credential checks, real verification, source integrity and explicit publication/deployment boundaries remain mandatory.

Newly prepared workspaces exclude untracked `.venv`, `node_modules` and `vendor` dependencies from Git snapshots, including read-only dependency symlinks. This does not authorize a dependency path or make it deliverable source: an already tracked private path is still rejected. Existing retained workspace metadata is not rewritten automatically.

Native Agentic file tools reject traversal, symlink and hardlink access, Git metadata and private credential files. Official CLI providers have their own workspace tool permissions. Neither mechanism establishes operating-system isolation for the complete factory.

Knowledge references are untrusted data with retained citations. They cannot authorize a new action or override core guards. Explicit knowledge indexing accepts selected verified tracked text files; it does not upload an operator's home, whole checkout or private configuration directory.

## Inspect uncertain effects

The write-ahead journal records intent before an effect and a receipt after its observed completion. A network failure after a remote write can leave its outcome unknown. Inspect the remote branch, deployment or flycanon source/version before continuing. Generic resume cannot replay an ambiguous remote effect.

Passing checks establish only their tested behavior. Use the receiving project's normal source review and release controls. Report private security issues through the repository's security policy without attaching tokens, private logs or sensitive source.

## Identity and project permissions

Production sign-in uses [Keycloak OIDC](identity.md). Cookie mutations require native CSRF double-submit and the exact application origin. New identities receive no project access from their email. Project roles are distinct from agent roles; repository-level writes require the permission in every project sharing that repository.

[User administration](user-administration.md) records provisioning/invitation intents before contacting the provider. An uncertain response needs reconciliation. Suspending a local factory identity revokes its sessions and blocks later checked effects. Project membership changes take effect at the next authorization boundary. Queued administrator work also checks the latest validated API-client grants, their expiry and the admitted audience. The original administrator flag alone cannot authorize execution. Older tokens cannot restore a role already observed as removed; tokens with equal issuance time can only reduce grants. [Identity](identity.md) explains these token-ordering rules and the requirement for a newly issued token to expand roles.

Provider-side changes are observed through new validated tokens and expiry, not continuous introspection. Suspend an identity locally for immediate interruption. A current project membership can still authorize isolated work after global privileges expire or are removed. Host coding and host verification remain restricted to a current administrator.

An initial hosted installation may expose OIDC sign-in while allowing execution only for trusted administrators. Keep repository-channel automation disabled unless its separately configured trusted-actor contract is intended. Do not grant administrator roles to untrusted tenants: host execution can access the service account's files. Project users need an explicitly configured, actually available isolated runner; an unavailable Docker profile never falls back to host execution. Do not mount the host Docker socket, provider login home, or server credentials into generated-code containers.

Project users cannot select direct host coding CLIs through initial admission, independent review or handoff. Runners, source-control operations and deployment effects require their own authorization checks. Production API/worker separation, cluster isolation and hosted-to-local subscription relay remain explicit acceptance boundaries.

## Personal observation credentials

[Personal project API tokens](personal-api-tokens.md) use native pyfly authentication and transactional digest storage. They carry a distinct service origin with read-only selected-project grants, checked against current membership and revocation. They cannot inherit an owner's factory-administrator role or authorize execution, review, release, credential management, global configuration reads or MCP. Owner-session lifecycle routes disclose each new secret once and never list secret digests.
