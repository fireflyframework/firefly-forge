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
| `read_run_evidence` | Read-only, bounded retained journal, tool and activity evidence for the current run, with exact cut/filter pagination and record identities. It never executes or retries the recorded work. |
| `profile_python` | Optional stdlib CPU-profile observation through the same frozen Docker command runner and retained effects. Requires both profiling and command grants. |
| `trace_python` | Optional best-effort target Python line counts through the same isolated runner. Requires both tracing and command grants. |
| `profile_php` | Optional PHP runtime metrics or a preinstalled Xdebug profile through the same isolated command runner. Requires both profiling and command grants. |

Use the [tool prerequisites guide](tooling-prerequisites.md) to check every phase and provider before enabling a capability. Commands, browsers, internet search and external MCP are optional configured capabilities; the initial implementation role grants the six confined source tools.

Native tool arguments reject known configured credentials before file writes or external requests. Text returned by every native tool is redacted against the shared server credential registry before the model receives it. This checks known values and their configured representations; it does not discover every possible secret in a repository.

Paths cannot traverse outside the component. Source read and write operations reject symlinks, hardlinks, Git metadata and credential filenames. Git diff reads only the component's admitted source paths and does not update the index. A rejected line edit requires reading the new file before retrying.

The table is a conditional native registry, not a promise that every provider receives all sixteen tools. Ordinary Codex and Claude Code execution uses the CLI's own permissions; role tool lists alone do not enforce those providers' tool access. Firefly Agentic and bridge execution use the native Python admission boundary.

The separately qualified source-only Codex route has a different, measured contract: governed source list/read/search and source edits within its exact admitted workspace, plus only the utilities explicitly recorded in its supported profile. It does not inherit `execute_command`, browser tools, internet search or `read_run_evidence` from the ordinary native registry. The current binary, model metadata, package policy and runtime must match that qualified profile before dispatch. A configured provider, an available capability, a qualified source-only profile and a completed invocation are separate states; check the [tool prerequisites](tooling-prerequisites.md) and [source-only recovery requirements](recovery.md) for the applicable route. Owner guidance does not modify a call already in flight or become delivery authority for a sealed source-only invocation.

## Observe a Python workload

Grant both `profile_python` and `execute_command` to the implementation role before creating the run. The repository runner must use an immutable Docker image, archive workspace transfer and network `none`, without a pool binding. Its image must already contain `python3` and the workload's dependencies. Host, bind-mount, network-enabled and pool profiles do not receive this optional tool. Readiness checks runner availability; the invocation still determines whether the image has a working Python interpreter. The default six source grants are unchanged.

The native tool accepts a versioned recipe request:

```json
{"request":{"recipe":"forge-python-cprofile-v1","script":"scripts/measure.py","args":["--size","1000"],"sort":"cumulative","rows":20,"timeout":30}}
```

The script must be a non-private, relative `.py` path within the component. forge reads its UTF-8 bytes and records their SHA256. A fixed isolated Python launcher imports the standard library before project modules, rechecks that exact source hash, compiles those bytes once and executes the workload with literal arguments. It prints a bounded text `cProfile` report without creating or loading a `.prof` file. Arguments are limited to sixteen bounded strings, rows to 1–100 and time to 1–120 seconds, further limited by the run's frozen command policy. CPU, memory, process, output and total-command budgets remain those of the same runner. Profiling consumes a command slot.

The observation returns the actual workload exit, up to 16,000 output characters, a truncation flag, source hash, image, exact command and the exact retained command/start/cleanup receipt identities. A missing optional receipt binding is explicit as `receipt: null`; it does not trigger another execution. Full runner output and effect evidence retain the existing private journal contract. A timeout, uncertain effect or failed workspace publication keeps that contract's failure or reconciliation behavior and cannot be blindly replayed. Current execution authority, cancellation and credential checks still apply.

Timing values and workload text are untrusted diagnostic observations, not deterministic thresholds or QA approval. `report_marker_observed` only reports whether the expected text appeared; a workload can spoof it or exit before a report is printed. A nonzero workload exit remains nonzero, and profiling alone does not satisfy the worker's source-change or verification requirements. There is no host profiler parser, debugger port, `ptrace`, Docker socket grant or host fallback. Interactive debugging and application-network orchestration require separate acceptance.

### Inspect which Python lines ran

Grant both `trace_python` and `execute_command` before creating the run. This optional tool uses the same frozen Docker, archive-transfer and network-`none` boundary as profiling, without a pool binding. It consumes one command slot and requires the image's existing Python interpreter and project dependencies.

```json
{"request":{"recipe":"forge-python-line-trace-v1","script":"scripts/inspect.py","args":["--fixture","owned"],"max_rows":50,"timeout":30}}
```

The launcher verifies the confined target's source hash, compiles it once and counts line events from that compiled target and its nested code. Imported modules and separately compiled code are excluded. It retains at most `max_rows` distinct lines (1–100), sorts those retained lines by number and sets `rows_truncated` if others were observed. Counts saturate at the signed 64-bit maximum. The report contains line numbers and hit counts, with no source text, local variables or report files.

