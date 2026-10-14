---
description: Inspect participants, skills, knowledge access and handoffs throughout factory admission, implementation, verification and delivery.
---

# Agent lifecycle

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

Use **Agents & skills** to follow work from a product decision through source delivery and application operation. The lifecycle catalog distinguishes AI dialogue, a run-selected coding provider, and deterministic services. A participant describes an implemented service or instruction binding; it is not evidence that a model session or external action has occurred.

<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/agent-lifecycle-narrow.svg">
  <img src="../diagrams/agent-lifecycle.svg" alt="The twelve lifecycle phases grouped by dialogue, delivery and application operation, with the seven editable instruction roles shown separately">
</picture>

[Open the lifecycle diagram](diagrams/agent-lifecycle.svg) · [Portrait layout](diagrams/agent-lifecycle-narrow.svg)

## Inspect a project's phase configuration

Start with a registered repository you can read. Factory-wide instruction edits require an administrator; project overrides require membership-management permission for that repository.

1. Open **Agents & skills**, select the repository and choose the phase you want to inspect.
2. Read each participant's execution binding. A dialogue participant proposes decisions; the coding provider edits source; a deterministic service performs its configured checks or actions. Optional assurance uses its own selected reviewer.
3. Inspect the applied skills, tool availability and knowledge scope. An injected instruction is prompt context. An executable quality gate runs its declared commands. A configured knowledge connection needs retrieval evidence, and an unverified runner needs a successful runtime check.
4. Use **Edit** on an instruction role to change the permitted instructions, skills or native-tool selections. Save before leaving the project. The next admitted run receives those settings; earlier run and conversation snapshots keep their original revision.
5. Open the actual run or conversation to inspect its frozen inputs and receipts. Use [run evidence](operations.md#inspect-a-run-from-request-to-source) for commands, source and delivery, and [conversation files](assistant-files.md) for the selected model inputs.

The explorer has twelve phase entries and seven editable instruction roles. A role can participate in several phases. Selecting a phase does not start those participants or create twelve model sessions.

API clients can read the same projection at `GET /api/v1/repositories/{id}/agent-roles/lifecycle`. It returns the phase participants, effective role selections, tool catalog, knowledge summary and configuration digests. The factory projection is `GET /api/v1/agent-roles/lifecycle`.

If a requested tool is unavailable, configure its executor and role grant before creating a new run. If knowledge is configured but unverified, perform a [scoped search](knowledge.md#search-and-inspect-citations) and inspect its citations. A denied project view requires current repository access; switching to factory defaults cannot grant it.

## Product decisions

Product dialogue uses the existing `ProductAssistant` task selection. Each admitted turn freezes the selected instructions, their provenance, catalog revision and content digest in its durable context before calling the configured provider. The prompt applies those instructions within a dialogue that has no execution tools. Old turns retain their original snapshot; reusing a request ID returns its existing receipt without another provider call.

| Phase | Participant | Applied instruction skills | Handoff |
| --- | --- | --- | --- |
| Product definition | Product and functions dialogue | Thinking and evidence-first | Customer outcomes, journeys, functional behavior and proposed decisions for owner review |
| Design | Design dialogue | Thinking, frontend design and accessibility | Proposed design guidance; saved reference rendering and owner review remain separate |
| Architecture | Architecture dialogue | Thinking and evidence-first | Proposed component and interface decisions; profile-based architecture previews remain deterministic |

The selected task profile supplies the provider when configured. Otherwise the invocation selects a supported provider explicitly. A profile is configuration, not proof of model access or a successful response. Dialogue proposals cannot assign their own approval, edit source, launch a run, or grant tools.

## Delivery and operation

The canonical phase definitions live in `firefly_forge.agent_lifecycle`. The API projection combines them with the current role registry, repository overrides, policy selection, task bindings and tool catalog.

| Phase | Actual participants | Skills and knowledge | Retained handoff |
| --- | --- | --- | --- |
| Prepare | Product manager, technical lead, architect and security gate bindings | Role skills describe instruction metadata; gates use saved contracts and evidence | Frozen product, architecture, design, source and execution snapshots |
| Implement | One coding provider; optional documentation guidance uses the same worker | Enabled role instructions and selected project policy instructions are injected; project and configured framework knowledge can be retrieved | Source, tests, worker feedback and bounded clarification requests |
| Verify | Technical lead, QA and security gate bindings | Repository checks and selected executable policy gates; frozen source and contract evidence | Actual command outcomes, failures and repair evidence |
| Checkpoint | Technical lead and security gate bindings | Deterministic evidence and source checks | Verified source tree, diff, export and journal checkpoint |
| Assurance | Optional explicitly selected review provider | Review of the immutable verified tree; no separate knowledge retrieval is implied | Findings bound to the reviewed source tree |
| Publication | Security gate binding and source-control publisher | Deterministic source, scope and verification guards | Explicit publication intent and provider review-request receipt |
| Deploy | Deployment manager | Configured target and authorized delivery request; no agent knowledge grant | Deployment receipts, uncertain outcomes and reconciliation status |
| Observe | Application observer | Configured health and metric probes; no model call | Observations, threshold reasons and incident records |
| Maintain | The same coding-provider path with `operation=maintain` | Frozen coding guidance and scoped project/framework retrieval | A maintenance run through implementation, verification and delivery gates |

A `test` operation executes checks on existing source without invoking a coding model. The explorer describes available participants; inspect a run receipt to see which stages actually executed.

An incident can provide context for maintenance; it does not approve deployment or publication. Unknown side-effect outcomes require their existing reconciliation path. The catalog does not execute those actions when an operator selects a phase.

## Skills, tools and knowledge availability

Instruction adaptations retain their source revision, license and adaptation hash. The explorer identifies injected instructions separately from deterministic-role metadata and executable quality gates. Instructions cannot turn an unavailable tool into an available executor.

Scoped runner defaults may expose a configured command, browser or search grant as `worker_unverified`. The execution worker must validate its runtime before using it. Browser and search tools require their configured capability and frozen grants. Official CLI workers follow their native permission controls; Python tool selections do not restrict the CLI's native tools.

Coding and product dialogue retrieval use the selected repository and configured framework workspace. Fresh managed installations include required Flycanon and automatic verified memory; existing connection choices remain preserved. The lifecycle view reports knowledge as configured but unverified, or not configured. It returns no keys, secret references or raw provider settings. Invocation citations and retained receipts establish what was retrieved; configuration alone does not prove access. Deterministic gates use frozen contracts, source and receipts rather than pretending to retrieve framework knowledge.

## Editing and historical evidence

The seven editable instruction roles retain their existing IDs, execution bindings and snapshot digests. Physical participants extend the read projection without expanding the frozen `RoleDocument` contract. Factory role edits and repository skill/tool overrides affect later admitted runs. Assistant task instruction selection is fixed by the implemented task; changing validated catalog instructions affects later turns, never retained turns or a model call already admitted.

Use the lifecycle explorer to inspect current configuration, the run journal for frozen execution evidence, and conversation receipts for assistant history. None of these views substitutes for a live provider response, verified runner receipt, or recorded application observation.

## Knowledge questions and learning feedback

The [learning contracts](learning.md#structure-agent-and-tool-communication) associate each question with its run, task, effective role, actual source, tool result and later verification. A worker can propose a lesson or report a missing reference; trusted eligibility and state changes belong to native factory services. Scoped records, policy and projection actions are available through the authenticated API, SDK, CLI and factory MCP. The lifecycle projection reports execution bindings and knowledge availability; it does not provide a complete Studio learning workflow or establish that a projected lesson was retrieved.
