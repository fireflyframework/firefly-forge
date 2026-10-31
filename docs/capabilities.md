---
description: Understand what factory receipts establish for generated source, product contracts, verification, integrations and delivery.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Know what the factory establishes

forge implements an end-to-end development loop: register a Git source, queue a
brief with stack and provider, generate in an isolated workspace, run
verification, retain change evidence and optionally publish a pull request.

| Surface | What is implemented | Evidence boundary |
| --- | --- | --- |
| Git workspace | Clone a configured source and create a run branch | Does not isolate the host operating system |
| Primary backends | [pyfly](https://fireflyframework.org/frameworks/python/) and [larafly](https://fireflyframework.org/frameworks/php/) instruction profiles | Specific products require their own live checks |
| Additional backends | Java, Rust and .NET profiles | Source inspection is separate from product runtime validation |
| Browser products | Angular, React, Next.js and Vue profiles | Build success is separate from visual and interaction acceptance |
| Codex / Claude Code | Official signed-in CLI execution paths | Installed and authenticated readiness is separate from live generation |
| Firefly Agentic | Native API-driven workspace tool execution | API configuration is separate from actual model access |
| Agent bridge | Compatible endpoint client | Local bridge service and subscription login are configured separately |
| Verification | Explicit or discovered project commands with retained results | Commands establish only what they test |
| Automatic repair | Failed checks returned to the provider with bounded retries | Earlier failures remain recorded; final passing checks are separate |
| Source integrity | Verified source-tree snapshot checked before publication | Changed source requires fresh verification |
| SCM publication | GitHub, GitLab, Bitbucket, Azure DevOps and Gitea adapters | Each live host/account requires separate acceptance |
| Factory dashboard | Current-authorized factory/project/run counts, paged work and component repositories | Recorded running states do not establish live worker activity |
| Personal read tokens | Owner-managed issuance, rotation and revocation over selected current project memberships | No execution, administration or factory-wide observations |
| Local organization sign-in | Branded Keycloak sandbox preparation, native OIDC/PKCE and durable sessions | External federation and production provider operations require their own acceptance |
| Run persistence | Native PostgreSQL plans, task states and hash-chain journal | Ambiguous remote effects require reconciliation |
| Maintenance and tests | Existing-source maintenance and model-free test mode | Checks establish only their asserted behavior |
| Policies | Versioned, data-only central catalog and project defaults | Recommendations guide workers; optional gates execute commands |
| Flycanon | Included managed service, required cited project/framework retrieval and automatic verified memory on fresh setup | Actual embedding, scope grants and memory receipts establish readiness; existing configurations are preserved |
| Deployment | Explicit repository-bound targets, command receipts and rollback verification | External targets require their own live acceptance |
| Service ownership | Local lock plus PostgreSQL schema advisory lease | Independent installations need separate directories and schemas |

## Read a successful run precisely

Generation means the provider returned a result. Verification means configured
commands actually ran and passed. Visual review means someone inspected the UI
at the stated viewports and exercised its controls. Live integration means the
real external service returned the expected outcome. Publication means a remote
branch or pull request was actually created. These facts are independent.

This release is intended for trusted development environments. It does not
claim tenant isolation, regulated decision authority, production availability,
an SLA, unconfigured deployment, a universal migration path or correctness for an
arbitrary brief. The operator remains responsible for reviewing generated
changes and the resulting product's own tests and release process.

See the [release notes](release-notes.md) for the specific preparation
checks and remaining live-acceptance boundaries.
