<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Protect trusted development access

firefly forge executes code for authenticated operators. A run uses an isolated Git clone, and generated code and verification commands execute with the development host's identity. Use a dedicated host or identity with limited access to repositories and external services.

## Control the API

Set a unique `FORGE_API_TOKEN` of at least 24 characters. Native PyFly filters protect `/api/v1`, including health and OpenAPI. Management, admin and actuator surfaces are disabled, with no separate management listener.

Source-control OAuth callbacks are the narrow exception: their exact GET paths accept a single-use server-side state and an HttpOnly browser nonce cookie. Authorization starts through the authenticated operator API. A callback cannot carry or recover the operator token.

Studio holds its token in page memory, never in a URL or browser local storage. Prefer loopback for local use and one HTTPS origin behind restricted access for remote use. Host-header validation remains enabled.

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
