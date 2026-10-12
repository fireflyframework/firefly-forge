<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Choose and configure a coding agent

Forge separates the coding provider from the run lifecycle. Each provider
receives the brief, stack instructions and isolated workspace. The factory
records progress and independently executes verification after generation.

| Provider | Authentication | Configuration |
| --- | --- | --- |
| Codex | Official CLI's existing login | `FORGE_CODEX_EXECUTABLE`, optional `FORGE_CODEX_MODEL` |
| Claude Code | Official CLI's existing login | `FORGE_CLAUDE_EXECUTABLE`, optional `FORGE_CLAUDE_MODEL` |
| Firefly Agentic | API provider environment | `FORGE_AGENTIC_MODEL` and the matching provider key |
| Internal agent bridge | Authorized endpoint token | [Bridge connection](bridge.md) |

## Signed-in CLI providers

Install and authenticate the official CLI on the server, using its documented
login flow. The Codex provider launches `codex exec` in the isolated workspace
and consumes structured events. Claude Code runs its noninteractive print flow
with explicit tool permissions. A failed process or a missing final result is
a provider failure.

Forge does not reuse browser cookies or desktop private storage, replace an
official login flow, or guarantee account-specific subscription access. An
existing CLI login and actual model access are separate from API-key providers.

## Native Firefly Agentic

Configure the model and the key required by that provider. For an OpenAI model:

```sh
export FORGE_AGENTIC_MODEL="openai-responses:your-available-model"
export OPENAI_API_KEY="your-provider-key"
```

Use your real available model and a securely supplied key. Provider keys stay
on the server. For an explicitly configured compatible API, set
`FORGE_AGENTIC_BASE_URL` and `FORGE_AGENTIC_API_KEY`; this path does not establish
access to a hosted model by itself.

## Interpret readiness correctly

**Connections** reads `GET /providers`. CLI readiness checks executable discovery
and the official login status. API and bridge readiness checks configuration;
their connectivity and model access are tested by a real run. A green
configuration status must not be reported as a successful model execution.

`FORGE_PROVIDER_TIMEOUT` bounds generation duration. Failures appear in the run
timeline and error field. Inspect those before retrying, and retain the first
attempt as evidence.
