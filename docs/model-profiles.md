---
description: Assign configured models to product dialogue, implementation and review, with retained revisions and capability checks.
---

# Choose models for specific tasks

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

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
| `communication` | Issue and pull-request wording over recorded facts | `agentic`, `bridge` |

Dialogue tasks are model proposals for owner review. They do not change the existing architecture recommendation or SVG reference renderer into model calls. Deterministic role gates remain deterministic. CLI implementations do not currently provide the native structured dialogue contract merely because they can edit source.

Task routing produces a frozen provider selection for each implemented invocation. An unbound task returns no override and preserves the existing explicit provider selection. A configured binding that is missing, unsupported or unverified fails; it cannot quietly switch to another provider or model.

## Write project updates with AI

Select an existing `agentic` or `bridge` profile for `communication`. This enables AI wording for subsequent issue and pull-request updates. The implementation profile is independent: a run using the Codex CLI can use an explicitly selected Bridge subscription for its updates. No paid API profile or coding subscription is chosen automatically.

First configure the profile through the write-only model-profile endpoint described above. A Bridge profile needs its normal endpoint, model and scoped token; borrowing the factory's configured credentials requires explicit `inherit_credentials: true`. A direct API profile needs its own model and credentials. Then read `GET /api/v1/task-routing`, preserve the existing bindings and add the communication selection:

```json
{
  "bindings": {
    "implementation": "source-builder",
    "communication": "project-updates"
  }
}
```

In Studio, open **Connections > Coding AI > Project updates**. A factory administrator can select a compatible existing profile or choose **Recorded updates · no AI wording**, then save. Other task selections stay unchanged. **Refresh selection** reads the current settings without saving them. A missing response never triggers an automatic second save; read back the selection before deciding what to do next.

API clients send the complete binding map to `PUT /api/v1/task-routing`, together with the `expected_digest` returned by the preceding GET. A changed digest refuses the write. Omitting this optional field preserves the existing API contract. Profiles in this example must already exist; creating one still uses the authenticated model-profile endpoint.

The model writes the complete update: a specific title, an explanation, the available results and the next supported decision. forge applies Markdown formatting; it does not append the old status card. The pinned [Humanizer adaptation](agent-roles.md#pinned-community-instructions) is included in the actual agent instructions, and its revision and content digest are retained with the message.

Each eligible update uses a tool-free drafting request and an independent semantic classification against the same facts. If the classifier finds unsupported claims or uncertainty, one additional AI request corrects the message and returns the final response. There is no second semantic veto or correction loop. All requests use the selected profile with SDK retries disabled. The draft allows 1,400 output tokens; the review allows 700. One total deadline, capped at 120 seconds, covers queue time and all requests. It does not restart for the review. The effective timeout is retained in the communication receipt. Use a 60-second profile timeout for local subscription workers; a shorter configured limit remains in force.

The fact packet contains the recorded state, active stage, recovery attempt, check totals and provenance, known source-safety blockers and native delivery state. A pending human decision also supplies a bounded, safely escaped quotation of the question, context and choices. The generated decision must preserve that quotation and every choice; it cannot supply an answer. Questions that cannot fit the public quotation bounds hold AI delivery rather than silently shortening the options. Customer briefs, raw errors, source, logs and credentials are withheld.

Native checks validate the message structure, citation identities, public quotation and current authority. A separate structured AI review classifies the complete candidate as supported, unsupported or uncertain. It checks meaning in context, including negations, numbers, saved versus current results, delivery claims and pending human decisions. It does not classify prose with keyword lists. A supported classification proceeds directly; otherwise the AI makes its single correction and responds, explaining missing information or asking a specific question when needed. The original review is explicitly bound to the original candidate; it is not relabeled as an independent approval of the correction. The receipt retains the candidate digest, review prompt digest, verdict, issues and rationale. This is a fallible model review, not independent product QA or proof of every sentence. With an AI profile selected, forge never substitutes a generic template. A provider outage, exhausted quota, malformed result or failed native authority/framing check holds delivery with its recorded reason, without a remote comment or automatic repeated model calls. A new explicit request can try again. Choosing recorded updates without an AI profile remains a separate configuration mode. There is no automatic provider substitution.

The receipt retains `feedback_intent.communication`: its mode and any hold reason, fact packet and digest, selected profile revision, prompt digest and applied skill provenance. The exact final body and its SHA-256 are saved before the remote comment is attempted. If the run changes while drafting, configured AI delivery is held without a template replacement or another automatic model call. Unknown remote delivery remains held for reconciliation; a second comment is not sent automatically.

## Frozen execution and handoff

A captured invocation records its profile ID and revision, provider, task, configuration revision and redacted model settings. Its private credential revision and frozen manifest are retained separately with 0600 permissions. Loading that snapshot checks the manifest, settings digest and public configuration. Changing the profile later cannot alter a queued invocation's model or credentials.

For multimodal work, the selected model must match the verified image-input configuration revision. Unknown capabilities remain unverified. Direct API profiles currently require an actual image-input proof before this routing contract admits them; naming a vision model is insufficient. A compatible bridge may supply a verified proof, but remains optional for other direct API tasks.

Rate limits and missing credits do not authorize automatic provider substitution. Use an explicit recorded handoff when a running job should change providers. The new selection does not reset repair, request or browser budgets.

Continue with [AI configuration](configuration.md), [roles and tools](agent-tools.md), [browser QA](browser-qa.md) and [durable recovery](recovery.md).
