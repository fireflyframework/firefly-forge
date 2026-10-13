# Factory roles and community skills

A role assigns responsibility to an actual factory stage. It does not create a separate model session. The default implementation uses one selected coding provider, deterministic planning and source checks, real repository verification, and an optional explicitly configured assurance reviewer.

## Responsibilities and execution

| Role | Execution | Inputs and outputs |
| --- | --- | --- |
| Product manager | Deterministic preparation | Retain customer decisions, journey-to-feature expectations and pending definitions in the frozen handoff. |
| Technical lead | Deterministic preparation, verification and checkpoint | Manage the development plan, bounded repair evidence and delivery checkpoint. |
| Architect | Deterministic preparation | Retain reviewed layers, components, interfaces and knowledge references. No separate architectural model call is implied. |
| Implementer | Run-selected coding provider | Edit the isolated source and tests using frozen product, feature, architecture and design references. |
| QA | Deterministic executable verification | Run configured checks and retain real outcomes. Optional assurance review uses its configured provider independently. |
| Security | Mandatory factory boundary checks | Enforce repository scope, credentials, source integrity and publication checks. This does not claim a penetration test. |
| Docs | Optional guidance to the implementation provider | Write reproducible installation, usage and operational instructions. It uses the same coding session. |

`RoleDefinition` retains responsibilities, provider binding, stages, skills, inputs, outputs, instructions, feedback and escalation guidance. Core execution bindings are fixed to implemented stages. Operators may edit their instructions and skill/tool selections; mandatory factory roles and platform gates cannot be disabled.

## Version and project selection

Authenticated operators read or replace the registry at `GET /api/v1/agent-roles` and `PUT /api/v1/agent-roles`. The replacement is a `RoleDocument` with a version, maximum request budget of 1–3, and all seven supported roles. It is stored in private factory configuration.

Project/run role overrides use this shape:

```json
{
  "enabled": ["docs"],
  "disabled": [],
  "skills": {
    "implementer": ["implementation", "test-driven-development", "security", "frontend-design"],
    "docs": ["documentation", "humanizer"]
  },
  "allowed_tools": {
    "implementer": ["list_files", "read_file", "write_file"]
  }
}
```

A new run freezes the registry revision, selected roles, selected instructions, provenance, actual tool availability and a content digest. Later edits do not change that handoff. Product grid, typography and colors belong to the product definition and its brand snapshot, not the role registry. The implementer receives reviewed references; QA compares behavior with the journey-to-feature contract and its acceptance and failure paths.

## Native tools and official CLI tools

Native Firefly Agentic and Bridge inference register only the intersection of requested tools and the available configured executor. The base workspace tools are `list_files`, `read_file`, `write_file`, `search_text`, `edit_lines` and `git_diff`. Commands, browser checks, browser journeys and internet search require their configured executors and explicit frozen grants. An unavailable tool remains unavailable when a role requests it. See [agent tools](agent-tools.md) and [browser journeys](browser-qa.md) for their evidence and limits.

Official Codex and Claude workers use their own native tools and host permissions. The Python native-tool selection is advisory for these CLI workers; it does not establish an equivalent sandbox or restrict their native tool surface. Use the official worker's supported permission controls and a trusted isolated execution host. A role instruction never grants deployment, publication, arbitrary network or credential access.

## Worker feedback and manager requests

Feedback is typed data with a stable ID, role, planned task, attempt 1–4, disposition, summary and bounded public workspace evidence paths. Dispositions are `completed`, `failed`, `blocked` or `clarification`. A blocker or clarification must carry exactly one request with an ID, kind, question, context and optional choices.

The manager journals feedback before pausing. Questions survive restarts and do not trigger automatic retries. Replaying the same feedback is idempotent; reusing its ID with different content fails. The frozen request budget limits new questions. A response is evidence of an answer, not deployment or publication approval.

Read the durable conversation at `GET /api/v1/runs/{id}/worker-messages`. An authenticated operator answers through `POST /api/v1/runs/{id}/worker-requests/{request_id}/respond`:

```json
{"id":"response-1","actor":"operator","answer":"Support account owners receive the alerts."}
```

Answering leaves the run paused. Resume explicitly after resolving the question. A pending request must be answered before resuming. Responses remain in the journal alongside prior attempts and their evidence.

For issue-bound runs, the existing source-control feedback intent records the pending question before posting it. An enabled `/forge respond` command lets a trusted configuration actor answer in that repository:

```text
/forge respond {"run_id":"0123456789abcdef0123456789abcdef","request_id":"request-1","answer":"Support account owners receive the alerts."}
```

The signed receipt supplies the actor identity and response ID. The command cannot answer another repository's run, forge operator credentials are never sent to the provider, and uncertain comment writes require reconciliation rather than blind reposting. `/forge resume` remains a separate enabled action.

## Pinned community instructions

The catalog contains instruction adaptations, not downloaded executable plugins. Each adaptation retains its source commit/file, SHA-256, license text and adaptation hash. Validation detects changed adaptation or license bytes. The original source hash identifies the researched file; it is not a claim that the shortened adaptation is byte-identical to that file.

| Instructions | Source revision | License |
| --- | --- | --- |
| Thinking, TDD, debugging and evidence | [Superpowers bb92a777](https://github.com/obra/superpowers/tree/bb92a77741419a4ab5f06e711a283343f1ada0c3) | MIT; Jesse Vincent |
| Frontend design and browser QA | [Anthropic skills dbd4588f](https://github.com/anthropics/skills/tree/dbd4588f9e1033efb41dad4bef2f7947c8993d44) | Apache 2.0, verified separately for each selected skill |
| Human-readable prose | [Humanizer 225a6f39](https://github.com/blader/humanizer/tree/225a6f39ac85f76ee48dbad772ea4abe4ed6c9d8) | MIT; Siqi Chen |

The adaptations preserve authorized product decisions, factory request/repair budgets and actual tooling. They exclude upstream helper execution, installation commands and automatic commits or publication. Humanizer applies to prose while preserving commands, contracts, code and links; pattern checks do not prove editorial quality. First-party security instructions supplement these sources without claiming a community security certification.
