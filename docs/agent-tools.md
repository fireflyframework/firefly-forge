---
description: Use native file, Git, command, search and browser tools within configured workspace, execution and credential boundaries.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Use native workspace and browser tools

Firefly Agentic registers the tools allowed by the run's frozen role configuration and available runner capabilities. `agentic` and `bridge` use this native registry. A role requesting an unconfigured browser or search capability does not receive a working substitute.

| Tool | Scope and evidence |
| --- | --- |
| `list_files` | Up to 2,000 component source paths; skips dependency trees, credentials and symlinks. |
| `read_file` | Bounded UTF-8 source with its SHA256 hash. |
| `write_file` | Complete bounded source content in a relative component path. |
| `search_text` | Literal text search with line numbers, file hashes and an explicit truncation flag. |
| `edit_lines` | Inclusive line replacement requiring the current file hash, rechecked on the open write descriptor. |
| `git_diff` | A bounded component diff with external drivers, text conversion, filesystem monitors and hooks disabled. |
| `execute_command` | Bounded argv execution through the frozen host or Docker policy. |
| `browser_check` | A fresh Playwright context against a declared container-local project origin. |
| `browser_journey` | Declared user flows with typed actions, state DOM evidence and optional verified image input. |
| `internet_search` | A configured server-mediated search API with bounded citations. |
| `external_mcp` | An exact reviewed read tool within the project/repository/role grant. |
| `knowledge_lookup` | Repository-scoped references and eligible local outcomes under the run's frozen learning policy. |
| `profile_python` | Optional stdlib CPU-profile observation through the same frozen Docker command runner and retained effects. Requires both profiling and command grants. |
| `trace_python` | Optional best-effort target Python line counts through the same isolated runner. Requires both tracing and command grants. |

Use the [tool prerequisites guide](tooling-prerequisites.md) to check every phase and provider before enabling a capability. Commands, browsers, internet search and external MCP are optional configured capabilities; the initial implementation role grants the six confined source tools.

Native tool arguments reject known configured credentials before file writes or external requests. Text returned by every native tool is redacted against the shared server credential registry before the model receives it. This checks known values and their configured representations; it does not discover every possible secret in a repository.

Paths cannot traverse outside the component. Source read and write operations reject symlinks, hardlinks, Git metadata and credential filenames. Git diff reads only the component's admitted source paths and does not update the index. A rejected line edit requires reading the new file before retrying.

Official Codex and Claude Code tools use their own CLI permissions. Role tool lists describe advisory intent for those providers; native Python enforcement applies to Firefly Agentic and bridge execution.

## Observe a Python workload

Grant both `profile_python` and `execute_command` to the implementation role before creating the run. The repository runner must use an immutable Docker image, archive workspace transfer and network `none`, without a pool binding. Its image must already contain `python3` and the workload's dependencies. Host, bind-mount, network-enabled and pool profiles do not receive this optional tool. Readiness checks runner availability; the invocation still determines whether the image has a working Python interpreter. The default six source grants are unchanged.

The native tool accepts a versioned recipe request:

```json
{"request":{"recipe":"forge-python-cprofile-v1","script":"scripts/measure.py","args":["--size","1000"],"sort":"cumulative","rows":20,"timeout":30}}
```

The script must be a non-private, relative `.py` path within the component. forge reads its UTF-8 bytes and records their SHA256. A fixed isolated Python launcher imports the standard library before project modules, rechecks that exact source hash, compiles those bytes once and executes the workload with literal arguments. It prints a bounded text `cProfile` report without creating or loading a `.prof` file. Arguments are limited to sixteen bounded strings, rows to 1–100 and time to 1–120 seconds, further limited by the run's frozen command policy. CPU, memory, process, output and total-command budgets remain those of the same runner. Profiling consumes a command slot.

The observation returns the actual workload exit, up to 16,000 output characters, a truncation flag, source hash, image, exact command and the exact retained command/start/cleanup receipt identities. A missing optional receipt binding is explicit as `receipt: null`; it does not trigger another execution. Full runner output and effect evidence retain the existing private journal contract. A timeout, uncertain effect or failed workspace publication keeps that contract's failure or reconciliation behavior and cannot be blindly replayed. Current execution authority, cancellation and credential checks still apply.

Timing values and workload text are untrusted diagnostic observations, not deterministic thresholds or QA approval. `report_marker_observed` only reports whether the expected text appeared; a workload can spoof it or exit before a report is printed. A nonzero workload exit remains nonzero, and profiling alone does not satisfy the worker's source-change or verification requirements. There is no host profiler parser, debugger port, `ptrace`, Docker socket grant or host fallback. Interactive debugging, PHP profiling and application-network orchestration require separate acceptance.

### Inspect which Python lines ran

Grant both `trace_python` and `execute_command` before creating the run. This optional tool uses the same frozen Docker, archive-transfer and network-`none` boundary as profiling, without a pool binding. It consumes one command slot and requires the image's existing Python interpreter and project dependencies.

