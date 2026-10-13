---
description: Assign configured models to product dialogue, implementation and review, with retained revisions and capability checks.
---

# Choose models for specific tasks

Use named model profiles when planning, coding and reviewing should use different models or credentials. A profile belongs to one real provider: direct Firefly Agentic API, an optional compatible bridge, Codex CLI or Claude Code CLI. Direct API profiles do not require a bridge.

The provider's existing connection configuration and a named profile are separate. New profiles copy nonsecret connection defaults. They inherit a provider credential only when you explicitly set `inherit_credentials: true`; otherwise supply the profile's own credential. Updating one profile does not edit another profile or the server's process environment.

## Create a profile

Send a profile to `PUT /api/v1/model-profiles/{id}`. The ID is a stable lowercase slug. The body contains a name, provider and provider-specific fields:

```json
{
  "name": "Product planning",
  "provider": "agentic",
  "description": "Customer journeys and expected behavior proposals",
  "values": {
    "model": "openai-responses:YOUR_CONFIGURED_MODEL"
  },
  "inherit_credentials": true
}
```

Use your provider's actual model identifier. This example deliberately reuses the selected provider credential. To keep credentials independent, leave inheritance disabled and supply `api_key` for `agentic` or `token` for `bridge`. The server stores those values in owner-only files; GET responses contain configured booleans rather than secrets. Empty secret values retain an existing profile credential. Use `clear_secrets` to remove it explicitly.

| Provider | Accepted configuration fields |
| --- | --- |
| `agentic` | `model`, `endpoint`, `api_key`, `timeout`, `max_tokens` |
| `bridge` | `model`, `endpoint`, `token`, `timeout` |
| `codex`, `claude` | `model`, `executable`, `timeout` |

For an OpenAI-compatible gateway, configure the `agentic` profile's HTTPS endpoint and its own API key. HTTPS is required except for loopback development. Embedded URL credentials, query parameters and fragments are rejected. Native hosted OpenAI, Anthropic and Google model families use their respective SDK providers when no gateway endpoint is configured.

## Test the connection

`POST /api/v1/model-profiles/{id}/test` tests the selected profile. CLI tests inspect its official login; compatible endpoints test authenticated model listing. A configured key alone does not prove model access or generation.

The response separates `readiness`, `image_input` and `generation_verified`. An authenticated model list does not prove source editing, tool execution or screenshot reasoning. The catalog initially reports image input as unverified; a selected multimodal invocation must obtain an actual matching capability proof.

## Bind a task

`PUT /api/v1/task-routing` saves explicit task defaults:

```json
{
  "bindings": {
    "product": "product-planning",
    "functions": "product-planning",
    "implementation": "source-builder",
    "review": "independent-reviewer"
  }
}
```

Profiles named in this example must already exist. Unsupported task/provider combinations fail before the binding is saved.

| Task | Actual contract | Providers |
| --- | --- | --- |
| `product` | Customer dialogue proposal | `agentic`, `bridge` |
| `functions` | Journey, feature and expected-behavior proposal | `agentic`, `bridge` |
| `design` | Design guidance proposal | `agentic`, `bridge` |
| `architecture` | Architecture dialogue proposal | `agentic`, `bridge` |
| `implementation` | Source-editing worker | All four providers |
| `review` | Independent review of an isolated verified tree | All four providers |
| `multimodal_qa` | Actual screenshot reasoning | `agentic`, `bridge`, with a matching verified image-input capability |

Dialogue tasks are model proposals for owner review. They do not change the existing architecture recommendation or SVG reference renderer into model calls. Deterministic role gates remain deterministic. CLI implementations do not currently provide the native structured dialogue contract merely because they can edit source.

Task routing produces a frozen provider selection for each implemented invocation. An unbound task returns no override and preserves the existing explicit provider selection. A configured binding that is missing, unsupported or unverified fails; it cannot quietly switch to another provider or model.

## Frozen execution and handoff

A captured invocation records its profile ID and revision, provider, task, configuration revision and redacted model settings. Its private credential revision and frozen manifest are retained separately with 0600 permissions. Loading that snapshot checks the manifest, settings digest and public configuration. Changing the profile later cannot alter a queued invocation's model or credentials.

For multimodal work, the selected model must match the verified image-input configuration revision. Unknown capabilities remain unverified. Direct API profiles currently require an actual image-input proof before this routing contract admits them; naming a vision model is insufficient. A compatible bridge may supply a verified proof, but remains optional for other direct API tasks.

Rate limits and missing credits do not authorize automatic provider substitution. Use an explicit recorded handoff when a running job should change providers. The new selection does not reset repair, request or browser budgets.

Continue with [AI configuration](configuration.md), [roles and tools](agent-tools.md), [browser QA](browser-qa.md) and [durable recovery](recovery.md).
