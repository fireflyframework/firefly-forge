<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Give Forge trusted development access

Forge is a software execution service for trusted operators. An authenticated
operator can register Git sources, ask an agent to change code and run
verification commands on the server. An isolated Git clone protects the source
checkout from direct edits; it does not isolate the process from the host.

## Protect the control plane

Set a strong `FORGE_API_TOKEN`. The API expects `Authorization: Bearer <token>`.
Do not expose an unauthenticated API or share an operator token with untrusted
users. Use loopback binding for local work and HTTPS plus access controls for
remote operation.

The packaged PyFly configuration disables the native management, admin and
actuator surfaces. Its management server port is `-1`, so it does not open a
separate management listener. Forge's own `/api/v1` routes, including health
and OpenAPI, pass through the shared operator-token boundary.

Studio keeps the entered token in page memory. It is cleared on reload, never
put in a URL, and sent to the API address the operator enters. Provider tokens,
Git credentials and CLI logins remain server-side.

## Scope the execution identity

Use a dedicated user or isolated development host with limited permissions.
Give its Git credentials access only to intended repositories. Avoid production
databases, shared services and unrelated workspaces. Explicit command argument
arrays prevent accidental shell interpretation, but the commands and generated
code still execute with the server user's authority.

Repository instructions and product briefs are part of the coding agent's input.
Review unfamiliar sources and treat generated output as untrusted until you
inspect it and run appropriate checks. A coding provider's own workspace
permissions do not create multi-tenant isolation for the complete factory.

## Review publication

`publish: false` is the default. An explicit publishing run can write a remote
branch and create a pull request using server credentials. Verification passing
is a prerequisite, not approval of every possible generated change. Restrict
publishing credentials and use the receiving project's ordinary reviews.

Publication requires the final passing verification and the same source-tree
snapshot that those commands checked. A changed workspace is rejected before a
remote write. This integrity check does not establish correctness beyond the
actual checks performed.

For private security reporting, follow the repository's security policy.
Never include tokens, private logs or sensitive source in a public issue.