The returned observation has `best_effort: true` and the same bounded output, actual exit and exact execution receipt fields as profiling. Counts are untrusted diagnostics, **not complete coverage, a complete execution snapshot or QA approval**: the workload can disable tracing, alter the report or terminate before it is emitted. A missing marker does not trigger another execution. Nonzero exits, timeout, output limits, cancellation and unknown effects preserve the command runner's existing outcome and reconciliation rules. There is no debugger port, attachment or host fallback.

## Observe a PHP or larafly workload

Grant both `profile_php` and `execute_command` before admitting the run. Choose a digest-pinned Docker image, archive workspace transfer and network `none`, without a pool binding. Readiness confirms that boundary is configured; it does not prove that PHP, Xdebug or the application's dependencies are available. The default six source grants remain unchanged.

For [larafly](https://fireflyframework.org/frameworks/php/) and other PHP applications, prepare PHP and the normal application bootstrap first. An existing `vendor/autoload.php` and its Composer dependencies must already be available in the image or admitted regular workspace files within its archive budget. Profiling never runs Composer, downloads dependencies or enables a network. An `artisan` entrypoint is supported, but a small PHP or framework-module fixture does not establish that a complete Laravel application bootstraps successfully.

Choose one of two recipes explicitly:

```json
{"request":{"recipe":"forge-php-runtime-v1","script":"artisan","args":["list"],"timeout":30}}
```

The runtime recipe uses PHP's `hrtime`, `getrusage` and `memory_get_peak_usage(true)`. Its summary reports wall time in nanoseconds, user and system CPU time in microseconds, and peak allocated memory in bytes. The shutdown collector covers the entrypoint through its first collector callback; the separate process wall time also includes process startup and shutdown. These are best-effort observations, not benchmark thresholds, coverage or evidence of a speedup.

```json
{"request":{"recipe":"forge-php-xdebug-v1","script":"scripts/measure.php","args":[],"rows":20,"timeout":30}}
```

The Xdebug recipe requires an image that already has Xdebug and PHP's POSIX file-limit functions. Missing prerequisites refuse execution **before the application starts**, without installing an extension or falling back to another recipe. Xdebug runs only in `profile` mode, with profiling enabled at request start and compression disabled. It writes a private Cachegrind file that is parsed after the child process exits; debugger connections and inherited Xdebug triggers are disabled. See the [official Xdebug profiler settings](https://xdebug.org/docs/profiler).

The child applies an 8 MiB hard per-file limit before application code runs. This also limits files the application writes during that diagnostic invocation. The bounded parser accepts `Time_(10ns)` and `Memory_(bytes)` costs, resolves at most 4,096 file and function references, and returns up to `rows` functions ordered by self time. Call-edge costs are not added to self time. An unsupported or incomplete profile is reported as incomplete, never as a successful call graph. Actual Xdebug call-graph operation requires separate qualification with that extension present.

Both recipes accept a confined non-private `.php` file or an entrypoint named `artisan`, with at most 1,000,000 source bytes. The parent and the child's private prepend recheck its SHA256 and refuse symbolic or hard links. The child invokes the real file with explicit PHP file and argument boundaries, preserving normal `__FILE__`, `__DIR__`, includes, bootstrap, `argv`, `argc` and `$_SERVER` behavior. The application is not evaluated or copied to a replacement entrypoint. The child uses the image's standard CLI ini configuration with recipe overrides; inherited `PHPRC` and `PHP_INI_SCAN_DIR` paths are cleared. Required extensions must be available in that standard configuration. Inherited prepend/append hooks are replaced by the diagnostic collector, and runtime-only profiling explicitly disables Xdebug.

Arguments are limited to sixteen bounded strings, rows to 1–100 and time to 1–120 seconds, subject to the run's stricter frozen limits. Each call consumes a command slot. The supervising process bounds forwarded application output to 1,000,000 bytes; the runner can enforce a smaller limit. The returned observation retains at most 16,000 characters plus truncation and marker flags, source hash, frozen image, exact command and optional exact start/cleanup receipt identities. Inspect the native tool result and Plan → Execution journal in Studio for the metric summary, workload outcome and execution receipts; the original command and output remain available in the collapsed raw details. `receipt: null` never authorizes another execution.

After the original command and cleanup complete, forge can retain a `diagnostic_observation` bound to their exact immutable intents and receipts. Read it through the existing run journal API or reload the run's Plan view in Studio. The tool returns a separate `retention` status: `recorded` includes the journal sequence and digest, `unknown` means the save could not be confirmed, and `not_retained` means the command binding was unavailable. A retention failure preserves the known command exit and never authorizes replay. If current credential admission refuses the result, `diagnostic_unavailable: true` preserves that exit without returning the diagnostic text or command details. A complete report does not mean the workload succeeded.

A missing interpreter or extension is a prerequisite refusal. A workload that exits with 7 or a PHP fatal error keeps that outcome. Report-writing or parser failures cannot replace a known workload exit; they produce incomplete diagnostics. A deadline, signal or cap that prevents observing the application's exit leaves `workload_exit_code: null` and cannot establish success. Temporary collectors and reports are removed only after the direct child has stopped; the normal isolated-runner cleanup and reconciliation rules still apply. Current actor, role, cancellation and credential checks remain in force. A report is untrusted text, including any apparent marker or metrics, and never substitutes for source changes, executable verification or QA approval.

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