```json
{"request":{"recipe":"forge-python-line-trace-v1","script":"scripts/inspect.py","args":["--fixture","owned"],"max_rows":50,"timeout":30}}
```

The launcher verifies the confined target's source hash, compiles it once and counts line events from that compiled target and its nested code. Imported modules and separately compiled code are excluded. It retains at most `max_rows` distinct lines (1–100), sorts those retained lines by number and sets `rows_truncated` if others were observed. Counts saturate at the signed 64-bit maximum. The report contains line numbers and hit counts, with no source text, local variables or report files.

The returned observation has `best_effort: true` and the same bounded output, actual exit and exact execution receipt fields as profiling. Counts are untrusted diagnostics, **not complete coverage, a complete execution snapshot or QA approval**: the workload can disable tracing, alter the report or terminate before it is emitted. A missing marker does not trigger another execution. Nonzero exits, timeout, output limits, cancellation and unknown effects preserve the command runner's existing outcome and reconciliation rules. There is no debugger port, attachment or host fallback.

## Configure real browser checks

Prepare a browser image containing the matching Playwright package and browser binaries. `infrastructure/runners/Dockerfile.browser` supplies a pinned Playwright 1.64.0 example. The [official Playwright image](https://playwright.dev/docs/docker) includes browsers and system dependencies; the package must be installed separately.

Add a `browser` object to the repository's `sandbox` configuration:

```json
{
  "image": "your-browser-image@sha256:REPLACE_WITH_64_HEX_DIGEST",
  "start_argv": ["npm", "run", "preview", "--", "--host", "127.0.0.1", "--port", "18080"],
  "origin": "http://127.0.0.1:18080",
  "startup_timeout": 30,
  "max_width": 2560,
  "max_height": 1440,
  "max_screenshot_bytes": 1000000,
  "max_calls": 8
}
```

The startup command runs inside the same container as the browser. Prepare the frontend build and dependencies first. The origin must be a literal container-loopback address on port 1024 or higher. The tool accepts a project route, viewport and light/dark theme. It cannot browse arbitrary websites or use a host browser profile.

Browser execution always uses Docker network `none`, a fresh browser context, blocked service workers and an origin restriction on requests. WebSockets are blocked. External databases, APIs, fonts and assets will not load in this profile. Database sidecars and full application network orchestration are outside its current scope.

The runner reports the actual HTTP status, horizontal overflow, clipped text, covered controls, the first keyboard focus target and blocked origins. These checks are bounded layout diagnostics; they do not establish accessibility conformance or visual approval. Playwright screenshots retain the requested viewport and theme. Chromium's internal sandbox is disabled in this profile; Docker supplies the process boundary with capability and resource restrictions.

Screenshots are private files. `browser_check` returns an artifact ID and SHA256 hash. The [journey QA tool](browser-qa.md) can also attach actual PNG bytes to a model with verified image input; host paths are never included. An authenticated operator can retrieve a PNG through `GET /api/v1/runner-evidence/{id}/screenshot`. Inspect the image before accepting a product's design.

## Enable internet search deliberately

Select a search provider and save its credential through `PUT /api/v1/search/configuration`. For a new configuration, the [Tavily setup guide](search-providers.md) explains domain restrictions and provider usage receipts:

```json
{"provider": "tavily", "api_key": "YOUR_AUTHORIZED_SEARCH_KEY", "clear_secret": false}
```

The key is write-only in the API and stored in an owner-private `0600` file. `GET /api/v1/search/configuration` reports configuration presence and actual connection-test evidence. `POST /api/v1/search/test` makes a real provider request. It consumes the provider's normal API allowance.

Set `sandbox.internet_search` to `true` and grant the role `internet_search` before queueing a run. The run freezes the provider, private credential revision and domain restrictions; later rotation affects new runs. A missing search configuration prevents enabling that capability. Existing Brave configurations and queued snapshots retain their provider. Changing providers requires a new key and never changes the provider of an admitted run. Requests use the selected provider's fixed HTTPS endpoint. See [search providers](search-providers.md) for the request limits, authentication, and quota failures.

Queries have a 600-character and 75-word limit. Results are capped at ten, with bounded titles and excerpts. The server rejects known factory credentials before sending a query and excludes them from admitted results. It does not give the search key to the model or container. Search excerpts remain untrusted evidence and cannot change the task or tool permissions.

When the run captures enabled learning, native search first checks authorized knowledge and eligible local evidence. An explicit refresh request retains its request identity and requires a new lookup; it cannot silently reuse an old provider result. `knowledge_lookup` uses the same scoped decision path without an external search. See [learning controls](learning.md) for policy, eligibility and projection limits.

Each admitted result carries its source URL and stable citation ID. The private cache retains at most 1,000 citations; results beyond that cache capacity still include their source URLs and report `cached: false`. Retrieve a cached citation through `GET /api/v1/search/citations/{id}`. `POST /api/v1/search/query` provides the same bounded search operation to authenticated operators.

No search credential is bundled with firefly forge. Installing the search tool does not prove access to a search account; configuration, connection testing and real task evidence remain separate states.
