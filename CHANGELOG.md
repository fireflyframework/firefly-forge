# Changelog

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

## 0.2.20 (unreleased)

- Preserve product-definition edits and the current wizard step when navigating between products or other Studio pages. Keep drafts scoped to the current user and factory connection, with explicit discard and an unload warning.
- Retain the original draft when Studio observes a newer saved revision, and hold saving until the operator discards it. This client check does not provide an atomic server revision lock.
- Accept worker feedback and response identifiers up to the existing 80-character limit without exceeding the journal's 64-character storage column. Preserve short legacy keys, original message identifiers and duplicate-content checks.

## 0.2.19 — 2026-10-16

- Let separately authorized source-control users revise an existing product contract through signed ingress; preserve the reviewed base, original execution identity and exact change receipt.
- Add GitHub App issue polling for status and answers to existing paused runs, with a durable baseline, separate account consent, current permission checks and historical answer attribution.
- Fence polling policy changes and collaboration revocation against in-flight WAL commits, and recheck credentials before sending feedback. An uncertain reply stays held without an automatic repost.
- Query and export a bounded, consistent cut of retained run evidence; explicitly granted implementation agents can read a small page from their own current run.
- Add opt-in Bridge original-request correlation, committed intent and read-only pending/terminal observations. Evidence recovery keeps the graph-continuation hold in place.
- Support verified local HTTPS knowledge bootstrap while preserving required retrieval, existing credentials and queued configuration snapshots.
- Keep narrow product-consent actions stacked and aligned, and document polling setup, permissions and exact-request recovery step by step.

## 0.2.18 — 2026-10-15

- Add separate administrator consent for signed same-run continuation after an answered agent question, bound to current mapping, component scope, original execution identity, plan and WAL.
- Guide consent and revocation through a three-step Studio review with retained-request recovery, exact receipt checks and coordination between browser tabs.
- Keep the consent review readable on short screens and at increased zoom, with scrolling content, responsive actions and visible keyboard focus.
- Expose consent save, exact read and current read through the SDK, CLI and three native-template-backed MCP tools; keep existing V1 delegation contracts unchanged.
- Suppress automatic Bridge inference retries and retain structured quota/context pauses for deliberate recovery.
- Retain ambiguous Bridge transport, completion and cancellation outcomes for reconciliation, preserving the original intent and refusing generic resume, handoff or replacement retry.
- Preserve an absent retry counter during explicit manual continuation, keeping the reviewed run unchanged while automatic retries retain their existing counter.
- Keep the production scene, project and run selectors within the responsive factory overview, with stage details and the full guide available below.

## 0.2.17 — 2026-10-15

- Add a guided agent-question answer flow with Markdown editing, review and recovery from the original response identity.
- Require current authenticated reply authority and retain server-derived authorship separately from caller labels.
- Add reporting-plan waves, milestones and progress from selected immutable campaign evidence.
- Preserve complete confirmed-marker bindings before accepting later operator decisions.
- Add administrator-reviewed SCM account delegation to an existing Forge user, component scope and allowed actions, with retained-request and exact-receipt recovery.
- Verify historical delegation before attributing signed source-control answers; preserve current action permission checks and explicit resume.
- Maintain built-in model instructions and MCP descriptions as versioned native Firefly Agentic YAML templates, preserving rendered text and message priority.

## 0.1.0 — 2026-10-11

- Introduce the Firefly Forge software factory, pyfly control plane and web Studio.
- Prioritize pyfly and larafly products with configurable frontend profiles.
- Connect official local Codex and Claude workers, native Firefly Agentic and internal bridge endpoints.
- Persist isolated runs, activity, verification evidence, diffs and changed-source artifacts.
- Add GitHub, GitLab, Bitbucket Cloud, Azure DevOps, Gitea and generic Git integration.
- Supply the operator CLI, Python SDK, OpenAPI and MCP tools, installation guides and MkDocs documentation.
