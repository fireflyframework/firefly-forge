<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Protect trusted development access

firefly forge executes code for authorized operators. A run uses an isolated Git clone. Trusted host execution uses the service account's identity and filesystem access; it is restricted to factory administrators. Configured Docker runners enforce their recorded container controls for supported commands. Use a dedicated service identity with limited access to repositories and external services.

## Control the API

Production uses Keycloak OIDC with a verified issuer and API audience. A `FORGE_API_TOKEN` is available only in explicitly selected loopback bootstrap mode. Native PyFly filters protect `/api/v1`, including health and OpenAPI. The sign-in endpoints and state-bound callbacks have their explicit authentication contracts. Management, admin and actuator surfaces are disabled, with no separate management listener.

Source-control OAuth callbacks are the narrow exception: their exact GET paths accept a single-use server-side state and an HttpOnly browser nonce cookie. Authorization starts through the authenticated operator API. A callback cannot carry or recover the operator token.

Studio uses an opaque HttpOnly session cookie and native CSRF protection; it does not persist authentication tokens in browser storage. Serve remote users through one HTTPS origin with an exact allowed host and browser origin. Host-header validation remains enabled.

## Protect credentials

Saved credentials and immutable run revisions are plaintext files with mode `0600` inside a mode `0700` configuration directory. Secret GET responses report presence only. Empty secret fields preserve saved values; explicit clearing removes them. This is owner-level filesystem protection, not encryption at rest.

Configured credentials, URI passwords and private keys are redacted from evidence. Source containing a configured credential is rejected before delivery. Worker and verification processes receive filtered environments excluding the database URL and unrelated server secrets. Git credentials are supplied only to authorized Git operations, through ephemeral askpass configuration, never credential-bearing repository URLs or global Git settings.

Provider-native grants remain authoritative. Repository discovery is restricted to those grants, and clone/publication authentication rechecks the stored repository identity. GitHub tokens use a selected repository scope. Origin checks and disabled credential redirects prevent an agent's changed Git remote from receiving an application token.

## Keep core gates enabled

Optional policy recommendations and quality gates can be selected per project. Core path confinement, credential checks, real verification, source integrity and explicit publication/deployment boundaries remain mandatory.

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
