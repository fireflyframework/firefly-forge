---
description: Understand scoped learning, knowledge-first lookup, failed-attempt evidence and structured agent handoffs, with explicit implementation boundaries.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Learn from evidence and reuse knowledge safely

A useful lesson explains which question it addresses, what was observed and where that observation still applies. Forge already retrieves required flycanon context and retains verified repository memory. The learning subsystem adds deterministic eligibility, exact question-to-evidence associations and controls for repeating an unchanged repair attempt.

!!! info "Implementation boundary"
    The initial learning slice implements scoped PostgreSQL records, evidence eligibility, native knowledge-first decisions and explicit flycanon projection. Local qualification includes an actual indexed source, citation and local embedding, with repeat submission creating no duplicate. Automatic retrieval from projected learning namespaces, classified negative repair circuits, a complete Studio learning workflow and sharing between projects remain open. Generic failed checks record an observed outcome; they do not prove that an approach fails everywhere.

## Configure and inspect local learning

Learning policy applies to new runs; admitted runs retain their captured policy. An administrator can inspect it with `forge learning policy` and save a reviewed JSON policy with `forge learning configure policy.json`:

```json
{
  "enabled": true,
  "max_age_hours": 72,
  "max_candidates": 8,
  "max_repeated_attempts": 2,
  "max_lookup_calls": 30
}
```

The values shown are defaults. Disabling the policy affects later admissions. Required framework knowledge remains a separate setting and can still block generation when unavailable.

| Action | CLI command | Factory MCP tool |
| --- | --- | --- |
| Read policy | `forge learning policy` | `get_learning_policy` |
| Save policy | `forge learning configure policy.json` | `configure_learning_policy` |
| List authorized records | `forge learning records REPOSITORY_ID` | `list_learning_records` |
| Invalidate evidence | `forge learning invalidate RECORD_ID --reason REASON --request-id REQUEST_ID` | `invalidate_learning` |
| Read projection state | `forge learning projection RECORD_ID` | `get_learning_projection` |
| Submit once | `forge learning submit RECORD_ID --request-id REQUEST_ID` | `submit_learning_projection` |
| Observe an admitted source | `forge learning observe RECORD_ID --request-id REQUEST_ID --source-id SOURCE_ID` | `observe_learning_projection` |

Record, repository and request IDs use 32 lowercase hexadecimal characters; the source ID is the actual flycanon identifier. Each new action needs an explicit request identity. Inspect the record's source, checks and scope before submitting it. Submission requires verified local outcome evidence and current review authority. If its effect is unknown, observe the known source or retain the unresolved state; another submission must not replay it. The API and SDK expose the same actions. These commands do not create a cross-project grant or a new model session.

## Decide whether a search is needed

<figure class="forge-shot forge-architecture-diagram" markdown="1">
<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/learning-lookup-narrow.svg">
  <img src="../diagrams/learning-lookup.svg" alt="Bind the question to a task and source; retrieve authorized flycanon candidates; evaluate ledger evidence; either reuse an exact eligible outcome or record a bounded search; verify before retaining a lesson.">
</picture>

[Open full diagram](diagrams/learning-lookup.svg) · [Portrait layout](diagrams/learning-lookup-narrow.svg)

<figcaption>A missing reference and an unavailable required knowledge service have different outcomes. Required service failure stops the task; it does not silently trigger an internet search.</figcaption>
</figure>

The intended lookup begins with the current task and inspected source. It consults the authorized repository and framework knowledge scopes, then resolves candidates against the authoritative learning records. A similarity score can help find a record. Eligibility comes from its scope, evidence, source and version assumptions.

A verified exact match can supply its bounded local procedure result without a new provider request. Missing knowledge, stale references or a changed dependency context produce an explicit search reason. A requested refresh must perform a fresh lookup even if an older result exists. Credentials stay in private provider configuration, and external results retain their untrusted-reference status.

No result is silently moved from another repository into the task. If a known framework version differs from the product's version, that difference must remain visible. An absent version is unknown; the system cannot infer compatibility from a framework name alone.

## Keep authority and retrieval separate

| Component | Responsibility | Evidence it supplies |
| --- | --- | --- |
| PostgreSQL learning ledger | Persist status, applicability, decisions and immutable outcome references | Exact record version, state transitions and eligibility reasons |
| flycanon | Retain source knowledge, project memory and searchable candidates | Source/chunk IDs, content provenance and knowledge versions |
| Native fireflyframework-agentic worker | Propose changes and call the configured bounded tools | Typed worker feedback and actual tool results |
| Factory orchestrator | Admit tasks, enforce budgets, verify source and decide stage transitions | WAL intents, command outcomes, source custody and delivery receipts |
| Configured reviewer | Review the exact retained source and supported lesson statement | A separate review decision tied to the reviewed tree |

PostgreSQL is the authority for accepting a learning record. flycanon is its retrieval projection and the source-knowledge service. A semantic result must resolve back to a current authorized record before the factory treats it as an eligible learned outcome. A stale or missing projection never establishes acceptance.

