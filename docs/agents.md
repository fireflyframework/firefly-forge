---
description: Choose the coding provider for a factory run and understand native agentic execution, local subscriptions and provider limitations.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Choose a coding provider

Use Codex or Claude Code when the development host already has an official CLI login. Use native Firefly Agentic for API credentials and a service environment. A separately operated agent bridge connects the same native workspace tools to the machine with its coding-agent login. Start with [pyfly](https://fireflyframework.org/frameworks/python/) or [larafly](https://fireflyframework.org/frameworks/php/) and a small brief with observable acceptance criteria.

Each run uses one coding worker. The factory supplies framework instructions and selected policies, then independently runs verification. The worker receives failed checks for bounded repairs. [Workflow roles](workflows.md) describe which steps use a model.

| Provider | Authentication | Model selection | Workspace tools |
| --- | --- | --- | --- |
| Codex | Official CLI login on the server | CLI default or explicit available model | Official Codex tools in `workspace-write` mode |
| Claude Code | Official CLI login on the server | CLI default or explicit available model | Read, Write, Edit, Glob, Grep and specified command permissions |
| Firefly Agentic | Provider API key | Provider-qualified model ID | Confined source tools and explicitly granted command, browser and search tools |
| Agent bridge | Endpoint bearer token | Authorized alias such as `bridge/default` | The same native workspace tools, subject to the execution profile |

## Set up a signed-in CLI

Install the [official Codex CLI](https://developers.openai.com/codex/cli/) or [Claude Code](https://code.claude.com/docs/en/setup) on the factory host. Complete its official login:

```sh
codex login
codex login status
# Or:
claude auth login
claude auth status
```

In Studio, open the provider connection, leave the model blank to use the CLI default, and test the login. Set an executable path only when the CLI is outside the server's `PATH`. The server account must have access to its own CLI login. A container does not inherit a host account's login.

Codex ignores unrelated user configuration while retaining the official login. It runs noninteractive execution with the workspace write sandbox and no approval prompts. Claude Code uses its print mode with explicit tool permissions. Both paths require a successful process and a final implementation report. Neither path guarantees that a particular account can use a model.

## Configure native Firefly Agentic

The packaged factory consumes [fireflyframework-agentic 26.10.1](https://github.com/fireflyframework/fireflyframework-agentic/releases/tag/v26.10.1) through its immutable public wheel URL. The dependency lock records the resolved packages; the factory still owns retry, journal and handoff decisions. Optional provider and retrieval extras need their own credentials, infrastructure and security review.

Choose an available provider-qualified model, for example `openai-responses:MODEL_ID`, `anthropic:MODEL_ID` or `google-gla:MODEL_ID`. Enter its API key in the write-only configuration form. This key is independent of CLI subscription login.

For an OpenAI-compatible service, supply its HTTPS endpoint, key and model. The compatible endpoint uses Chat Completions. HTTP is accepted only for `localhost`, `127.0.0.1` and `::1`. URLs containing credentials, query strings or fragments are rejected.

Environment configuration remains available:

```sh
export FORGE_AGENTIC_MODEL="openai-responses:your-available-model"
export OPENAI_API_KEY="your-provider-key"
# Compatible service, when needed:
export FORGE_AGENTIC_BASE_URL="https://your-service.example/v1"
export FORGE_AGENTIC_API_KEY="your-service-key"
```

Environment keys can also be `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` or `GEMINI_API_KEY` for their matching model family. Saved settings override the corresponding environment values. See [configuration](configuration.md) for secret retention and run snapshots.

Native Agentic edits project files through confined source tools. When the execution profile grants command, browser or search access, the worker can use those bounded tools during generation. The factory runs verification independently afterwards. A summary without source writes is a failed generation. File tools reject path traversal, symlinks, hardlinks and private credential paths; commands follow the selected runner's isolation boundaries. See [agent tools](agent-tools.md) for grants and runtime availability.

## Test access and interpret the result

| State | What was established | Next step |
| --- | --- | --- |
| `missing` | Required configuration or official login is absent | Configure or sign in |
| `configured` | Connection fields are present | Verify access with a test or real run |
| `authenticated` | The official CLI login command succeeded | Run a small generation with the selected model |
| `connected` | Authenticated model listing includes the selected model | Verify a real source-editing run |
| `failed` | The explicit connection check failed | Correct the address, key, login or model and retest |

Compatible API and bridge tests request `/models`. A hosted provider without an explicit compatible endpoint receives a configuration check; model and tool access are established by a real run. A successful listing does not prove generation. The provider catalog records the check's evidence and timestamp.

Generation defaults to a 1,800-second wall timeout. Native Agentic also bounds requests and tool calls; its optional output-token limit is available for direct APIs. Subscription bridge execution omits that cap because the bridge cannot implement it. Verification has its own per-command timeouts.

The bridge submits each model request once. A structured `quota_exhausted` response pauses the run for an operator; `execution_paused` also requires an explicit review of the retained native session. A supplied job reference and numeric retry delay are saved as observations, and neither schedules an automatic resume. A missing reset time remains unknown.

An unclassified bridge server error, transport failure, or unusable completion response (including an empty stream) leaves the run and its active intent in `needs_reconciliation`, with its workspace retained. Forge cannot tell whether the bridge finished that submission. Resume and provider handoff refuse this unresolved outcome, preventing another model submission. Recovery of the same bridge job is not implemented in this cut; inspect its retained records before deciding how to continue. These rules apply to the bridge connection; direct API providers retain their existing failure handling.

A useful first acceptance brief is a small inventory service: list stock, create an item, reserve a positive quantity, and reject overselling. Require meaningful service/API tests and an executable `make check`. For browser products, exercise the actual cookies, forms, validation and conflict feedback. [pyfly](pyfly.md) and [larafly](larafly.md) cover the primary framework paths.