The current runtime binds native tools through `fireflyframework_agentic.tools.firefly_tool` and executes a `FireflyAgent` with bounded model usage. Its `RuntimeReport`, `WorkerFeedback`, browser plans and QA reports are typed. Official coding CLI workers have their own tool and permission systems; their native calls do not automatically pass through every Python tool contract. See [agent tools](agent-tools.md) and [architecture](architecture.md#agent-roles-native-tools-and-cited-context) for the implemented boundaries.

## Record useful outcomes without overstating them

<figure class="forge-shot forge-architecture-diagram" markdown="1">
<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/learning-feedback-narrow.svg">
  <img src="../diagrams/learning-feedback.svg" alt="Keep source candidates, verified local outcomes and observed failed checks separate; apply repeat control to repair generation; preserve invalidation history; review any future shared framework lesson.">
</picture>

[Open full diagram](diagrams/learning-feedback.svg) · [Portrait layout](diagrams/learning-feedback-narrow.svg)

<figcaption>Passing a test supports its observed assertion at a particular source tree. It does not prove every retrieved search result correct or establish behavior on another version.</figcaption>
</figure>

A positive lesson describes a supported local procedure or source outcome. Its question, task and check receipts identify the behavior that was verified. A model's summary and a successful run status cannot establish that association. Search snippets remain candidates unless the retained evidence and review support the specific statement being admitted.

A negative record says that an observed command or check failed in an exact context. It retains the actual outcome and source identity. Generalizing the result to an entire framework, library or programming technique requires further evidence and review.

| Outcome | Learning treatment |
| --- | --- |
| Actual verification check passes at the retained tree | Eligible evidence for a bounded local outcome after the remaining receipt checks |
| Actual verification check fails | Scoped observed failure; the failed command and context remain attached |
| Deliberately failing regression test | Expected development evidence with an explicit purpose; independent checks continue |
| Rate limit, exhausted credits or authentication failure | Operational feedback and recovery state |
| Timeout, lost response or interrupted external action | Uncertain effect that requires reconciliation |
| A model says an approach is wrong or a search snippet says it works | Candidate statement requiring evidence |

## Bound repairs while preserving independent QA

An attempt fingerprint identifies the repository/component, exact source tree, dependency context, command and bounded outcome evidence. Repeat control applies to repair generation. If the worker proposes the same failed attempt without changing its context, the orchestrator can hold that attempt and request a different approach or an authorized override.

Independent final verification still runs. Diagnostics, intentional RED tests and regression checks retain an explicit purpose and cannot be skipped because a previous failure exists. Existing repair and clarification budgets continue to limit execution. A known failure is useful input for the next implementation decision; it cannot substitute for current QA.

Transient provider and infrastructure failures must not poison the engineering knowledge base. Their recovery belongs to the execution log, provider controls and [reconciliation workflow](recovery.md).

## Structure agent and tool communication

A learning exchange needs a schema version, message type and stable correlation identity. Its run/task references identify the originating execution; causation references connect the question, tool request, result and later review. The source and configuration references must be observed by the factory or resolved from retained records.

| Contract group | Required association |
| --- | --- |
| Scope | Current actor authority, repository and optional product/component references |
| Execution | Run, task, effective role and stable request identity |
| Applicability | Inspected source identity, observed framework/dependency context and frozen policy revision |
| Tool result | Correlation/request digest, receipt disposition and cited source references |
| Verification | Actual command/checkpoint receipts and configured review evidence |
| Freshness | Database-time observation, validity rule and explicit refresh requirement |
| Decision | Eligibility reason, admitted record version and unresolved effect references |

The worker can propose a lesson, ask for missing knowledge or report a blocker. It cannot set a trusted verification flag, grant itself cross-project access or override a pending unknown effect. The native factory checks the message against the declared contract before changing execution state.

The lifecycle view distinguishes deterministic factory roles from model sessions. A product-manager or architect instruction role does not imply a separate autonomous model conversation. The implementation provider and an optional independent reviewer have actual execution bindings. [Agent lifecycle](agent-lifecycle.md) explains their participants and retained handoffs.

## Preserve updates, expiry and failed writes

Records progress through candidate, verified, invalidated and superseded states with retained evidence. A changed source or version makes an older record ineligible for exact reuse. Expiry requires refresh. Invalidation records the reason and actor; supersession names the replacement while preserving the old observations.

Database time defines freshness and admission decisions across workers. A frozen learning policy identifies the limits used by an admitted run. Legacy runs must keep their prior behavior and authority instead of acquiring new reuse permissions after an upgrade.

A flycanon projection write has a persisted intent before dispatch. A returned source ID supports read-only status inspection. If a response is lost and the remote outcome is unknown, unsupported source or knowledge creation must not be replayed automatically. Reconciliation must establish the actual remote record or retain the unresolved state.

## Share between projects deliberately

Repository-private knowledge is the default. A future shared framework lesson requires an explicit reviewed, generalized payload with named public source, version and license provenance. Private code, product details, repository paths and credentials must remain outside that payload. Its review must also define how invalidating the origin or changing version assumptions affects the shared lesson.

L1 retains private origins and does not implement automatic global promotion. Framework manuals already available in the configured shared workspace remain reviewed references with their original scope and provenance.

## Qualification required before acceptance

Native tests must establish that an exact eligible result avoids a provider request, an unknown or stale result cannot do so, required flycanon failure prevents internet fallback, and two repositories cannot reuse each other's private lessons. Real owned Git/check evidence must establish positive and negative outcomes. Concurrency, policy freezing and unknown-write recovery need PostgreSQL evidence.

A native `FireflyAgent` tool call must exercise the learning path; a standalone policy test does not establish runtime integration. Local flycanon projection and actual provider execution remain separate acceptance checks. Configuration alone cannot establish either.

Continue with [knowledge configuration](knowledge.md), [search providers](search-providers.md), [agent lifecycle](agent-lifecycle.md) and [WAL recovery](recovery.md).
